"""Watermark removal.

Born-digital PDFs: watermarks are almost always objects stamped identically on every
page (Acrobat "Add watermark" makes a Form XObject; others place the same image or
rotated text on each page). Anything repeated on most pages that sits *behind or across*
the content is emptied in a cleaned copy of the PDF, so the question text underneath
stays pixel-perfect. Scans/photos/screenshots are cleaned in imaging.to_light (pale or
tinted pixels -> white).
"""
import collections
import hashlib

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz


def _h(b):
    return hashlib.md5(b or b"").hexdigest()


def strip_pdf_watermarks(src, dst, log=print):
    """Write a cleaned copy of `src` to `dst`. Returns the number of objects removed."""
    doc = fitz.open(src)
    n = doc.page_count
    if n == 0:
        doc.save(dst)
        return 0
    need = max(2, int(0.5 * n + 0.5)) if n > 2 else n

    form_pages = collections.defaultdict(set)    # stream hash -> pages
    form_xrefs = collections.defaultdict(set)
    img_pages = collections.defaultdict(set)     # (w, h, stream hash) -> pages
    img_xrefs = collections.defaultdict(list)
    for pno in range(n):
        page = doc[pno]
        for xref, name, *_ in page.get_xobjects():
            try:
                stream = doc.xref_stream(xref)
            except Exception:
                continue
            k = _h(stream)
            form_pages[k].add(pno)
            form_xrefs[k].add(xref)
        area = page.rect.width * page.rect.height
        for im in page.get_images(full=True):
            xref, w, h = im[0], im[2], im[3]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            if not rects:
                continue
            big = max(r.width * r.height for r in rects) > 0.04 * area
            if not big:
                continue
            try:
                k = (w, h, _h(doc.xref_stream_raw(xref)))
            except Exception:
                k = (w, h, "")
            img_pages[k].add(pno)
            img_xrefs[k].append((pno, xref))

    removed = 0
    blank = b"q Q"
    for k, pages in form_pages.items():
        if len(pages) >= need:
            for xref in form_xrefs[k]:
                doc.update_stream(xref, blank)
                removed += 1
    for k, pages in img_pages.items():
        if len(pages) >= need:
            for pno, xref in img_xrefs[k]:
                try:
                    doc[pno].delete_image(xref)
                    removed += 1
                except Exception:
                    pass
    # explicit watermark annotations
    for page in doc:
        for a in list(page.annots() or []):
            if a.type[1] == "Watermark":
                page.delete_annot(a)
                removed += 1
    # (rotated text drawn straight into the page content is left to the raster cleanup:
    #  redacting it could also delete real text lying under its bounding box)
    doc.save(dst, garbage=1, deflate=True)
    doc.close()
    if removed:
        log(f"removed {removed} watermark object(s) from {src.rsplit('/', 1)[-1]}")
    return removed
