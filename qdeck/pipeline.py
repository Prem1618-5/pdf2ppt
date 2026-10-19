"""End-to-end job: pages -> layout (Gemini, classical fallback) -> crops -> questions
-> pptx + QA report + manifest. Streams one page at a time; peak RAM stays small."""
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from . import imaging
from .assemble import Fragment, assemble, reading_order
from .ingest import count_pages, dhash, iter_pages
from .layout_classical import ClassicalLayout
from .layout_gemini import GeminiLayout, QuotaExhausted
from .project import Project
from .refine import refine
from .watermark import strip_pdf_watermarks

KEEP_ROLES = ("question", "continuation", "shared_passage")
MIN_FONT_PT = 10      # below this on the slide the question is flagged "small_text"
ILLEGIBLE_PT = 7      # below this it is flagged "too_small_to_read"


def _box(b):
    y0, x0, y1, x1 = [max(0, min(1000, int(v))) / 1000 for v in (list(b) + [0, 0, 1000, 1000])[:4]]
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def run_job(paths, out_dir, settings, options=None, progress=lambda *a: None, log=print, keys=None,
            on_pool=None):
    """Analyse the inputs into an editable project (out_dir/project.json) and export
    the first version of the deck + QA report from it."""
    t0 = time.time()
    opts = settings.job_options()
    opts.update(options or {})
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "work")
    pages_dir = os.path.join(out_dir, "pages")
    os.makedirs(work, exist_ok=True)
    os.makedirs(pages_dir, exist_ok=True)
    # watermark removal at the source: cleaned PDF copies are used for everything
    srcs = []
    for i, path in enumerate(paths):
        if path.lower().endswith(".pdf") and opts["watermark"] == "remove":
            clean = os.path.join(out_dir, "src", f"{i:02d}_{os.path.basename(path)}")
            os.makedirs(os.path.dirname(clean), exist_ok=True)
            try:
                strip_pdf_watermarks(path, clean, log=log)
                srcs.append(clean)
                continue
            except Exception as e:
                log(f"watermark strip skipped for {os.path.basename(path)}: {type(e).__name__}")
        srcs.append(path)
    paths = srcs
    n_pages = count_pages(paths)
    progress("pages", 0, n_pages)

    # ---- pass 1: small layout images, duplicate-page detection, text layers -----------
    pages = []
    hashes = []
    for seq, page in enumerate(iter_pages(paths)):
        jpeg, size = page.layout_jpeg(settings.layout_px)
        jp = os.path.join(pages_dir, f"{seq:04d}.jpg")
        with open(jp, "wb") as f:
            f.write(jpeg)
        h = dhash(Image.open(jp))
        dup_of = next((i for i, hh in enumerate(hashes) if np.count_nonzero(h != hh) <= 12), None)
        hashes.append(h)
        pages.append({"seq": seq, "ref": page.ref, "kind": page.kind, "jpeg": jp, "dup_of": dup_of,
                      "lines": page.text_lines(), "figs": page.figure_boxes(), "src": page.file_name,
                      "index": page.index, "w_in": page.width_in, "h_in": page.height_in,
                      "native_ppi": page.native_ppi,
                      "thumb_w": size[0], "thumb_h": size[1]})
        progress("rendered", seq + 1, n_pages)
    classical = ClassicalLayout()
    classical.prime([p["lines"] for p in pages], [p["figs"] for p in pages])

    # ---- pass 2a: classical first for born-digital files ------------------------------
    layouts = [None] * len(pages)
    if settings.classical_first:
        _classical_digital(paths, pages, layouts, classical, log)
        classical = ClassicalLayout()          # fresh state for any later fallback use
        classical.prime([p["lines"] for p in pages], [p["figs"] for p in pages])

    # ---- pass 2b: vision-LLM layout for the rest (batched, parallel, cached) ----------
    llm = GeminiLayout(settings, log=log, keys=keys) if settings.use_llm else None
    if llm is not None and on_pool:
        on_pool(llm.pool)
    todo = [p for p in pages if layouts[p["seq"]] is None and p["dup_of"] is None]
    for p in pages:
        if p["dup_of"] is not None:
            layouts[p["seq"]] = {"page_kind": "duplicate", "dark_background": False, "fragments": [],
                                 "_model": "skip"}
    done = [len(pages) - len(todo)]
    progress("analysed", done[0], n_pages)
    llm_failed = [False]
    batches = [todo[i:i + max(1, settings.pages_per_call)] for i in range(0, len(todo), max(1, settings.pages_per_call))]

    def run_batch(batch):
        if llm is not None and not llm_failed[0]:
            try:
                jpegs = []
                for p in batch:
                    with open(p["jpeg"], "rb") as f:
                        jpegs.append(f.read())
                res = llm.analyse_batch(jpegs)
                for p, r in zip(batch, res):
                    layouts[p["seq"]] = r
            except QuotaExhausted as e:
                log(f"LLM unavailable ({e}); classical layout for the remaining pages")
                llm_failed[0] = True
        done[0] += len(batch)
        progress("analysed", done[0], n_pages)

    if batches:
        workers = max(1, min(len(batches), llm.pool.parallelism if llm else 1, max(settings.gemini_workers, 1) * 4))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(run_batch, batches))

    # ---- pass 3: crops (re-open pages one at a time) ----------------------------------
    frags = []
    page_notes = []
    for seq, page in enumerate(iter_pages(paths)):
        p = pages[seq]
        lay = layouts[seq]
        if lay is None:
            p["fallback"] = True
            if page.kind == "pdf-digital":
                lay = classical.analyse_text_page(page)
            else:
                lay = classical.analyse_image_page(page.render_full(1600))
        p["model"] = lay.get("_model")
        page_notes.append({"page": p["ref"], "kind": lay.get("page_kind"), "model": lay.get("_model"),
                           "duplicate_of": pages[p["dup_of"]]["ref"] if p["dup_of"] is not None else None})
        raw = [f for f in lay.get("fragments", []) if _box(f.get("box", [])) is not None]
        # geometric reading order for content; clutter/headers keep their own position
        content = [f for f in raw if f.get("role") in KEEP_ROLES + ("solution", "answer_key")]
        for k, f in enumerate(reading_order(content, key=lambda f: _box(f["box"]))):
            f["order"] = k
        raw = content + [f for f in raw if f not in content]
        boxes = [_box(f.get("box", [])) for f in raw]
        if not str(lay.get("_model", "")).startswith("classical"):
            boxes = refine(Image.open(p["jpeg"]), raw, boxes)
        for i, (f, box) in enumerate(zip(raw, boxes)):
            if box is None or f.get("role") not in KEEP_ROLES:
                if box is not None and f.get("role") in ("solution", "answer_key", "clutter",
                                                         "instructions", "section_header"):
                    frags.append(_frag(p, f, box, "", 0, lay))
                continue
            neighbours = [b for j, b in enumerate(boxes) if j != i and b is not None]
            pb = imaging.padded_box(box, neighbours)
            img = page.crop(pb, settings.crop_dpi)
            pw, ph = pb[2] - pb[0], pb[3] - pb[1]
            core = ((box[0] - pb[0]) / pw * img.width, (box[1] - pb[1]) / ph * img.height,
                    (box[2] - pb[0]) / pw * img.width, (box[3] - pb[1]) / ph * img.height)
            W0, H0 = img.size
            snapped, r1 = imaging.snap(img, core)
            _, r2 = imaging.tighten(snapped)
            l, t = r1[0] + r2[0], r1[1] + r2[1]
            r, b = r1[0] + r2[2], r1[1] + r2[3]
            eff = (pb[0] + l / W0 * pw, pb[1] + t / H0 * ph, pb[0] + r / W0 * pw, pb[1] + b / H0 * ph)
            dark = bool(lay.get("dark_background")) or imaging.is_dark(img)
            fr = _frag(p, f, eff, "", i, lay)
            if "lite" in str(lay.get("_model", "")):
                fr.confidence = min(fr.confidence, 0.5)   # lite models give rough boxes: review
            fr.dark = dark
            fr.watermark = bool(lay.get("watermark_present"))
            fr._erase = f.get("erase") or []
            frags.append(fr)
        progress("cropped", seq + 1, n_pages)

    # ---- assemble ---------------------------------------------------------------------
    questions, dropped, integrity = assemble(frags, lang_pref=opts["lang"], log=log)
    progress("building", 0, len(questions))

    # ---- project (editable) ------------------------------------------------------------
    items = []
    for q in questions:
        parts = [{"page": f.page_seq, "box": [round(v, 5) for v in f.box]} for f in q.fragments]
        erase = [{"page": f.page_seq, "box": e} for f in q.fragments for e in getattr(f, "_erase", [])]
        flags = list(q.flags)
        if q.has("has_figure"):
            flags.append("figure")
        plain = not (q.has("has_figure") or q.has("has_math") or q.has("has_table")) and q.passage is None
        stem = _stem(q)
        render = "snapshot"
        if stem and plain and (opts["render"] == "rebuild" or (opts["render"] == "auto" and q.root.dark)):
            render = "text"
        items.append({"id": f"q{q.slide_no:04d}", "label": q.label, "number": q.number, "lang": q.lang,
                      "section": q.section + 1, "render": render, "stem": stem, "options": list(q.options),
                      "parts": parts, "erase": erase, "flags": list(dict.fromkeys(flags)),
                      "confidence": round(q.confidence, 2), "blank_after": None, "custom_image": False, "rev": 1})
    summary = (f"{len(questions)} questions from {n_pages} pages in {time.time() - t0:.0f}s. "
               f"Excluded: {dropped['solutions']} solution/answer blocks, {dropped['twins']} other-language twins, "
               f"{dropped['duplicates']} duplicates, {dropped['orphans']} orphan fragments. "
               f"Layout: {llm.calls if llm else 0} LLM calls, {llm.cache_hits if llm else 0} cache hits, "
               f"{sum(1 for p in pages if (p.get('model') or '').startswith('classical'))} pages via classical parser "
               f"({sum(1 for p in pages if p.get('fallback'))} as quota fallback).")
    page_meta = [{"seq": p["seq"], "ref": p["ref"], "kind": p["kind"], "src": os.path.abspath(p["src"]),
                  "index": p["index"], "w_in": p["w_in"], "h_in": p["h_in"], "thumb": os.path.abspath(p["jpeg"]),
                  "native_ppi": p.get("native_ppi"),
                  "thumb_w": p["thumb_w"], "thumb_h": p["thumb_h"]} for p in pages]
    settings_p = {"dpi": settings.crop_dpi, "layout": opts.get("layout", "fill"), "blank_after": opts.get("blank_after", True),
                  "watermark": opts["watermark"], "theme": opts.get("theme", "light")}
    proj = Project.create(out_dir, page_meta, items, settings_p, integrity, summary)
    for k, it in enumerate(proj.data["items"], 1):
        proj.item_png(it["id"])
        progress("building", k, len(questions))
    pptx, n_slides = proj.export(out_dir)
    shutil.rmtree(work, ignore_errors=True)
    log(summary)
    base = os.path.join(out_dir, "questions")
    return {"pptx": pptx, "qa_html": base + "_qa.html", "qa_csv": base + "_qa.csv",
            "project": os.path.join(out_dir, "project.json"), "integrity": integrity, "summary": summary,
            "slides": len(questions), "pptx_slides": n_slides}


def _classical_digital(paths, pages, layouts, classical, log):
    """Run the CPU parser on born-digital files; keep its result only when the
    numbering integrity is perfect (no gaps / duplicates, every page accounted for)."""
    from .assemble import integrity_check, Question
    seq = 0
    for path in paths:
        file_pages = []
        for page in iter_pages([path]):
            file_pages.append((seq, page.kind,
                               classical.analyse_text_page(page) if page.kind == "pdf-digital" else None))
            seq += 1
        if not file_pages or any(k != "pdf-digital" for _, k, _ in file_pages):
            continue
        nums = [f.get("number") for _, _, lay in file_pages for f in lay["fragments"] if f["role"] == "question"]
        ok = (len(nums) >= 3 and None not in nums and nums == list(range(nums[0], nums[0] + len(nums))))
        if ok:
            for s_, _, lay in file_pages:
                if pages[s_]["dup_of"] is None:
                    layouts[s_] = lay
            log(f"{os.path.basename(path)}: text layer parsed without LLM ({len(nums)} questions, numbering complete)")
        else:
            log(f"{os.path.basename(path)}: classical parse incomplete; using the vision LLM")


def _script_lang(text, fallback):
    """Language from the transcribed script, not the model's label (lite models call a
    Hindi block 'bilingual' when it contains a few Latin symbols)."""
    dev = sum(1 for c in text if "\u0900" <= c <= "\u097f")
    lat = sum(1 for c in text if c.isascii() and c.isalpha())
    if dev + lat < 12:
        return fallback
    r = dev / (dev + lat)
    return "hi" if r > 0.6 else ("en" if r < 0.1 else "bilingual")


def _frag(p, f, box, path, i, lay):
    text = (f.get("stem") or f.get("text") or "").strip()
    f["lang"] = _script_lang(text + " " + " ".join(f.get("options") or []), f.get("lang", "en"))
    return Fragment(page_seq=p["seq"], page_ref=p["ref"], order=f.get("order", i), role=f.get("role", "question"),
                    label=f.get("label"), number=f.get("number"), lang=f.get("lang", "en"), box=box,
                    crop_path=path, text=text, options=[o for o in (f.get("options") or []) if o],
                    confidence=float(f.get("confidence", 0.5)), has_figure=bool(f.get("has_figure")),
                    has_math=bool(f.get("has_math")), has_table=bool(f.get("has_table")),
                    continues_previous=bool(f.get("continues_previous")),
                    continues_on_next=bool(f.get("continues_on_next")), source_kind=p["kind"],
                    model=lay.get("_model", ""))


OPT = __import__("re").compile(r"^\s*[\(\[]?([a-dA-D1-4])[\)\].]\s+(.*)$")


def _stem(q):
    """Question text without duplicated lines; option lines the LLM left inside the
    stem are moved to q.options (when it returned none)."""
    lines, seen = [], set()
    for f in [q.root] + q.parts:
        for ln in (f.text or "").splitlines():
            k = " ".join(ln.split()).lower()
            if k and k not in seen:
                seen.add(k)
                lines.append(ln.strip())
    if not q.options:
        stem, opts = [], []
        for ln in lines:
            m = OPT.match(ln)
            if m and len(opts) < 4:
                opts.append(m.group(2))
            elif opts:
                opts[-1] += " " + ln
            else:
                stem.append(ln)
        if len(opts) >= 2:
            q.root.options = opts
            return " ".join(stem)
    return " ".join(lines)
