"""CPU refinement of LLM boxes (fixes boxes that are a few lines short or shifted).

Exam layouts use a hanging indent: the line carrying the question number starts at
the column's left margin, wrapped stem lines and options are indented. Inside each
column we find text lines with a projection profile, mark the flush-left ones as
"block starts", then snap every numbered fragment's top to the nearest block start
and its bottom to just above the next fragment's start. Moves are bounded, so a wrong
snap can never travel far.
"""
import numpy as np
import cv2

MAX_MOVE = 0.06       # fraction of page height a boundary may move
BOUNDARY_ROLES = ("question", "continuation", "shared_passage", "solution", "answer_key",
                  "instructions", "section_header")


def _lines(bw):
    rows = bw.sum(axis=1) > 0
    out, s = [], None
    for y, r in enumerate(list(rows) + [False]):
        if r and s is None:
            s = y
        elif not r and s is not None:
            if y - s >= 3:
                out.append([s, y])
            s = None
    merged = []
    for a, b in out:                       # join accents / matras split from their line
        if merged and a - merged[-1][1] <= 2:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return merged


def refine(page_img, frags, boxes):
    """page_img: PIL image of the page (layout resolution). frags/boxes aligned lists,
    boxes are (x0,y0,x1,y1) fractions or None. Returns new boxes."""
    g = np.asarray(page_img.convert("L"))
    H, W = g.shape
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 18)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))   # drop speckle
    new = list(boxes)
    idx = [i for i, (f, b) in enumerate(zip(frags, boxes)) if b is not None and f.get("role") in BOUNDARY_ROLES
           and (b[2] - b[0]) < 0.6]
    # group into columns by horizontal overlap
    cols = []
    for i in sorted(idx, key=lambda i: boxes[i][0]):
        x0, _, x1, _ = boxes[i]
        for c in cols:
            if min(x1, c["x1"]) - max(x0, c["x0"]) > 0.5 * min(x1 - x0, c["x1"] - c["x0"]):
                c["items"].append(i)
                c["x0"], c["x1"] = min(c["x0"], x0), max(c["x1"], x1)
                break
        else:
            cols.append({"x0": x0, "x1": x1, "items": [i]})
    for c in cols:
        items = sorted(c["items"], key=lambda i: boxes[i][1])
        X0, X1 = int(c["x0"] * W), int(np.ceil(c["x1"] * W))
        Y0 = int(max(0, boxes[items[0]][1] - MAX_MOVE) * H)
        Y1 = int(min(1, boxes[items[-1]][3] + MAX_MOVE) * H)
        strip = bw[Y0:Y1, X0:X1]
        lines = _lines(strip)
        if len(lines) < 3:
            continue
        lefts = []
        for a, b in lines:
            colsum = strip[a:b].sum(axis=0) > 0
            xs = np.where(colsum)[0]
            lefts.append(xs.min() if len(xs) else 10 ** 6)
        margin = np.percentile(lefts, 10)
        tol = 0.012 * W
        starts = [(a + Y0, b + Y0) for (a, b), l in zip(lines, lefts) if l <= margin + tol]
        all_lines = [(a + Y0, b + Y0) for a, b in lines]
        gaps = [all_lines[k + 1][0] - all_lines[k][1] for k in range(len(all_lines) - 1)]
        med_gap = float(np.median(gaps)) if gaps else 4.0
        tops = {}
        for i in items:
            f = frags[i]
            y0 = boxes[i][1] * H
            if f.get("role") == "continuation" or (f.get("role") == "question" and f.get("number") is None):
                tops[i] = y0
                continue
            cand = [s for s in starts if abs(s[0] - y0) <= MAX_MOVE * H]
            tops[i] = (min(cand, key=lambda s: abs(s[0] - y0))[0] - 2) if cand else y0
        for k, i in enumerate(items):
            x0, _, x1, y1 = new[i]
            top = tops[i]
            if k + 1 < len(items):
                bottom = tops[items[k + 1]] - 1
            else:
                # last block of the column: extend through lines that continue the block
                bottom = y1 * H
                after = [ln for ln in all_lines if ln[0] >= top]
                for a, b in after:
                    if a > bottom + 2.2 * med_gap + 2:
                        break
                    bottom = max(bottom, b)
            if abs(bottom - y1 * H) > MAX_MOVE * H * 1.5:
                bottom = y1 * H
            if bottom - top > 6:
                new[i] = (x0, max(0.0, top / H), x1, min(1.0, bottom / H))
    return new
