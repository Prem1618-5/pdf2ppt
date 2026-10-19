"""Turn uploads into a stream of SourcePage objects, one page at a time.

A page knows how to give (a) a small JPEG for layout analysis and (b) a crop of any
normalised box at whatever resolution the slide needs. PDF pages are re-rendered per
crop (clip rendering), so a whole page is never held at high DPI in memory.
"""
import io
import os

import numpy as np
from PIL import Image, ImageOps

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
MAX_IMAGE_SIDE = 3200


class SourcePage:
    def __init__(self, file_name, index, kind):
        self.file_name = file_name
        self.index = index      # 0-based index within its file
        self.kind = kind        # "pdf-digital" | "pdf-scan" | "image"
        self.width_in = None    # physical size (inches) if known
        self.height_in = None

    @property
    def ref(self):
        return f"{os.path.basename(self.file_name)}#p{self.index + 1}"

    def layout_jpeg(self, long_side):
        img = self.render_full(long_side)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=82)
        return buf.getvalue(), img.size

    def render_full(self, long_side):
        raise NotImplementedError

    def crop(self, box, px_per_in):
        """box = (x0, y0, x1, y1) in 0..1 page fractions."""
        raise NotImplementedError

    def text_lines(self):
        """[(x0, y0, x1, y1, text, size)] in 0..1 fractions; empty if no text layer."""
        return []

    def close(self):
        pass


class PdfPage(SourcePage):
    def __init__(self, doc, file_name, index):
        page = doc[index]
        text = page.get_text("text").strip()
        imgs = page.get_images()
        kind = "pdf-digital" if len(text) > 200 else "pdf-scan"
        super().__init__(file_name, index, kind)
        self.doc, self.page = doc, page
        self.rect = page.rect
        self.width_in = self.rect.width / 72.0
        self.height_in = self.rect.height / 72.0
        self.n_images = len(imgs)

    def render_full(self, long_side):
        z = long_side / max(self.rect.width, self.rect.height)
        pix = self.page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def crop(self, box, px_per_in):
        x0, y0, x1, y1 = box
        r = self.rect
        clip = fitz.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height,
                         r.x0 + x1 * r.width, r.y0 + y1 * r.height)
        z = px_per_in / 72.0
        pix = self.page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def text_lines(self):
        if self.kind != "pdf-digital":
            return []
        W, H = self.rect.width, self.rect.height
        out = []
        for b in self.page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                t = " ".join(s["text"].strip() for s in spans)
                size = max(s["size"] for s in spans)
                out.append((x0 / W, y0 / H, x1 / W, y1 / H, t, size))
        return out


class ImagePage(SourcePage):
    def __init__(self, file_name, img, index=0):
        super().__init__(file_name, index, "image")
        img = ImageOps.exif_transpose(img).convert("RGB")
        if max(img.size) > MAX_IMAGE_SIDE:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
        self.img = img
        # Unknown physical size: assume the page is printed ~8.3 in wide (A4) so that
        # legibility estimates stay meaningful; screenshots are treated the same way.
        self.width_in = 8.3 if img.width < img.height else 11.7
        self.height_in = self.width_in * img.height / img.width

    def render_full(self, long_side):
        im = self.img.copy()
        im.thumbnail((long_side, long_side), Image.LANCZOS)
        return im

    def crop(self, box, px_per_in):
        W, H = self.img.size
        x0, y0, x1, y1 = box
        c = self.img.crop((int(x0 * W), int(y0 * H), int(np.ceil(x1 * W)), int(np.ceil(y1 * H))))
        # Never upscale beyond the source; the slide only needs ~220 dpi.
        want_w = int((x1 - x0) * self.width_in * px_per_in)
        if want_w and c.width > want_w * 1.05:
            c = c.resize((want_w, max(1, int(c.height * want_w / c.width))), Image.LANCZOS)
        return c

    def close(self):
        self.img = None


def iter_pages(paths):
    """Yield SourcePage objects in upload order, opening one file at a time."""
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            doc = fitz.open(path)
            try:
                for i in range(doc.page_count):
                    p = PdfPage(doc, path, i)
                    yield p
                    p.close()
            finally:
                doc.close()
        elif ext in IMAGE_EXT:
            with Image.open(path) as im:
                p = ImagePage(path, im)
            yield p
            p.close()
        else:
            raise ValueError(f"Unsupported file type: {os.path.basename(path)}")


def count_pages(paths):
    n = 0
    for path in paths:
        if path.lower().endswith(".pdf"):
            with fitz.open(path) as d:
                n += d.page_count
        else:
            n += 1
    return n


def dhash(img, size=16):
    g = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    a = np.asarray(g, dtype=np.int16)
    return (a[:, 1:] > a[:, :-1]).flatten()


def _pdf_figure_boxes(self):
    """Raster images + vector drawing clusters on the page (fractions), excluding
    page-sized background/watermark art."""
    W, H = self.rect.width, self.rect.height
    out = []
    try:
        for info in self.page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            if (x1 - x0) * (y1 - y0) < 0.35 * W * H:
                out.append((x0 / W, y0 / H, x1 / W, y1 / H))
    except Exception:
        pass
    return out


PdfPage.figure_boxes = _pdf_figure_boxes
SourcePage.figure_boxes = lambda self: []
