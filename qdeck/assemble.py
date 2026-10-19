"""Fragments -> questions: continuation stitching (column and page breaks), shared
passages, bilingual twins, duplicate pages, section restarts and the integrity check."""
import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Fragment:
    page_seq: int
    page_ref: str
    order: int
    role: str
    label: Optional[str]
    number: Optional[int]
    lang: str
    box: tuple                 # fractions x0,y0,x1,y1 on the page
    crop_path: str
    text: str
    options: list
    confidence: float
    has_figure: bool = False
    has_math: bool = False
    has_table: bool = False
    continues_previous: bool = False
    continues_on_next: bool = False
    dark: bool = False
    watermark: bool = False
    source_kind: str = ""
    model: str = ""


@dataclass
class Question:
    root: Fragment
    parts: List[Fragment] = field(default_factory=list)   # continuations, in order
    passage: Optional[Fragment] = None                     # stitched above (first in group)
    passage_slide_of: Optional[int] = None                 # later questions refer to it
    twins: List[Fragment] = field(default_factory=list)    # other-language versions kept
    flags: List[str] = field(default_factory=list)
    section: int = 0
    slide_no: int = 0

    @property
    def number(self):
        return self.root.number

    @property
    def label(self):
        return self.root.label or (str(self.root.number) if self.root.number is not None else "?")

    @property
    def lang(self):
        return self.root.lang

    @property
    def fragments(self):
        return ([self.passage] if self.passage else []) + [self.root] + self.parts + self.twins

    @property
    def confidence(self):
        return min(f.confidence for f in [self.root] + self.parts)

    @property
    def text(self):
        return " ".join(f.text for f in [self.root] + self.parts if f.text).strip()

    @property
    def options(self):
        out = []
        for f in [self.root] + self.parts:
            out += f.options or []
        return out

    def has(self, attr):
        return any(getattr(f, attr) for f in [self.root] + self.parts)


def _norm(t):
    return re.sub(r"[^\wऀ-ॿ]+", " ", (t or "").lower()).strip()


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a[:400], b[:400]).ratio() > 0.75


def assemble(frags: List[Fragment], lang_pref="en", log=print):
    frags = sorted(frags, key=lambda f: (f.page_seq, f.order))
    questions: List[Question] = []
    dropped = {"solutions": 0, "clutter": 0, "duplicates": 0, "twins": 0, "orphans": 0}
    pending_passage = None
    passage_group_open = False

    for f in frags:
        if f.role in ("solution", "answer_key"):
            dropped["solutions"] += 1
            continue
        if f.role in ("clutter",):
            dropped["clutter"] += 1
            continue
        if f.role in ("instructions", "section_header"):
            passage_group_open = False
            pending_passage = None
            continue
        if f.role == "shared_passage":
            pending_passage = f
            passage_group_open = True
            continue
        is_cont = f.role == "continuation" or (f.role == "question" and f.number is None and f.continues_previous)
        if is_cont:
            target = None
            # prefer an open question of the same language (bilingual spreads interleave)
            for q in reversed(questions[-8:]):
                last = q.parts[-1] if q.parts else q.root
                if last.continues_on_next and q.lang == f.lang:
                    target = q
                    break
            if target is None:
                for q in reversed(questions[-8:]):
                    if q.lang == f.lang or f.lang in ("other", "bilingual") or q.lang == "bilingual":
                        target = q
                        break
            if target is None and questions:
                target = questions[-1]
            if target is None:
                dropped["orphans"] += 1
                continue
            target.parts.append(f)
            if "split_stitched" not in target.flags:
                target.flags.append("split_stitched")
            if f.page_seq != target.root.page_seq:
                if "page_break_stitched" not in target.flags:
                    target.flags.append("page_break_stitched")
            continue
        if f.role != "question":
            continue
        q = Question(root=f)
        if pending_passage is not None:
            q.passage = pending_passage
            q.flags.append("shared_passage")
            pending_passage = None
        elif passage_group_open:
            # later member of a passage group: refer to the slide that shows the passage
            for prev in reversed(questions):
                if prev.passage is not None:
                    q.passage_slide_of = id(prev)
                    q.flags.append("refers_to_passage")
                    break
        questions.append(q)

    # ---- bilingual twins: same number printed once in Hindi and once in English ----
    by_num = {}
    for q in questions:
        if q.number is not None:
            by_num.setdefault(q.number, []).append(q)
    keep = []
    removed = set()
    if lang_pref in ("en", "hi", "both"):
        for n, qs in by_num.items():
            langs = {q.lang for q in qs}
            if "en" in langs and "hi" in langs:
                pref = "hi" if lang_pref == "hi" else "en"
                main = next(q for q in qs if q.lang == pref)
                for q in qs:
                    if q is not main and q.lang in ("en", "hi") and q.lang != pref:
                        removed.add(id(q))
                        dropped["twins"] += 1
                        if lang_pref == "both":
                            main.twins.append(q.root)
                            main.twins += q.parts
                if "bilingual_twin" not in main.flags:
                    main.flags.append("bilingual_twin")
    questions = [q for q in questions if id(q) not in removed]

    # ---- duplicate pages / repeated photos: same number + same text --------------------
    for q in questions:
        dup = False
        for k in keep[-60:]:
            if k.number == q.number and k.number is not None and k.lang == q.lang and _similar(k.text, q.text):
                dup = True
                break
        if dup:
            dropped["duplicates"] += 1
            continue
        keep.append(q)
    questions = keep

    # ---- sections (numbering restarts) + integrity -------------------------------------
    section, prev = 0, None
    for q in questions:
        n = q.number
        if n is not None and prev is not None and n < prev - 2:
            section += 1
        q.section = section
        if n is not None:
            prev = n
    for i, q in enumerate(questions, 1):
        q.slide_no = i
        if q.number is None:
            q.flags.append("no_number")
        if q.confidence < 0.6:
            q.flags.append("low_confidence")
    ids = {id(q): q for q in questions}
    for q in questions:
        if q.passage_slide_of in ids:
            q.passage_slide_of = ids[q.passage_slide_of].slide_no
        elif q.passage_slide_of is not None:
            q.passage_slide_of = None

    integrity = integrity_check(questions)
    return questions, dropped, integrity


def integrity_check(questions):
    secs = {}
    for q in questions:
        secs.setdefault(q.section, []).append(q)
    out = {"sections": [], "detected": len(questions), "expected": 0,
           "gaps": [], "duplicates": [], "out_of_order": []}
    for s, qs in sorted(secs.items()):
        nums = [q.number for q in qs if q.number is not None]
        if not nums:
            out["expected"] += len(qs)
            out["sections"].append({"section": s + 1, "first": None, "last": None, "count": len(qs)})
            continue
        lo, hi = min(nums), max(nums)
        expected = hi - lo + 1 + sum(1 for q in qs if q.number is None)
        out["expected"] += expected
        seen = set()
        for n in nums:
            if n in seen:
                out["duplicates"].append(f"S{s + 1}:{n}")
            seen.add(n)
        out["gaps"] += [f"S{s + 1}:{n}" for n in range(lo, hi + 1) if n not in seen]
        for a, b in zip(nums, nums[1:]):
            if b != a + 1 and b not in seen - {b}:
                if b < a:
                    out["out_of_order"].append(f"S{s + 1}:{a}->{b}")
        out["sections"].append({"section": s + 1, "first": lo, "last": hi, "count": len(qs)})
    out["ok"] = not out["gaps"] and not out["duplicates"] and out["detected"] == out["expected"]
    return out
