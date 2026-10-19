"""Key rotation with mocked Gemini responses (no network)."""
import json

import pytest

from qdeck import keypool
from qdeck.config import Settings
from qdeck.keypool import KeyPool, NoLane, parse_retry


class R:
    def __init__(self, code, body):
        self.status_code, self.text = code, body

    def json(self):
        return json.loads(self.text)


OK = json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps({"pages": [{"page": 1, "page_kind": "questions", "dark_background": False, "watermark_present": False, "fragments": []}]})}]}}]})
DAILY = '{"error":{"code":429,"message":"Quota exceeded ... GenerateRequestsPerDayPerProjectPerModel-FreeTier. Please retry in 16h41m51s."}}'
MINUTE = '{"error":{"code":429,"message":"rate","details":[{"retryDelay":"1s"}]}}'
INVALID = '{"error":{"code":400,"message":"API key not valid. Please pass a valid API key."}}'


def k(i):
    return f"AIzaFAKEKEYxxxxxxxxxxxxxxxxxxxx{i:04d}"


@pytest.fixture(autouse=True)
def fresh_registry():
    keypool._REGISTRY.clear()


def test_parse_retry():
    assert parse_retry('"retryDelay": "37s"') == 37
    assert parse_retry("Please retry in 16h41m51.5s.") == pytest.approx(16 * 3600 + 41 * 60 + 51.5)


def test_rotation_through_failures(monkeypatch, tmp_path):
    from qdeck import layout_gemini
    behaviour = {k(0): [DAILY], k(1): [INVALID], k(2): [MINUTE, OK], k(3): [OK]}
    seen = []

    def fake_post(url, json=None, timeout=None, headers=None):
        key = headers["x-goog-api-key"]
        model = url.split("/models/")[1].split(":")[0]
        seen.append((key[-6:], model))
        seq = behaviour[key]
        body = seq.pop(0) if len(seq) > 1 else seq[0]
        code = 200 if body == OK else (400 if body == INVALID else 429)
        return R(code, body)

    monkeypatch.setattr(layout_gemini.requests, "post", fake_post)
    s = Settings()
    s.cache_dir = str(tmp_path)
    s.gemini_models = "m-flash,m-flash-lite"
    g = layout_gemini.GeminiLayout(s, log=lambda *_: None, keys=[k(0), k(1), k(2), k(3)])
    out = g.analyse_batch([b"jpeg-bytes-1"])
    assert out[0]["page_kind"] == "questions"
    st = {x["masked"][-4:]: x for x in g.pool.stats()}
    assert st[k(1)[-4:]]["state"] == "invalid"
    # key 0 is out of daily quota on m-flash only, so it may still serve m-flash-lite
    assert keypool._REGISTRY[keypool._khash(k(0))]["lanes"]["m-flash"]["kind"] == "exhausted"


def test_everything_exhausted_raises():
    pool = KeyPool([k(5), k(6)], ["m1"], rpm_per_key=100)
    for key in (k(5), k(6)):
        got_key, model = pool.acquire()
        pool.report(got_key, model, 429, DAILY)
    with pytest.raises(NoLane):
        pool.acquire()


def test_round_robin_spreads_load():
    keys = [k(i) for i in range(50)]
    pool = KeyPool(keys, ["m1"], rpm_per_key=1)
    used = set()
    for _ in range(50):
        key, model = pool.acquire()
        assert key is not None
        used.add(key)
    assert len(used) == 50                       # every key used once before any repeats
    key, wait = pool.acquire()                   # all at their 1-rpm budget now -> wait
    assert key is None and 0 < wait <= 60
    assert pool.parallelism == 8


def test_busy_model_falls_back_to_next_model():
    pool = KeyPool([k(9)], ["big", "lite"], rpm_per_key=100)
    key, model = pool.acquire()
    assert model == "big"
    pool.report(key, model, 503, "")
    key, model = pool.acquire()
    assert model == "lite"
