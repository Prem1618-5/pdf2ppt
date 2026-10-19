"""CPU-side image work: padded crop + ink snap, light-theme normalisation, watermark
keep/remove, dark-frame inversion, legibility estimate and compact encoding."""
import io

import cv2
import numpy as np
from PIL import Image

PAD_Y, PAD_X = 0.018, 0.012


def padded_box(box, neighbours):
    """Grow `box` (fractions x0,y0,x1,y1) by a margin, but never past the midpoint of
    the gap to a neighbouring fragment that shares the same rows/columns."""
    x0, y0, x1, y1 = box
    lo_y, hi_y, lo_x, hi_x = y0 - PAD_Y, y1 + PAD_Y, x0 - PAD_X, x1 + PAD_X
    for nx0, ny0, nx1, ny1 in neighbours:
        h_overlap = min(x1, nx1) - max(x0, nx0) > 0.2 * min(x1 - x0, nx1 - nx0)
        v_overlap = min(y1, ny1) - max(y0, ny0) > 0.2 * min(y1 - y0, ny1 - ny0)
        if h_overlap:
            if ny1 <= y0 + 0.004:
                lo_y = max(lo_y, (ny1 + y0) / 2)
            if ny0 >= y1 - 0.004:
                hi_y = min(hi_y, (y1 + ny0) / 2)
        if v_overlap:
            if nx1 <= x0 + 0.004:
                lo_x = max(lo_x, (nx1 + x0) / 2)
            if nx0 >= x1 - 0.004:
                hi_x = min(hi_x, (x1 + nx0) / 2)
    return (max(0.0, min(lo_x, x0)), max(0.0, min(lo_y, y0)),
            min(1.0, max(hi_x, x1)), min(1.0, max(hi_y, y1)))


def ink_mask(img):
    g = np.asarray(img.convert("L"))
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 18)
    return bw > 0


def snap(img, core):
    """`core` = pixel rect (x0,y0,x1,y1) of the LLM box inside the padded crop. Keep the
    core, extend into the padding only through rows/columns that touch the core with ink
    (completes clipped glyphs), stop at the first blank line, then trim blank margins."""
    m = ink_mask(img)
    H, W = m.shape
    cx0, cy0, cx1, cy1 = [int(v) for v in core]
    rows = m[:, max(0, cx0):max(cx0 + 1, cx1)].sum(axis=1) > 1
    cols = m[max(0, cy0):max(cy0 + 1, cy1), :].sum(axis=0) > 1
    top = cy0
    while top > 0 and rows[top - 1]:
        top -= 1
    bot = cy1
    while bot < H and rows[bot]:
        bot += 1
    left = cx0
    while left > 0 and cols[left - 1]:
        left -= 1
    right = cx1
    while right < W and cols[right]:
        right += 1
    sub = m[top:bot, left:right]
    if sub.any():
        ys, xs = np.where(sub)
        margin = 6
        t = max(0, top + ys.min() - margin)
        b = min(H, top + ys.max() + 1 + margin)
        l = max(0, left + xs.min() - margin)
        r = min(W, left + xs.max() + 1 + margin)
        return img.crop((l, t, r, b))
    return img.crop((left, top, right, bot))


def is_dark(img):
    g = np.asarray(img.convert("L"))
    return float(np.median(g)) < 110


def to_light(img, watermark="keep", photo=True):
    """Return a light-theme version: dark frames are inverted, tinted paper becomes
    white. Watermarks are kept at their original faint tone unless watermark=remove."""
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    if float(np.median(a.mean(axis=2))) < 110:      # light text on dark background
        a = 255.0 - a
        gray = a.mean(axis=2, keepdims=True)
        a = np.repeat(gray, 3, axis=2)              # inverted colours look odd; use grey
        photo = True
    if photo:
        # background (paper) estimate = large max filter on a downscaled copy
        h, w = a.shape[:2]
        f = max(1, int(max(h, w) / 300))
        small = cv2.resize(a, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA)
        bg = cv2.dilate(small, np.ones((9, 9), np.uint8))
        bg = cv2.GaussianBlur(bg, (0, 0), 3)
        bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
        a = np.clip(a / np.maximum(bg, 1) * 255.0, 0, 255)
        # levels: paper -> pure white, ink slightly darker
        a = np.clip((a - 25) * 255.0 / (238 - 25), 0, 255)
    if watermark == "remove":
        lum = a.mean(axis=2)
        sat = a.max(axis=2) - a.min(axis=2)
        # pale or tinted-but-light pixels are watermark/background, real ink is dark
        wm = (lum > 165) | ((sat > 40) & (lum > 120))
        a[wm] = 255
    return Image.fromarray(a.astype(np.uint8))


def text_height_pt(img, px_per_in):
    """Median glyph height in source points (used to estimate displayed font size)."""
    m = ink_mask(img).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    hs = [h for x, y, w, h, area in stats[1:] if 3 <= h <= 0.2 * m.shape[0] and w <= 3 * h and area > 6]
    if len(hs) < 8:
        return None
    return float(np.median(hs)) / px_per_in * 72.0


def stack(images, gap=14):
    W = max(i.width for i in images)
    H = sum(i.height for i in images) + gap * (len(images) - 1)
    out = Image.new("RGB", (W, H), "white")
    y = 0
    for im in images:
        out.paste(im.convert("RGB"), (0, y))
        y += im.height + gap
    return out


def encode(img, max_w_px, max_h_px):
    """Resize to what the quadrant can show and pick the smaller of PNG / JPEG."""
    im = img.convert("RGB")
    s = min(1.0, max_w_px / im.width, max_h_px / im.height)
    if s < 1.0:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    a = np.asarray(im).astype(np.int16)
    colourful = float(np.abs(a[..., 0] - a[..., 1]).mean() + np.abs(a[..., 1] - a[..., 2]).mean())
    if colourful < 6:
        im = im.convert("L")
    png = io.BytesIO()
    (im.quantize(32) if im.mode == "RGB" else im.point(lambda v: v // 16 * 17)).save(png, "PNG", optimize=True)
    jpg = io.BytesIO()
    im.save(jpg, "JPEG", quality=85, optimize=True)
    if png.tell() <= jpg.tell() * 1.4:
        return png.getvalue(), "png", im.size
    return jpg.getvalue(), "jpeg", im.size


def tighten(img):
    """Shrink a crop to its real content: ignore long table rules, and drop a narrow
    strip on the right (marks / negative-marks cells) separated by a wide blank gap.
    The left edge is never cut, so the printed question number stays visible."""
    H, W = img.height, img.width
    if H < 20 or W < 20:
        return img
    c = content_mask(img)
    if c.sum() < 50:
        return img
    cols = c.sum(axis=0) > 0
    xs = np.where(cols)[0]
    right = xs.max() + 1
    # look for a wide blank gap followed by a narrow, sparse strip
    gap_min = max(12, int(0.06 * W))
    run, x = 0, right - 1
    while x > 0:
        if cols[x]:
            if run >= gap_min and (right - (x + run + 1)) < 0.15 * W and c[:, x + run:right].sum() < 0.03 * c.sum():
                right = x + 1
                break
            run = 0
        else:
            run += 1
        x -= 1
    xs = np.where(c[:, :right].sum(axis=0) > 0)[0]
    ys = np.where(c[:, :right].sum(axis=1) > 0)[0]
    if not len(xs) or not len(ys):
        return img
    mg = 8
    return img.crop((max(0, xs.min() - mg), max(0, ys.min() - mg),
                     min(W, xs.max() + 1 + mg), min(H, ys.max() + 1 + mg)))


def content_mask(img):
    """Ink without long table rules (they would hide blank bands / margins)."""
    m = ink_mask(img).astype(np.uint8)
    H, W = m.shape
    horiz = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, W // 3), 1)))
    vert = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, H // 4))))
    return (m & ~(horiz | vert)).astype(bool)


def compress_gaps(img, ppi, max_gap_in=0.12):
    """Collapse tall blank horizontal bands (common between stacked options) so
    the question can be shown larger. Not used on figures."""
    m = content_mask(img)
    blank = m.sum(axis=1) == 0
    max_gap = max(4, int(max_gap_in * ppi))
    keep, run = [], 0
    for y, b in enumerate(blank):
        run = run + 1 if b else 0
        if run <= max_gap:
            keep.append(y)
    if len(keep) >= len(blank) - 2:
        return img
    a = np.asarray(img)
    return Image.fromarray(a[keep])
