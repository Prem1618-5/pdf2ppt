"""Vision-LLM layout pass: one Gemini call per page returns question fragments with
roles, labels, continuation flags and normalised boxes (schema-constrained JSON).

Cost control: downscaled JPEG input, one call per page, on-disk cache keyed by the
image hash + prompt version, client-side RPM throttle, exponential backoff on 429/5xx,
and model fallback. When every model is exhausted the caller falls back to the
classical (no-LLM) layout.
"""
import base64
import hashlib
import json
import os
import random
import threading
import time

import requests

PROMPT_VERSION = "v5"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

ROLES = ["question", "continuation", "shared_passage", "solution", "answer_key",
         "instructions", "section_header", "clutter"]

PAGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "page": {"type": "INTEGER"},
        "page_kind": {"type": "STRING", "enum": ["questions", "cover_or_instructions", "answer_key",
                                                 "solutions", "blank_or_ad", "mixed"]},
        "dark_background": {"type": "BOOLEAN"},
        "watermark_present": {"type": "BOOLEAN"},
        "fragments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "order": {"type": "INTEGER"},
            "role": {"type": "STRING", "enum": ROLES},
            "label": {"type": "STRING", "nullable": True},
            "number": {"type": "INTEGER", "nullable": True},
            "continues_previous": {"type": "BOOLEAN"},
            "continues_on_next": {"type": "BOOLEAN"},
            "box": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "lang": {"type": "STRING", "enum": ["en", "hi", "bilingual", "other"]},
            "has_figure": {"type": "BOOLEAN"},
            "has_math": {"type": "BOOLEAN"},
            "has_table": {"type": "BOOLEAN"},
            "stem": {"type": "STRING"},
            "options": {"type": "ARRAY", "items": {"type": "STRING"}},
            "confidence": {"type": "NUMBER"},
        }, "required": ["order", "role", "box", "continues_previous", "continues_on_next", "lang",
                        "has_figure", "has_math", "has_table", "confidence"]}},
    },
    "required": ["page", "page_kind", "dark_background", "watermark_present", "fragments"],
}
SCHEMA = {"type": "OBJECT", "properties": {"pages": {"type": "ARRAY", "items": PAGE_SCHEMA}},
          "required": ["pages"]}

PROMPT = """You are a document layout analyser for exam question papers. You receive {n} page image(s), in document order, each preceded by its tag PAGE k. Analyse EVERY page and return one entry per page in "pages" (page = k). Each image is one page (it may be a photo of a two-page booklet spread, a scan, a screenshot or a video frame; it may have 1-4 text columns, a table layout, watermarks, logos, headers and footers).

Return every content region as a fragment, in true human READING ORDER (columns left-to-right, top-to-bottom inside a column; full-width blocks in their natural position; for a two-page spread finish the whole left page before the right page).

Roles:
- question: a region that STARTS a question. It must include the printed question number/label, the stem, any figure/diagram/table/graph belonging to it (also when placed beside the text), statements/lists, and all its options. One question = one fragment unless it is physically split.
- continuation: the rest of a question split by a column break or page break (e.g. options sitting at the top of the next column, or a first block of a page that has no question number / says "contd."). Set continues_previous=true. A line like "(a) 1 (b) 2 (c) 3 (d) 4" at the top of a column is a continuation, never a new question.
- shared_passage: a passage, data table or figure needed by several following questions (comprehension, data interpretation). Short generic directions like "Directions (Q.1-5): choose the correct option" are role=instructions.
- solution: "Ans.", "Sol.", "Answer:", hints, worked solutions or explanations. answer_key: answer tables/lists.
- instructions, section_header (e.g. "PART-A", "SECTION A", "Trade Theory", subject bands), clutter (logos, running headers/footers, URLs, page numbers, brand banners, marks/negative-marks columns, video player UI, subscribe banners, cursors).

Rules:
- box = [ymin, xmin, ymax, xmax] normalised 0-1000 on THAT page's image. Question boxes must be COMPLETE (question number, every option line, every figure) and must not include neighbouring questions, other columns, solutions or clutter. Fragments must not overlap.
- label = the question label exactly as printed ("7.", "Q.43", "(1)", "12"); number = its integer. Option labels (a)/(A)/(1)/A1 are never question labels; numbers inside a stem are not labels. In a table layout the serial-number cell is the label.
- continues_on_next = true if the question is cut at the bottom of its column/page and continues later (possibly on the next page).
- A continuation at the top of PAGE k+1 belongs to the open question at the end of PAGE k; still return it on PAGE k+1 with continues_previous=true.
- lang: language of the question text (bilingual = English and Hindi both inside this fragment).
- stem: the question text in plain Unicode (maths with Unicode symbols, Devanagari as is), without the label. options: each option text without its (a)/(b) label. For a continuation give only the text it contains.
- watermark_present: true if a faint logo/text watermark sits behind the page content.
- dark_background: true if the page has light text on a dark background (e.g. a video frame).
- confidence 0-1.
Skip nothing that is a question. Do not invent questions."""


class QuotaExhausted(Exception):
    pass


class GeminiLayout:
    def __init__(self, settings, log=print):
        self.s = settings
        self.log = log
        self.models = list(settings.model_list)
        self.dead_models = set()
        self.lock = threading.Lock()
        self.last_calls = []
        self.calls = 0
        self.only_lite = False
        self.cache_hits = 0
        os.makedirs(os.path.join(settings.cache_dir, "layout"), exist_ok=True)

    # -- throttle: at most `gemini_rpm` requests in any 60 s window ------------------
    def _throttle(self):
        while True:
            with self.lock:
                now = time.time()
                self.last_calls = [t for t in self.last_calls if now - t < 60]
                if len(self.last_calls) < max(1, self.s.gemini_rpm):
                    self.last_calls.append(now)
                    return
                wait = 60 - (now - self.last_calls[0]) + 0.1
            time.sleep(wait)

    def _cache_path(self, jpeg):
        h = hashlib.sha256(jpeg + PROMPT_VERSION.encode()).hexdigest()
        return os.path.join(self.s.cache_dir, "layout", h + ".json")

    def analyse(self, jpeg):
        return self.analyse_batch([jpeg])[0]

    def analyse_batch(self, jpegs):
        """One request for several pages; per-page results are cached individually so
        a rerun (or a different batch size) costs nothing."""
        results = [None] * len(jpegs)
        soft = {}      # lite-model results: used only if no better model answers
        todo = []
        for i, j in enumerate(jpegs):
            cp = self._cache_path(j)
            if os.path.exists(cp):
                with open(cp, encoding="utf-8") as f:
                    cached = json.load(f)
                if "lite" in cached.get("_model", "") and not self.only_lite:
                    soft[i] = cached
                    todo.append(i)
                else:
                    self.cache_hits += 1
                    results[i] = cached
            else:
                todo.append(i)
        if not todo:
            return results
        if not self.s.gemini_api_key:
            raise QuotaExhausted("no GEMINI_API_KEY configured")
        if soft and len(soft) == len(todo):
            try:
                return self._fresh(jpegs, todo, results, better_only=True)
            except QuotaExhausted:
                self.only_lite = True
                for i, pg in soft.items():
                    self.cache_hits += 1
                    results[i] = pg
                return results
        return self._fresh(jpegs, todo, results)

    def _fresh(self, jpegs, todo, results, better_only=False):
        parts = []
        for k, i in enumerate(todo, 1):
            parts.append({"text": f"PAGE {k}"})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpegs[i]).decode()}})
        parts.append({"text": PROMPT.replace("{n}", str(len(todo)))})
        out = self._call(parts, better_only=better_only)
        by_page = {}
        for pg in out.get("pages", []):
            by_page.setdefault(pg.get("page"), pg)
        for k, i in enumerate(todo, 1):
            pg = by_page.get(k)
            if pg is None:
                continue  # model skipped a page; caller falls back for it
            pg["_model"] = out["_model"]
            n_q = sum(1 for fr in pg.get("fragments", []) if fr.get("role") == "question")
            if pg.get("page_kind") in ("questions", "mixed") and n_q <= 1 and len(todo) > 1:
                # suspiciously empty page inside a batch: retry it alone, don't cache this
                self.log(f"page result looks incomplete ({n_q} question); retrying it alone")
                try:
                    pg = self.analyse_batch([jpegs[i]])[0] or pg
                except QuotaExhausted:
                    pass
                results[i] = pg
                continue
            # lite results are cached too, but as "soft" entries a better model may replace
            with open(self._cache_path(jpegs[i]), "w", encoding="utf-8") as f:
                json.dump(pg, f, ensure_ascii=False)
            results[i] = pg
        return results

    def _call(self, parts, better_only=False):
        gen = {"responseMimeType": "application/json", "responseSchema": SCHEMA,
               "temperature": 0, "maxOutputTokens": 60000, "thinkingConfig": {"thinkingLevel": "low"}}
        body = {"contents": [{"parts": parts}], "generationConfig": gen}
        last_err = None
        for rnd in range(1 if better_only else 4):   # 503 storms are transient: go round again
            out = self._call_round(body, gen, better_only)
            if isinstance(out, dict):
                return out
            last_err = out[1]
            if out[0] == "dead":
                break
            self.log(f"all models busy; retrying in {30 * (rnd + 1)}s")
            time.sleep(30 * (rnd + 1))
        raise QuotaExhausted(last_err or "all models exhausted")

    def _call_round(self, body, gen, better_only=False):
        last_err, busy = None, False
        for model in self.models:
            if model in self.dead_models or (better_only and "lite" in model):
                continue
            gen_m = dict(gen)
            for attempt in range(5):
                self._throttle()
                body["generationConfig"] = gen_m
                try:
                    r = requests.post(API.format(model=model), json=body, timeout=420,
                                      headers={"x-goog-api-key": self.s.gemini_api_key})
                except requests.RequestException as e:
                    last_err = str(e)
                    time.sleep(min(60, 2 ** attempt + random.random()))
                    continue
                if r.status_code == 200:
                    try:
                        j = r.json()
                        text = j["candidates"][0]["content"]["parts"][-1]["text"]
                        out = json.loads(text)
                    except Exception as e:  # truncated / blocked output
                        last_err = f"{model}: bad response: {e}"
                        self.log(last_err)
                        continue
                    self.calls += 1
                    out["_model"] = model
                    return out
                msg = r.text
                last_err = f"{model}: HTTP {r.status_code} {msg[:200]}"
                if r.status_code == 400 and "thinking" in msg.lower() and "thinkingConfig" in gen_m:
                    gen_m = {k: v for k, v in gen_m.items() if k != "thinkingConfig"}
                    continue
                if r.status_code in (400, 403, 404):
                    self.log(f"model {model} unusable (HTTP {r.status_code}); trying next")
                    self.dead_models.add(model)
                    break
                if r.status_code == 429:
                    if "PerDay" in msg or "per day" in msg.lower() or "limit: 0" in msg:
                        self.log(f"daily free-tier quota used up on {model}; switching model")
                        self.dead_models.add(model)
                        break
                    delay = min(90, 5 * 2 ** attempt) + random.random() * 3
                    self.log(f"rate limited on {model}; waiting {delay:.0f}s")
                    time.sleep(delay)
                    continue
                if r.status_code in (500, 502, 503, 504) and attempt >= 1:
                    self.log(f"{model} overloaded (HTTP {r.status_code}); trying next model")
                    busy = True
                    break
                time.sleep(min(30, 3 * 2 ** attempt + random.random()))
        return ("busy" if busy else "dead", last_err)
