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

from .keypool import KeyPool, NoLane, mask

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
    def __init__(self, settings, log=print, keys=None):
        self.s = settings
        self.log = log
        self.models = list(settings.model_list)
        self.pool = KeyPool(keys if keys else settings.key_list, self.models, settings.gemini_rpm)
        self.lock = threading.Lock()
        self.calls = 0
        self.only_lite = False
        self.cache_hits = 0
        os.makedirs(os.path.join(settings.cache_dir, "layout"), exist_ok=True)

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
        if not len(self.pool):
            raise QuotaExhausted("no Gemini API key configured")
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
        """Send one request through the key pool. Rotates keys/models on 429/503,
        waits when every lane is only cooling, gives up when every lane is dead."""
        gen = {"responseMimeType": "application/json", "responseSchema": SCHEMA,
               "temperature": 0, "maxOutputTokens": 60000, "thinkingConfig": {"thinkingLevel": "low"}}
        no_thinking = set()
        last_err, waited, bad_json = None, 0.0, 0
        deadline = time.time() + (120 if better_only else 1800)
        while time.time() < deadline:
            try:
                key, model = self.pool.acquire(better_only=better_only)
            except NoLane as e:
                raise QuotaExhausted(f"{e}; last error: {last_err}")
            if key is None:                      # everything is cooling: wait it out
                wait = min(max(model, 1.0), 90.0)
                if better_only and wait > 30:
                    raise QuotaExhausted("better models busy")
                self.log(f"all keys busy; waiting {wait:.0f}s")
                time.sleep(wait)
                waited += wait
                continue
            g = {k: v for k, v in gen.items() if not (k == "thinkingConfig" and model in no_thinking)}
            body = {"contents": [{"parts": parts}], "generationConfig": g}
            try:
                r = requests.post(API.format(model=model), json=body, timeout=420,
                                  headers={"x-goog-api-key": key})
            except requests.RequestException as e:
                last_err = f"network: {type(e).__name__}"
                self.pool.report(key, model, 503, "")
                time.sleep(2 + random.random() * 3)
                continue
            if r.status_code == 400 and "thinking" in r.text.lower() and model not in no_thinking:
                no_thinking.add(model)
                continue
            outcome = self.pool.report(key, model, r.status_code, r.text)
            if r.status_code == 200:
                try:
                    j = r.json()
                    out = json.loads(j["candidates"][0]["content"]["parts"][-1]["text"])
                except Exception as e:          # truncated / blocked output
                    bad_json += 1
                    last_err = f"{model}: unreadable response ({type(e).__name__})"
                    if bad_json >= 3:
                        raise QuotaExhausted(last_err)
                    continue
                with self.lock:
                    self.calls += 1
                out["_model"] = model
                return out
            last_err = f"{model}: HTTP {r.status_code}"
            if outcome in ("exhausted", "invalid", "missing"):
                self.log(f"key {mask(key)} / {model}: {outcome}; switching")
            elif outcome == "busy":
                self.log(f"{model} overloaded (503); trying another model")
        raise QuotaExhausted(last_err or "timed out waiting for a free key")
