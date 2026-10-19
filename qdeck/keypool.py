"""Many Gemini API keys behind one call site.

Each (key, model) pair is a "lane" with its own state:
  ok -> cooling (per-minute 429, short retry) -> ok
  ok -> exhausted (daily free-tier quota; until Google's retry hint or next midnight PT)
A key can also be `invalid` (bad / revoked key) and a model can be `missing` (404) or
`busy` (503 for everybody, short pause).

acquire() returns the best model that any key can serve right now, rotating keys
round-robin so load spreads evenly; when every usable lane is only cooling it tells the
caller how long to wait; when nothing can ever succeed today it raises NoLane.

Health is shared across jobs (module-level registry keyed by a hash of the key, never
the key itself), so a key that ran dry in one job is skipped by the next one.
Keys live in memory only.
"""
import datetime as dt
import hashlib
import re
import threading
import time

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    _PT = None

_REGISTRY = {}          # key hash -> {"invalid": bool, "lanes": {model: {...}}, "calls": int}
_REG_LOCK = threading.Lock()


class NoLane(Exception):
    pass


def mask(key):
    return "…" + key[-4:] if key else "…"


def _khash(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def next_reset():
    """Free-tier daily quotas reset at midnight Pacific time."""
    if _PT is None:
        return time.time() + 6 * 3600
    now = dt.datetime.now(_PT)
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return tomorrow.timestamp()


def parse_retry(msg):
    """Seconds from Google's hints: '"retryDelay": "37s"' or 'retry in 16h41m51.3s'."""
    m = re.search(r'retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', msg)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", msg)
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def split_keys(text):
    keys = []
    for k in re.split(r"[\s,;]+", text or ""):
        k = k.strip().strip('"\'')
        if len(k) >= 20 and k not in keys:
            keys.append(k)
    return keys


class KeyPool:
    def __init__(self, keys, models, rpm_per_key=8):
        self.keys = list(dict.fromkeys(k for k in keys if k))
        self.models = list(models)
        self.rpm = max(1, rpm_per_key)
        self.lock = threading.Lock()
        self.recent = {k: [] for k in self.keys}     # request timestamps per key
        self.rr = 0
        self.model_busy_until = {}
        self.model_missing = set()
        with _REG_LOCK:
            for k in self.keys:
                _REGISTRY.setdefault(_khash(k), {"invalid": False, "lanes": {}, "calls": 0, "error": None})

    def __len__(self):
        return len(self.keys)

    # ------------------------------------------------------------------ state
    def _rec(self, key):
        return _REGISTRY[_khash(key)]

    def _lane(self, key, model):
        return self._rec(key)["lanes"].setdefault(model, {"until": 0.0, "kind": "ok"})

    def acquire(self, better_only=False):
        """-> (key, model) or (None, wait_seconds). Raises NoLane if nothing is left."""
        with self.lock, _REG_LOCK:
            now = time.time()
            soonest = None
            any_future = False
            for model in self.models:
                if model in self.model_missing or (better_only and "lite" in model):
                    continue
                busy = self.model_busy_until.get(model, 0)
                n = len(self.keys)
                for j in range(n):
                    key = self.keys[(self.rr + j) % n]
                    rec = self._rec(key)
                    if rec["invalid"]:
                        continue
                    lane = self._lane(key, model)
                    wait = max(lane["until"] - now, busy - now, 0.0)
                    if lane["kind"] == "exhausted" and lane["until"] > now:
                        continue                     # dead for today
                    self.recent[key] = [t for t in self.recent[key] if now - t < 60]
                    if len(self.recent[key]) >= self.rpm:
                        wait = max(wait, 60 - (now - self.recent[key][0]))
                    if wait <= 0:
                        self.rr = (self.rr + j + 1) % n
                        self.recent[key].append(now)
                        rec["calls"] += 1
                        return key, model
                    any_future = True
                    soonest = wait if soonest is None else min(soonest, wait)
            if not any_future:
                raise NoLane("every key is invalid or out of daily quota for every model")
            return None, soonest

    def report(self, key, model, status, body=""):
        """Feed back the HTTP outcome of a request made with (key, model)."""
        with self.lock, _REG_LOCK:
            now = time.time()
            rec = self._rec(key)
            lane = self._lane(key, model)
            low = body.lower()
            if status == 200:
                lane.update(kind="ok", until=0.0)
                rec["error"] = None
                return "ok"
            if status in (400, 401, 403) and ("api key" in low or "api_key" in low or "permission" in low
                                              or "unauthenticated" in low):
                rec["invalid"] = True
                rec["error"] = "invalid key"
                return "invalid"
            if status == 404:
                self.model_missing.add(model)
                return "missing"
            if status == 429:
                retry = parse_retry(body)
                daily = "perday" in low.replace("_", "").replace(" ", "") or "limit: 0" in low or \
                    (retry is not None and retry > 600)
                if daily:
                    lane.update(kind="exhausted", until=now + retry if retry and retry > 600 else next_reset())
                    rec["error"] = f"daily quota used on {model}"
                    return "exhausted"
                lane.update(kind="cooling", until=now + (retry if retry else 20.0) + 1.0)
                rec["error"] = "rate limited (per minute)"
                return "cooling"
            if status in (500, 502, 503, 504):
                self.model_busy_until[model] = now + 20.0
                return "busy"
            lane.update(kind="cooling", until=now + 10.0)
            return "error"

    def stats(self):
        out = []
        with _REG_LOCK:
            now = time.time()
            for k in self.keys:
                rec = self._rec(k)
                lanes = [rec["lanes"].get(m, {"kind": "ok", "until": 0}) for m in self.models
                         if m not in self.model_missing]
                if rec["invalid"]:
                    state = "invalid"
                elif lanes and all(l["kind"] == "exhausted" and l["until"] > now for l in lanes):
                    state = "exhausted"
                elif any(l["kind"] == "ok" or l["until"] <= now for l in lanes):
                    state = "ok"
                else:
                    state = "cooling"
                out.append({"masked": mask(k), "state": state, "calls": rec["calls"], "error": rec["error"]})
        return out

    @property
    def parallelism(self):
        """More keys -> more requests in flight (bounded for a small server)."""
        live = sum(1 for k in self.keys if not self._rec(k)["invalid"])
        return max(1, min(8, live * 2))
