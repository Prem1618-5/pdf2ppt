"""No-LLM layout fallback (used when the Gemini quota is gone, or USE_LLM=0).

* Pages with a text layer: question anchors are lines that start with a number
  pattern, sit at the left edge of a column, and continue the numbering sequence
  (integrity filter: this rejects numbers inside stems and "(a) 1 (b) 2" lines).
* Image-only pages: whitespace projection profiles -> columns -> blocks. Without OCR
  the question boundaries are guesses, so these fragments carry low confidence.

Output uses the same structure as the Gemini layout (boxes 0-1000, ymin/xmin/ymax/xmax).
"""
import re
from collections import Counter

import cv2
import numpy as np

ANCHOR = re.compile(r"^\s*(?:Q(?:ue(?:stion)?)?\s*\.?\s*(?:No\.?)?\s*)?(\d{1,3})\s*(?:[.):\]]|\s|$)", re.I)
CLUTTER = re.compile(r"(www\.|https?://|\.com\b|\.in\b|page\s*\d+|p\.?\s*t\.?\s*o\b)", re.I)
MARKS = re.compile(r"^\s*[-+]?\d+\.\d+\s*$")
ANSWER_KEY = re.compile(r"answer\s*key|answers?\s*:|^\s*key\s*$", re.I)
PAGE_NO = re.compile(r"^\s*[-\[(]?\s*\d+\s*[-\])]?\s*$")
INSTRUCTION_WORDS = re.compile(r"\b(tick|marks?|negative|candidates?|answer sheet|booklet|carry|"
                               r"instructions?|duration|allowed)\b", re.I)
SOLUTION = re.compile(r"^\s*(ans(wer)?\s*[.:]|sol(ution)?\s*[.:]|explanation\s*[.:]|hint\s*[.:])", re.I)


def _norm(t):
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", t.strip().lower()))


class ClassicalLayout:
    def __init__(self):
        self.repeated = Counter()
        self.expected_next = None
        self.had_question = False

    def prime(self, lines_by_page, figs_by_page=()):
        """Text that repeats on many pages (running headers, 'Trade Theory' bands,
        footers) is clutter; so are images repeated at the same place (watermark art)."""
        c = Counter()
        for lines in lines_by_page:
            for t in set(_norm(l[4]) for l in lines):
                c[t] += 1
        n = max(1, len(lines_by_page))
        self.repeated = {t for t, k in c.items()
                         if k >= max(3, 0.3 * n) and len(t) < 60 and len(re.sub(r"[^a-z\u0900-\u097f]", "", t)) >= 4}
        fc = Counter()
        for figs in figs_by_page:
            for f in set(tuple(round(v, 2) for v in f) for f in figs):
                fc[f] += 1
        self.repeated_figs = {f for f, k in fc.items() if k >= max(3, 0.3 * n)}

    # ------------------------------------------------------------------ text layer
    def _columns(self, lines):
        bins = np.zeros(400, dtype=int)
        for x0, y0, x1, y1, t, s in lines:
            if x1 - x0 > 0.6:
                continue
            bins[int(x0 * 399):int(x1 * 399) + 1] += 1
        occ = bins > 0
        segs, start = [], None
        for i, o in enumerate(list(occ) + [False]):
            if o and start is None:
                start = i
            elif not o and start is not None:
                segs.append([start / 399, i / 399])
                start = None
        if not segs:
            return [(0.0, 1.0)]
        # merge gaps < 1.5% and narrow segments (table cells such as Sr.No / Marks)
        merged = [segs[0]]
        for s in segs[1:]:
            if s[0] - merged[-1][1] < 0.015:
                merged[-1][1] = s[1]
            else:
                merged.append(s)
        changed = True
        while changed and len(merged) > 1:
            changed = False
            for i, s in enumerate(merged):
                if s[1] - s[0] < 0.15:
                    j = i - 1 if i > 0 and (i == len(merged) - 1 or
                                            s[0] - merged[i - 1][1] < merged[i + 1][0] - s[1]) else i + 1
                    a, b = sorted([i, j])
                    merged[a] = [min(merged[a][0], merged[b][0]), max(merged[a][1], merged[b][1])]
                    del merged[b]
                    changed = True
                    break
        # a real column carries a fair share of the text; thin numeric strips do not
        def mass(seg):
            return sum(len(l[4]) for l in lines if seg[0] <= (l[0] + l[2]) / 2 <= seg[1])
        total = sum(mass(sg) for sg in merged) or 1
        while len(merged) > 1:
            light = [i for i, sg in enumerate(merged) if mass(sg) < 0.12 * total]
            if not light:
                break
            i = light[0]
            j = i - 1 if i > 0 else i + 1
            a, b = sorted([i, j])
            merged[a] = [min(merged[a][0], merged[b][0]), max(merged[a][1], merged[b][1])]
            del merged[b]
        return [(max(0, a - 0.01), min(1, b + 0.01)) for a, b in merged]

    def _is_clutter(self, l):
        x0, y0, x1, y1, t, s = l
        if _norm(t) in self.repeated:
            return True
        m = ANCHOR.match(t)
        if m and self.expected_next is not None and 0 <= int(m.group(1)) - self.expected_next <= 6 and x0 < 0.5:
            return False   # the next question number, not a page number
        if (y0 < 0.05 or y1 > 0.95) and (CLUTTER.search(t) or PAGE_NO.match(t) or len(t) < 40):
            return True
        if MARKS.match(t) and x0 > 0.6:
            return True    # marks / negative-marks cells
        return bool(CLUTTER.search(t)) and len(t) < 40

    def analyse_text_page(self, page):
        lines = page.text_lines()
        if self._looks_like_answer_key(lines):
            return {"page_kind": "answer_key", "dark_background": False, "fragments": [], "_model": "classical-text"}
        figs = [f for f in page.figure_boxes()
                if tuple(round(v, 2) for v in f) not in getattr(self, "repeated_figs", set())]
        content = [l for l in lines if not self._is_clutter(l)]
        cols = self._columns(content)
        frags = []
        for ci, (cx0, cx1) in enumerate(cols):
            col_lines = sorted([l for l in content if cx0 <= (l[0] + l[2]) / 2 <= cx1], key=lambda l: l[1])
            col_figs = [f for f in figs if cx0 <= (f[0] + f[2]) / 2 <= cx1]
            if not col_lines:
                continue
            left = min(l[0] for l in col_lines)
            anchors = []
            for l in col_lines:
                m = ANCHOR.match(l[4])
                if not m or l[0] > left + 0.05 or INSTRUCTION_WORDS.search(l[4]):
                    continue
                n = int(m.group(1))
                if anchors and abs(anchors[-1][0][1] - l[1]) < 0.004:
                    continue  # second number cell on the same row (e.g. QID column)
                exp = self.expected_next
                if exp is None or n == exp or (0 < n - exp <= 3) or n == 1:
                    anchors.append((l, n))
                    self.expected_next = n + 1
            # regions between anchors
            starts = [a[0][1] for a in anchors]
            bounds = starts + [1.01]
            top_lines = [l for l in col_lines if not starts or l[3] <= starts[0] + 0.002]
            if top_lines and (frags or self.had_question):
                y0 = min(l[1] for l in top_lines)
                y1 = max(l[3] for l in top_lines)
                if not any(SOLUTION.match(l[4]) for l in top_lines[:1]):
                    frags.append(self._frag("continuation", None, cx0, y0, cx1, y1, top_lines, col_figs, cont=True))
            for i, (l, n) in enumerate(anchors):
                ys, ye = bounds[i], bounds[i + 1]
                body = [b for b in col_lines if ys - 0.002 <= b[1] < ye - 0.002]
                # stop at an interleaved solution block
                for k, b in enumerate(body):
                    if k and SOLUTION.match(b[4]):
                        body = body[:k]
                        break
                y1 = max(b[3] for b in body)
                for f in col_figs:
                    if ys <= f[1] < ye:
                        y1 = max(y1, f[3])
                frags.append(self._frag("question", str(n), cx0, l[1], cx1, y1, body, col_figs, number=n))
        self.had_question = self.had_question or any(f["role"] == "question" for f in frags)
        if frags and frags[-1]["role"] == "question":
            frags[-1]["continues_on_next"] = True  # unknown: the next page decides
        for l in lines:
            if l not in content:   # bands, headers, footers: boundaries for crop padding
                frags.append({"role": "clutter", "label": None, "number": None, "continues_previous": False,
                              "continues_on_next": False,
                              "box": [int(l[1] * 1000), int(l[0] * 1000), int(np.ceil(l[3] * 1000)), int(np.ceil(l[2] * 1000))],
                              "lang": "en", "has_figure": False, "has_math": False, "has_table": False,
                              "stem": "", "options": [], "confidence": 1.0})
        for i, f in enumerate(frags):
            f["order"] = i
        return {"page_kind": "questions" if frags else "blank_or_ad", "dark_background": False,
                "watermark_present": bool(getattr(self, "repeated_figs", None)) or any(
                    "watermark" in t or "sample" in t for t in self.repeated),
                "fragments": frags, "_model": "classical-text"}

    @staticmethod
    def _looks_like_answer_key(lines):
        if not lines:
            return False
        head = any(ANSWER_KEY.search(l[4]) for l in lines)
        short = sum(1 for l in lines if len(l[4].strip()) <= 4)
        return head and short > 0.6 * len(lines)

    def _frag(self, role, label, x0, y0, x1, y1, lines, figs, number=None, cont=False):
        text = " ".join(l[4] for l in lines)
        hi = bool(re.search(r"[ऀ-ॿ]", text))
        en = bool(re.search(r"[A-Za-z]{3}", text))
        return {"role": role, "label": label, "number": number, "continues_previous": cont,
                "continues_on_next": False,
                "box": [int(max(0, y0 - 0.004) * 1000), int(x0 * 1000), int(min(1, y1 + 0.004) * 1000), int(x1 * 1000)],
                "lang": "bilingual" if hi and en else ("hi" if hi else "en"),
                "has_figure": any(f for f in figs if y0 <= f[1] <= y1), "has_math": False, "has_table": False,
                "stem": text[:400], "options": [], "confidence": 0.6}

    # ------------------------------------------------------------------ image only
    def analyse_image_page(self, img):
        g = np.asarray(img.convert("L"))
        H, W = g.shape
        bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
        bw[: int(0.05 * H)] = 0
        bw[int(0.95 * H):] = 0
        colprof = bw.sum(axis=0) / 255
        occupied = colprof > 0.01 * H
        cols, start = [], None
        gap_min = int(0.015 * W)
        run = 0
        for x, o in enumerate(list(occupied) + [False] * gap_min):
            if o:
                if start is None:
                    start = x
                run = 0
            elif start is not None:
                run += 1
                if run >= gap_min:
                    if x - run - start > 0.12 * W:
                        cols.append((start, x - run))
                    start, run = None, 0
        frags = []
        for cx0, cx1 in cols or [(0, W)]:
            rows = bw[:, cx0:cx1].sum(axis=1) > 2 * 255
            lines, s = [], None
            for y, o in enumerate(list(rows) + [False]):
                if o and s is None:
                    s = y
                elif not o and s is not None:
                    if y - s > 3:
                        lines.append((s, y))
                    s = None
            if not lines:
                continue
            gaps = [lines[i + 1][0] - lines[i][1] for i in range(len(lines) - 1)]
            thr = (np.median(gaps) * 2.2 + 2) if gaps else 1e9
            block = [lines[0]]
            for i, ln in enumerate(lines[1:]):
                if gaps[i] > thr:
                    frags.append(self._img_frag(block, cx0, cx1, W, H))
                    block = []
                block.append(ln)
            frags.append(self._img_frag(block, cx0, cx1, W, H))
        for i, f in enumerate(frags):
            f["order"] = i
        return {"page_kind": "questions" if frags else "blank_or_ad", "dark_background": float(g.mean()) < 100,
                "fragments": frags, "_model": "classical-image"}

    @staticmethod
    def _img_frag(block, cx0, cx1, W, H):
        y0, y1 = block[0][0], block[-1][1]
        return {"role": "question", "label": None, "number": None, "continues_previous": False,
                "continues_on_next": False,
                "box": [int(y0 / H * 1000), int(cx0 / W * 1000), int(np.ceil(y1 / H * 1000)), int(np.ceil(cx1 / W * 1000))],
                "lang": "other", "has_figure": False, "has_math": False, "has_table": False,
                "stem": "", "options": [], "confidence": 0.25}
