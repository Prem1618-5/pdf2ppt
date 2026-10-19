"""End-to-end job: pages -> layout (Gemini, classical fallback) -> crops -> questions
-> pptx + QA report + manifest. Streams one page at a time; peak RAM stays small."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from . import imaging
from .assemble import Fragment, assemble
from .deck import Q1, DeckBuilder, rebuild_fits
from .ingest import count_pages, dhash, iter_pages
from .layout_classical import ClassicalLayout
from .layout_gemini import GeminiLayout, QuotaExhausted
from .report import _thumb, write_csv, write_html

KEEP_ROLES = ("question", "continuation", "shared_passage")
MIN_FONT_PT = 10      # below this on the slide the question is flagged "small_text"
ILLEGIBLE_PT = 7      # below this it is flagged "too_small_to_read"


def _box(b):
    y0, x0, y1, x1 = [max(0, min(1000, int(v))) / 1000 for v in (list(b) + [0, 0, 1000, 1000])[:4]]
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def run_job(paths, out_dir, settings, options=None, progress=lambda *a: None, log=print):
    t0 = time.time()
    opts = settings.job_options()
    opts.update(options or {})
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "work")
    os.makedirs(work, exist_ok=True)
    n_pages = count_pages(paths)
    progress("pages", 0, n_pages)

    # ---- pass 1: small layout images, duplicate-page detection, text layers -----------
    pages = []
    hashes = []
    for seq, page in enumerate(iter_pages(paths)):
        jpeg, size = page.layout_jpeg(settings.layout_px)
        jp = os.path.join(work, f"layout_{seq:04d}.jpg")
        with open(jp, "wb") as f:
            f.write(jpeg)
        h = dhash(Image.open(jp))
        dup_of = next((i for i, hh in enumerate(hashes) if np.count_nonzero(h != hh) <= 12), None)
        hashes.append(h)
        pages.append({"seq": seq, "ref": page.ref, "kind": page.kind, "jpeg": jp, "dup_of": dup_of,
                      "lines": page.text_lines(), "figs": page.figure_boxes()})
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
    llm = GeminiLayout(settings, log=log) if settings.use_llm else None
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
        with ThreadPoolExecutor(max_workers=max(1, settings.gemini_workers)) as ex:
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
        raw = sorted(lay.get("fragments", []), key=lambda f: f.get("order", 0))
        boxes = [_box(f.get("box", [])) for f in raw]
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
            img = imaging.tighten(imaging.snap(img, core))
            dark = bool(lay.get("dark_background")) or imaging.is_dark(img)
            img = imaging.to_light(img, watermark=opts["watermark"], photo=page.kind != "pdf-digital")
            cp = os.path.join(work, f"frag_{seq:04d}_{i:03d}.png")
            img.save(cp)
            fr = _frag(p, f, box, cp, i, lay)
            fr.dark = dark
            fr.watermark = bool(lay.get("watermark_present"))
            frags.append(fr)
            fr._ppi = _ppi(page, pb, settings.crop_dpi)
        progress("cropped", seq + 1, n_pages)

    # ---- assemble ---------------------------------------------------------------------
    questions, dropped, integrity = assemble(frags, lang_pref=opts["lang"], log=log)
    progress("building", 0, len(questions))

    # ---- deck -------------------------------------------------------------------------
    deck = DeckBuilder(theme=opts["theme"], axes=opts["axes"])
    rows, thumbs, manifest = [], [], []
    max_w_px = int(Q1[2] * settings.crop_dpi)
    max_h_px = int(Q1[3] * settings.crop_dpi)
    for q in questions:
        parts = q.fragments
        imgs = []
        ppis = [getattr(f, "_ppi", settings.crop_dpi) for f in parts]
        target_ppi = min(ppis)
        for f, ppi in zip(parts, ppis):
            im = Image.open(f.crop_path)
            if ppi > target_ppi * 1.05:
                s = target_ppi / ppi
                im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
            imgs.append(im)
        img = imaging.stack(imgs)
        if not (q.has("has_figure") or q.has("has_table") or q.passage):
            img = imaging.compress_gaps(img, target_ppi)
        if opts["theme"] == "dark":
            from PIL import ImageOps
            img = ImageOps.invert(img.convert("RGB"))
        scale = min(Q1[2] / (img.width / target_ppi), Q1[3] / (img.height / target_ppi))
        glyph = imaging.text_height_pt(img, target_ppi)
        disp_pt = round(glyph / 0.52 * scale, 1) if glyph else None
        if q.has("has_figure"):
            q.flags.append("figure")
        if any(f.watermark for f in parts):
            q.flags.append("watermark_" + ("kept" if opts["watermark"] == "keep" else "removed"))
        render = "snapshot"
        plain = not (q.has("has_figure") or q.has("has_math") or q.has("has_table")) and q.passage is None
        want_rebuild = opts["render"] == "rebuild" or (opts["render"] == "auto" and q.root.dark)
        size = rebuild_fits(q.label, _stem(q), q.options) if (want_rebuild and plain and q.text) else None
        notes = _notes(q, disp_pt)
        if size:
            deck.add_rebuilt(q.label, _stem(q), q.options, size, notes)
            render = "rebuild"
            disp_pt = size
            data, fmt, px = imaging.encode(img, 400, 300)
        else:
            data, fmt, px = imaging.encode(img, max_w_px, max_h_px)
            deck.add_snapshot(data, px, notes)
            if disp_pt is not None and disp_pt < ILLEGIBLE_PT:
                q.flags.append("too_small_to_read")
            elif disp_pt is not None and disp_pt < MIN_FONT_PT:
                q.flags.append("small_text")
        if q.passage_slide_of:
            deck.add_caption(f"Passage: see slide {q.passage_slide_of}")
        pages_used = list(dict.fromkeys(f.page_ref for f in [q.root] + q.parts))
        row = {"slide": q.slide_no, "label": q.label, "number": q.number, "section": q.section + 1,
               "lang": q.lang, "pages": " + ".join(pages_used), "render": render,
               "confidence": round(q.confidence, 2), "display_font_pt": disp_pt,
               "flags": ";".join(dict.fromkeys(q.flags)), "text": q.text[:300]}
        rows.append(row)
        thumbs.append(_thumb(data))
        manifest.append({**row, "fragments": [{"page": f.page_ref, "role": f.role, "box": f.box,
                                               "crop": os.path.basename(f.crop_path)} for f in parts],
                         "stem": _stem(q), "options": q.options})
        progress("building", q.slide_no, len(questions))

    base = os.path.join(out_dir, "questions")
    deck.save(base + ".pptx")
    summary = (f"{len(questions)} slides from {n_pages} pages in {time.time() - t0:.0f}s. "
               f"Excluded: {dropped['solutions']} solution/answer blocks, {dropped['twins']} other-language twins, "
               f"{dropped['duplicates']} duplicates (repeated pages/photos), {dropped['orphans']} orphan fragments. "
               f"Layout: {llm.calls if llm else 0} LLM calls, {llm.cache_hits if llm else 0} cache hits, "
               f"{sum(1 for p in pages if (p.get('model') or '').startswith('classical'))} pages via classical parser "
               f"({sum(1 for p in pages if p.get('fallback'))} as quota fallback). "
               f"Options: {json.dumps(opts)}")
    write_csv(base + "_qa.csv", rows)
    write_html(base + "_qa.html", ", ".join(os.path.basename(p) for p in paths), summary, integrity, rows, thumbs)
    with open(base + "_manifest.json", "w", encoding="utf-8") as f:
        json.dump({"options": opts, "integrity": integrity, "pages": page_notes, "questions": manifest},
                  f, ensure_ascii=False, indent=1)
    log(summary)
    return {"pptx": base + ".pptx", "qa_html": base + "_qa.html", "qa_csv": base + "_qa.csv",
            "manifest": base + "_manifest.json", "integrity": integrity, "summary": summary,
            "slides": len(questions)}


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


def _ppi(page, pb, crop_dpi):
    if page.kind == "image":
        src_ppi = page.img.width / page.width_in if page.img is not None else crop_dpi
        return min(crop_dpi, src_ppi)
    return crop_dpi


def _frag(p, f, box, path, i, lay):
    text = (f.get("stem") or f.get("text") or "").strip()
    return Fragment(page_seq=p["seq"], page_ref=p["ref"], order=f.get("order", i), role=f.get("role", "question"),
                    label=f.get("label"), number=f.get("number"), lang=f.get("lang", "en"), box=box,
                    crop_path=path, text=text, options=[o for o in (f.get("options") or []) if o],
                    confidence=float(f.get("confidence", 0.5)), has_figure=bool(f.get("has_figure")),
                    has_math=bool(f.get("has_math")), has_table=bool(f.get("has_table")),
                    continues_previous=bool(f.get("continues_previous")),
                    continues_on_next=bool(f.get("continues_on_next")), source_kind=p["kind"],
                    model=lay.get("_model", ""))


def _stem(q):
    return " ".join(f.text for f in [q.root] + q.parts if f.text).strip()


def _notes(q, disp_pt):
    pages = ", ".join(dict.fromkeys(f.page_ref for f in [q.root] + q.parts))
    lines = [f"Q {q.label} | source: {pages} | confidence {q.confidence:.2f}"
             + (f" | est. font on slide {disp_pt} pt" if disp_pt else "")]
    if q.flags:
        lines.append("flags: " + ", ".join(dict.fromkeys(q.flags)))
    if q.text:
        lines.append(q.text[:1500])
    for i, o in enumerate(q.options):
        lines.append(f"({chr(97 + i)}) {o}")
    return "\n".join(lines)
