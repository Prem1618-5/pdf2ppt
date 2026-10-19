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
    m = content_mask(img)
    H, W = m.shape
    cx0, cy0, cx1, cy1 = [int(v) for v in core]
    rows = m[:, max(0, cx0):max(cx0 + 1, cx1)].sum(axis=1) > 1
    cols = m[max(0, cy0):max(cy0 + 1, cy1), :].sum(axis=0) > 1
    cy0, cy1 = max(0, min(H - 1, cy0)), max(1, min(H, cy1))
    # a text line cut by the box edge: complete it if most of it is inside the box,
    # otherwise it belongs to the neighbour -> trim back to the blank row
    top = cy0
    if rows[cy0]:
        a = cy0
        while a > 0 and rows[a - 1]:
            a -= 1
        b = cy0
        while b < H and rows[b]:
            b += 1
        top = a if (b - cy0) >= (cy0 - a) else min(b, cy1 - 1)
    bot = cy1
    if rows[cy1 - 1]:
        a = cy1 - 1
        while a > 0 and rows[a - 1]:
            a -= 1
        b = cy1
        while b < H and rows[b]:
            b += 1
        bot = b if (cy1 - a) >= (b - cy1) else max(a, top + 1)
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
        return img.crop((l, t, r, b)), (l, t, r, b)
    return img.crop((left, top, right, bot)), (left, top, right, bot)


def is_dark(img):
    g = np.asarray(img.convert("L"))
    return float(np.median(g)) < 110


def to_light(img, watermark="keep", photo=True, screenshot=False):
    """Return a light-theme version: dark frames are inverted, tinted paper becomes
    white. Watermarks are kept at their original faint tone unless watermark=remove."""
    if not photo and not screenshot:
        # born-digital render: already white paper, black text. Cheap uint8 path; keep
        # colour only if the crop really has colour (coloured figures).
        small = img.convert("RGB").resize((max(1, img.width // 8), max(1, img.height // 8)))
        sa = np.asarray(small).astype(np.int16)
        colour = float((sa.max(axis=2) - sa.min(axis=2)).mean()) > 4
        out = img.convert("RGB") if colour else img.convert("L")
        if watermark == "remove":
            if out.mode == "L":
                out = out.point(lambda v: 255 if v > 238 else v)
            else:
                g = np.asarray(out.convert("L"))
                a = np.asarray(out).copy()
                a[g > 238] = 255
                out = Image.fromarray(a)
        if float(np.median(np.asarray(small.convert("L")))) >= 110:
            return out
        img = out          # dark digital page (rare): fall through to inversion
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    if float(np.median(a.mean(axis=2))) < 110:      # light text on dark background
        a = 255.0 - a
        gray = a.mean(axis=2, keepdims=True)
        a = np.repeat(gray, 3, axis=2)              # inverted colours look odd; use grey
        photo = True
    if screenshot and watermark == "keep" and float(np.percentile(a.mean(axis=2), 60)) > 200:
        # already-light page (screenshot / clean scan): one global white point, so a
        # large pale watermark is not mistaken for paper and flattened away
        white = np.percentile(a.mean(axis=2), 90)
        a = np.clip(a * (255.0 / max(white, 1)), 0, 255)
        gray = a.min(axis=2) * 0.6 + a.mean(axis=2) * 0.4
        a = np.repeat(gray[..., None], 3, axis=2)
        photo = False
    elif photo:
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
        # photos/scans: grey ink, not the camera's blue/yellow cast
        gray = a.min(axis=2) * 0.6 + a.mean(axis=2) * 0.4
        a = np.repeat(gray[..., None], 3, axis=2)
    if watermark == "remove":
        lum = a.mean(axis=2)
        sat = a.max(axis=2) - a.min(axis=2)
        # pale or tinted-but-light pixels are watermark/background, real ink is dark.
        # Digital pages have pure black text, so a lower cut also removes grey
        # watermark lettering and its anti-aliased edges.
        # photos/scans: no hard threshold here (it eats faint strokes of photographed
        # text); enhance() removes smooth pale watermarks via local contrast instead.
        if not photo:
            a[lum > 238] = 255
    return Image.fromarray(a.astype(np.uint8))


def text_height_pt(img, px_per_in):
    """Median glyph height in source points (used to estimate displayed font size)."""
    m = ink_mask(img).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    hs = [h for x, y, w, h, area in stats[1:] if 3 <= h <= 0.2 * m.shape[0] and w <= 3 * h and area > 6]
    if len(hs) < 8:
        return None
    return float(np.median(hs)) / px_per_in * 72.0


def stack(images, gap=14, offsets=None):
    """Stack fragments vertically. `offsets` (px) keep parts of a page-split question
    aligned as they were on the page; column-split parts are left-aligned."""
    offs = list(offsets or [0] * len(images))
    base = min(offs)
    offs = [o - base for o in offs]
    W = max(i.width + o for i, o in zip(images, offs))
    H = sum(i.height for i in images) + gap * (len(images) - 1)
    out = Image.new("RGB", (W, H), "white")
    y = 0
    for im, o in zip(images, offs):
        out.paste(im.convert("RGB"), (o, y))
        y += im.height + gap
    return out


def encode(img, max_w_px, max_h_px):
    """Resize to what the slide shows (at the target dpi) and store losslessly when the
    picture is mostly text/line art (PNG, full 8-bit grey: sharp glyph edges); photos of
    figures with lots of colour fall back to high-quality JPEG only when PNG is huge."""
    im = img.convert("RGB")
    s = min(1.0, max_w_px / im.width, max_h_px / im.height)
    if s < 1.0:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    a = np.asarray(im).astype(np.int16)
    colourful = float(np.abs(a[..., 0] - a[..., 1]).mean() + np.abs(a[..., 1] - a[..., 2]).mean())
    if colourful < 6:
        im = im.convert("L")
    png = io.BytesIO()
    im.save(png, "PNG", compress_level=6)
    if im.mode == "L":
        return png.getvalue(), "png", im.size          # text / line art: always lossless
    jpg = io.BytesIO()
    im.save(jpg, "JPEG", quality=92, subsampling=0)
    if png.tell() <= jpg.tell() * 2.0:
        return png.getvalue(), "png", im.size
    return jpg.getvalue(), "jpeg", im.size


def enhance(img, src_ppi, target_ppi=300, crisp_ink=True):
    """Low-resolution photos/screenshots: upscale to the target ppi (Lanczos), unsharp
    mask scaled to the upscale factor, and a gentle tone curve that makes ink solid
    black while paper stays white. Gives crisp characters and clean diagram lines
    on a projector without any ML model."""
    from PIL import ImageFilter
    f = target_ppi / max(src_ppi, 1.0)
    im = img
    if f > 1.15:
        f = min(f, 4.0)
        im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
    else:
        f = 1.0
    im = im.filter(ImageFilter.UnsharpMask(radius=max(1.0, 0.9 * f), percent=140, threshold=2))
    if not crisp_ink:
        a = np.asarray(im.convert("RGB")).astype(np.float32) / 255.0
        a = np.clip((a - 0.06) / 0.86, 0, 1) ** 1.25
        return Image.fromarray((a * 255).astype(np.uint8)), (src_ppi * f)
    g = np.asarray(im.convert("L")).astype(np.float32)
    # ink crisping: compare each pixel with its neighbourhood and push it through a
    # smooth sigmoid -> solid black strokes with soft (anti-aliased) edges, white paper
    local = cv2.GaussianBlur(g, (0, 0), sigmaX=max(4.0, 3.0 * f))
    ink = 1.0 / (1.0 + np.exp(-(local - g - 14.0) * 0.3))   # paper -> ~0, strokes -> ~1
    crisp = 255.0 * (1.0 - ink)
    dark = g < 110                                        # solid areas (filled shapes, bold)
    crisp[dark] = np.minimum(crisp[dark], g[dark])
    base = np.clip((g - 15) * 255.0 / 215.0, 0, 255)
    out = np.minimum(0.85 * crisp + 0.15 * base, np.maximum(crisp, base))
    out[out > 232] = 255
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)), (src_ppi * f)


def tighten(img):
    """Shrink a crop to its real content: ignore long table rules, and drop a narrow
    strip on the right (marks / negative-marks cells) separated by a wide blank gap.
    The left edge is never cut, so the printed question number stays visible."""
    H0, W0 = img.height, img.width
    if H0 < 20 or W0 < 20:
        return img, (0, 0, W0, H0)
    full = img
    img, f = _small(img)
    H, W = img.height, img.width
    c = content_mask(img)
    if c.sum() < 50:
        return full, (0, 0, W0, H0)
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
        return full, (0, 0, W0, H0)
    mg = 8
    rect = (max(0, xs.min() - mg), max(0, ys.min() - mg), min(W, xs.max() + 1 + mg), min(H, ys.max() + 1 + mg))
    rect = tuple(int(round(v * f)) for v in rect)
    rect = (rect[0], rect[1], min(W0, rect[2]), min(H0, rect[3]))
    return full.crop(rect), rect


def _small(img, max_side=2400):
    """Work copy for mask analysis (big 600-dpi renders are analysed at <= max_side)."""
    f = max(1.0, max(img.size) / max_side)
    if f == 1.0:
        return img, 1.0
    return img.resize((int(img.width / f), int(img.height / f)), Image.BILINEAR), f


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
    sm, f = _small(img)
    ms = content_mask(sm).sum(axis=1) == 0
    idx = np.minimum((np.arange(img.height) / f).astype(int), len(ms) - 1)
    blank = ms[idx]
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


def strip_rules(img):
    """Erase table rules: vertical lines taller than ~55% of the crop and horizontal
    lines wider than ~75% (cell borders around a question). Short lines such as
    underlines, fraction bars and figure frames are kept."""
    full = img
    img, f = _small(img)
    g = np.asarray(img.convert("L"))
    H, W = g.shape
    if H < 30 or W < 30:
        return full
    m = (cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 18) > 0).astype(np.uint8)
    vert = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(H * 0.55))))
    horiz = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(W * 0.75), 1)))
    rules = cv2.dilate(vert | horiz, np.ones((3, 3), np.uint8))
    if not rules.any():
        return full
    if f != 1.0:
        rules = cv2.resize(rules, (full.width, full.height), interpolation=cv2.INTER_NEAREST)
        rules = cv2.dilate(rules, np.ones((int(f) + 1, int(f) + 1), np.uint8))
    a = np.asarray(full).copy()
    a[rules > 0] = 255
    return Image.fromarray(a)


def erase(img, rects):
    """White out pixel rectangles (e.g. a duplicate question-ID column)."""
    if not rects:
        return img
    a = np.asarray(img.convert("RGB")).copy()
    for l, t, r, b in rects:
        a[max(0, int(t)):max(0, int(b)), max(0, int(l)):max(0, int(r))] = 255
    return Image.fromarray(a)
