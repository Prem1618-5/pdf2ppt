"""PowerPoint writer. 16:9, white, one question per slide in Quadrant 1 (top-right).
Repeated decoration (cross-hair axes, background) lives on the slide master only."""
import io

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

SLIDE_W, SLIDE_H = 13.333, 7.5
INSET = 0.15
Q1 = (SLIDE_W / 2 + INSET, INSET, SLIDE_W / 2 - 2 * INSET, SLIDE_H / 2 - 2 * INSET)  # left, top, w, h (in)
MIN_REBUILD_PT = 16

THEMES = {
    # v1 ships "light"; "dark" is the v2 switch: same manifest, different paint.
    "light": {"bg": RGBColor(0xFF, 0xFF, 0xFF), "fg": RGBColor(0x11, 0x11, 0x11), "axis": RGBColor(0xC8, 0xC8, 0xC8)},
    "dark": {"bg": RGBColor(0x12, 0x14, 0x18), "fg": RGBColor(0xF2, 0xF2, 0xF2), "axis": RGBColor(0x44, 0x48, 0x50)},
}

DEVANAGARI_FONT = "Nirmala UI"   # Windows 8+; PowerPoint falls back to Mangal on Windows 7
LATIN_FONT = "Arial"


MARGIN = 0.3
FULL = (MARGIN, MARGIN, SLIDE_W - 2 * MARGIN, SLIDE_H - 2 * MARGIN)
MAX_PT = 36          # never blow a short question up beyond ~36 pt type


def _new_presentation(theme):
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    blank = prs.slide_layouts[6]
    bg = prs.slide_master.background.fill
    bg.solid()
    bg.fore_color.rgb = THEMES[theme]["bg"]
    return prs, blank


def _set_fonts(run, size_pt, color, bold=False):
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = LATIN_FONT
    rPr = run._r.get_or_add_rPr()
    for tag, face in (("a:ea", DEVANAGARI_FONT), ("a:cs", DEVANAGARI_FONT)):
        el = rPr.find(qn(tag))
        if el is None:
            el = etree.SubElement(rPr, qn(tag))
        el.set("typeface", face)


def rebuild_fits(label, stem, options, box=Q1):
    """Largest font (pt) at which the rebuilt text fits the box, or None."""
    paras = [f"{label} {stem}"] + [f"({chr(97 + i)}) {o}" for i, o in enumerate(options)]
    for size in range(40, MIN_REBUILD_PT - 1, -1):
        cpl = box[2] * 72 / (0.55 * size)  # characters per line (average glyph width ~0.55 em)
        lines = sum(max(1, -(-len(p) // int(cpl))) for p in paras)
        if lines * size * 1.25 / 72 + 0.15 <= box[3]:
            return size
    return None


class DeckBuilder:
    """layout "fill": the question uses the whole slide (centred, top-aligned);
    layout "top-right": the v1 quadrant placement. No axes, no quadrant lines."""

    def __init__(self, theme="light", layout="fill"):
        self.theme = theme
        self.prs, self.layout = _new_presentation(theme)
        self.t = THEMES[theme]
        self.box = FULL if layout == "fill" else Q1
        self.centre = layout == "fill"

    def add_snapshot(self, img_bytes, px_size, notes, src_w_in=None, glyph_pt=None):
        """Place a question picture; returns the estimated font size on the slide."""
        s = self.prs.slides.add_slide(self.layout)
        L, T, BW, BH = self.box
        w_px, h_px = px_size
        scale = min(BW / w_px, BH / h_px)
        w_in, h_in = w_px * scale, h_px * scale
        disp = None
        if src_w_in and glyph_pt:
            disp = glyph_pt / 0.52 * (w_in / src_w_in)
            if disp > MAX_PT:                       # short question: don't make it gigantic
                f = MAX_PT / disp
                w_in, h_in, disp = w_in * f, h_in * f, MAX_PT
        left = L + (BW - w_in) / 2 if self.centre else L
        s.shapes.add_picture(io.BytesIO(img_bytes), Inches(left), Inches(T), Inches(w_in), Inches(h_in))
        self._notes(s, notes)
        return round(disp, 1) if disp else None

    def add_blank(self):
        """Empty slide for the teacher's working."""
        self.prs.slides.add_slide(self.layout)

    def add_rebuilt(self, label, stem, options, size_pt, notes):
        s = self.prs.slides.add_slide(self.layout)
        tb = s.shapes.add_textbox(Inches(self.box[0]), Inches(self.box[1]), Inches(self.box[2]), Inches(self.box[3]))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.TOP
        for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(tf, side, Emu(Inches(0.05)))
        p = tf.paragraphs[0]
        r = p.add_run()
        r.text = label + " "
        _set_fonts(r, size_pt, self.t["fg"], bold=True)
        r = p.add_run()
        r.text = stem
        _set_fonts(r, size_pt, self.t["fg"])
        for i, o in enumerate(options):
            p = tf.add_paragraph()
            p.space_before = Pt(size_pt * 0.3)
            r = p.add_run()
            r.text = f"({chr(97 + i)}) {o}"
            _set_fonts(r, size_pt, self.t["fg"])
        self._notes(s, notes)

    @staticmethod
    def _notes(slide, notes):
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    def save(self, path):
        self.prs.save(path)
