"""Editable project: the single source of truth between analysis and the .pptx.

project.json holds pages (where to re-render from) and items (one per question:
label, text, parts = page boxes, flags). The editor changes items; render_item()
turns an item into a clean picture (cached by content hash) and export() builds the
deck. Undo keeps the last 50 project states.
"""
import copy
import hashlib
import json
import os
import shutil
import threading
import time

from PIL import Image

from . import imaging
from .deck import DeckBuilder, rebuild_fits
from .ingest import ImagePage, PdfPage, fitz
from .report import _thumb, write_csv, write_html

UNDO_LIMIT = 50
_locks = {}
_locks_guard = threading.Lock()


def lock_for(path):
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


DEFAULT_SETTINGS = {"layout": "fill", "blank_after": True, "watermark": "remove", "theme": "light"}


class Project:
    def __init__(self, root):
        self.root = root
        self.path = os.path.join(root, "project.json")
        self.lock = lock_for(self.path)
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)
        self.data.setdefault("settings", dict(DEFAULT_SETTINGS))

    # ------------------------------------------------------------------ storage
    @classmethod
    def create(cls, root, pages, items, settings=None, integrity=None, summary=""):
        os.makedirs(root, exist_ok=True)
        data = {"version": 1, "settings": {**DEFAULT_SETTINGS, **(settings or {})}, "pages": pages,
                "items": items, "integrity": integrity or {}, "summary": summary, "undo": []}
        with open(os.path.join(root, "project.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return cls(root)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def checkpoint(self):
        """Call before every edit."""
        snap = {k: copy.deepcopy(self.data[k]) for k in ("items", "settings")}
        self.data.setdefault("undo", []).append(snap)
        del self.data["undo"][:-UNDO_LIMIT]

    def undo(self):
        if not self.data.get("undo"):
            return False
        snap = self.data["undo"].pop()
        self.data.update(snap)
        return True

    @property
    def settings(self):
        return self.data["settings"]

    def item(self, iid):
        for it in self.data["items"]:
            if it["id"] == iid:
                return it
        raise KeyError(iid)

    def new_id(self):
        used = {it["id"] for it in self.data["items"]}
        k = len(used)
        while f"q{k:04d}" in used:
            k += 1
        return f"q{k:04d}"

    def public(self):
        """Project JSON for the browser (no server paths, no undo stack)."""
        pages = [{"seq": p["seq"], "ref": p["ref"], "w": p.get("thumb_w"), "h": p.get("thumb_h")}
                 for p in self.data["pages"]]
        items = []
        for it in self.data["items"]:
            items.append({k: it.get(k) for k in ("id", "label", "number", "lang", "render", "stem", "options",
                                                 "parts", "flags", "confidence", "blank_after", "custom_image",
                                                 "rev", "img_w", "img_h", "display_font_pt")})
        return {"settings": self.settings, "integrity": self.data.get("integrity", {}),
                "summary": self.data.get("summary", ""), "pages": pages, "items": items,
                "can_undo": bool(self.data.get("undo"))}

    # ------------------------------------------------------------------ pages
    def open_page(self, seq):
        p = self.data["pages"][seq]
        if p["kind"] == "image":
            with Image.open(p["src"]) as im:
                return ImagePage(p["src"], im)
        doc = fitz.open(p["src"])
        page = PdfPage(doc, p["src"], p["index"])
        page._doc_owner = doc
        return page

    def page_jpeg(self, seq):
        return self.data["pages"][seq]["thumb"]

    # ------------------------------------------------------------------ rendering
    def _render_key(self, it):
        s = self.settings
        blob = json.dumps([it.get("parts"), it.get("custom_image"), it.get("erase"), s["watermark"],
                           s.get("theme"), it.get("no_compress")], sort_keys=True)
        return hashlib.md5(blob.encode()).hexdigest()[:12]

    def item_png(self, iid):
        it = self.item(iid)
        key = self._render_key(it)
        path = os.path.join(self.root, "items", f"{iid}_{key}.png")
        if not os.path.exists(path):
            img, ppi = self.render_parts(it)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path)
            it["img_w"], it["img_h"], it["ppi"] = img.width, img.height, ppi
            glyph = imaging.text_height_pt(img, ppi)
            it["glyph_pt"] = glyph
        return path

    def render_parts(self, it, dpi=220):
        """Crop every part from its page, clean it (watermark, rules, light theme),
        stack page-split parts with their original alignment, trim margins."""
        s = self.settings
        if it.get("custom_image"):
            img = Image.open(os.path.join(self.root, "items", it["custom_image"])).convert("RGB")
            return img, float(it.get("custom_ppi") or 200)
        imgs, lefts, ppis = [], [], []
        for part in it.get("parts") or []:
            meta = self.data["pages"][part["page"]]
            page = self.open_page(part["page"])
            try:
                box = part["box"]
                img = page.crop(box, dpi)
                ppi = dpi
                if meta["kind"] == "image":
                    ppi = min(dpi, img.width / max(1e-6, (box[2] - box[0]) * page.width_in))
                erase = [e for e in it.get("erase") or [] if e.get("page") == part["page"]]
                if erase:
                    W, H = img.size
                    bw, bh = box[2] - box[0], box[3] - box[1]
                    img = imaging.erase(img, [((e["box"][0] - box[0]) / bw * W, (e["box"][1] - box[1]) / bh * H,
                                               (e["box"][2] - box[0]) / bw * W, (e["box"][3] - box[1]) / bh * H)
                                              for e in erase])
                img = imaging.to_light(img, watermark=s["watermark"], photo=meta["kind"] != "pdf-digital",
                                       screenshot=meta["kind"] == "image")
                img = imaging.strip_rules(img)
                img, rect = imaging.tighten(img)
                imgs.append(img)
                ppis.append(ppi)
                lefts.append(box[0] * meta["w_in"] + rect[0] / ppi)
            finally:
                doc = getattr(page, "_doc_owner", None)
                if doc is not None:
                    doc.close()
        if not imgs:
            return Image.new("RGB", (400, 120), "white"), 200.0
        target = min(ppis)
        scaled = []
        for im, p in zip(imgs, ppis):
            if p > target * 1.05:
                f = target / p
                im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))), Image.LANCZOS)
            scaled.append(im)
        aligned = max(lefts) - min(lefts) < 1.0
        img = imaging.stack(scaled, offsets=[int((l - min(lefts)) * target) for l in lefts] if aligned else None)
        if not it.get("no_compress") and "figure" not in (it.get("flags") or []):
            img = imaging.compress_gaps(img, target)
        img, _ = imaging.tighten(img)
        if s.get("theme") == "dark":
            from PIL import ImageOps
            img = ImageOps.colorize(ImageOps.invert(img.convert("L")), black="#121418", white="#f2f2f2")
        return img, float(target)

    def touch(self, it):
        it["rev"] = int(it.get("rev") or 0) + 1
        self.item_png(it["id"])

    # ------------------------------------------------------------------ export
    def export(self, out_dir, title="questions"):
        s = self.settings
        deck = DeckBuilder(theme=s.get("theme", "light"), layout=s.get("layout", "fill"))
        rows, thumbs = [], []
        for n, it in enumerate(self.data["items"], 1):
            notes = _notes(it)
            if it.get("render") == "text" and (it.get("stem") or "").strip():
                size = rebuild_fits(it.get("label") or "", it["stem"], it.get("options") or [], deck.box)
                if size:
                    deck.add_rebuilt(it.get("label") or "", it["stem"], it.get("options") or [], size, notes)
                    disp = size
                else:
                    disp = self._add_picture(deck, it, notes)
            else:
                disp = self._add_picture(deck, it, notes)
            it["display_font_pt"] = disp
            blank = it.get("blank_after")
            if blank is None:
                blank = s.get("blank_after", True)
            if blank:
                deck.add_blank()
            flags = list(dict.fromkeys(it.get("flags") or []))
            flags = [f for f in flags if f not in ("small_text", "too_small_to_read")]
            if disp is not None and disp < 7:
                flags.append("too_small_to_read")
            elif disp is not None and disp < 12:
                flags.append("small_text")
            it["flags"] = flags
            pages = " + ".join(dict.fromkeys(self.data["pages"][p["page"]]["ref"] for p in it.get("parts") or []))
            rows.append({"slide": n, "label": it.get("label"), "number": it.get("number"), "section": it.get("section", 1),
                         "lang": it.get("lang"), "pages": pages or "(custom)", "render": it.get("render"),
                         "confidence": it.get("confidence"), "display_font_pt": disp, "flags": ";".join(flags),
                         "text": (it.get("stem") or "")[:300]})
            with open(self.item_png(it["id"]), "rb") as f:
                thumbs.append(_thumb(f.read()))
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, title)
        deck.save(base + ".pptx")
        integ = self.data.get("integrity") or {"detected": len(rows), "expected": len(rows), "gaps": [],
                                                "duplicates": [], "out_of_order": [], "sections": [], "ok": True}
        integ = dict(integ, detected=len(rows))
        write_csv(base + "_qa.csv", rows)
        write_html(base + "_qa.html", title, self.data.get("summary", ""), integ, rows, thumbs)
        self.save()
        return base + ".pptx", len(deck.prs.slides)

    def _add_picture(self, deck, it, notes):
        path = self.item_png(it["id"])
        img = Image.open(path)
        ppi = float(it.get("ppi") or 220)
        data, fmt, px = imaging.encode(img, int(deck.box[2] * 220), int(deck.box[3] * 220))
        scale_in = deck.add_snapshot(data, px, notes, src_w_in=img.width / ppi, glyph_pt=it.get("glyph_pt"))
        return scale_in


def _notes(it):
    lines = [f"Q {it.get('label') or '?'}"]
    if it.get("flags"):
        lines.append("flags: " + ", ".join(it["flags"]))
    if it.get("stem"):
        lines.append(it["stem"][:1500])
    for i, o in enumerate(it.get("options") or []):
        lines.append(f"({chr(97 + i)}) {o}")
    return "\n".join(lines)


def copy_into(root, src_path, name):
    os.makedirs(os.path.join(root, "items"), exist_ok=True)
    dst = os.path.join(root, "items", name)
    shutil.copyfile(src_path, dst)
    return name
