"""PowerPoint writer. 16:9, white, one question per slide in Quadrant 1 (top-right).
Repeated decoration (cross-hair axes, background) lives on the slide master only."""
import copy
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


def _new_presentation(theme, axes):
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    blank = prs.slide_layouts[6]
    master = prs.slide_master
    t = THEMES[theme]
    # background on the master
    bg = master.background.fill
    bg.solid()
    bg.fore_color.rgb = t["bg"]
    if axes:
        tmp = prs.slides.add_slide(blank)
        lines = [
            tmp.shapes.add_connector(1, Inches(SLIDE_W / 2), Inches(0), Inches(SLIDE_W / 2), Inches(SLIDE_H)),
            tmp.shapes.add_connector(1, Inches(0), Inches(SLIDE_H / 2), Inches(SLIDE_W), Inches(SLIDE_H / 2)),
        ]
        sp_tree = master.shapes._spTree
        for ln in lines:
            ln.line.color.rgb = t["axis"]
            ln.line.width = Pt(0.75)
            ln.line.dash_style = 4  # dash
            sp_tree.append(copy.deepcopy(ln._element))
        # drop the temporary slide
        sld_id = prs.slides._sldIdLst[-1]
        prs.part.drop_rel(sld_id.rId)
        prs.slides._sldIdLst.remove(sld_id)
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


def rebuild_fits(label, stem, options):
    """Largest font (pt) at which the rebuilt text fits Quadrant 1, or None."""
    paras = [f"{label} {stem}"] + [f"({chr(97 + i)}) {o}" for i, o in enumerate(options)]
    for size in range(28, MIN_REBUILD_PT - 1, -1):
        cpl = Q1[2] * 72 / (0.55 * size)  # characters per line (average glyph width ~0.55 em)
        lines = sum(max(1, -(-len(p) // int(cpl))) for p in paras)
        if lines * size * 1.25 / 72 + 0.15 <= Q1[3]:
            return size
    return None


class DeckBuilder:
    def __init__(self, theme="light", axes=True):
        self.theme = theme
        self.prs, self.layout = _new_presentation(theme, axes)
        self.t = THEMES[theme]

    def add_snapshot(self, img_bytes, px_size, notes):
        s = self.prs.slides.add_slide(self.layout)
        w_px, h_px = px_size
        scale = min(Q1[2] / w_px, Q1[3] / h_px)
        w_in, h_in = w_px * scale, h_px * scale
        s.shapes.add_picture(io.BytesIO(img_bytes), Inches(Q1[0]), Inches(Q1[1]), Inches(w_in), Inches(h_in))
        self._notes(s, notes)
        return w_in, h_in

    def add_rebuilt(self, label, stem, options, size_pt, notes):
        s = self.prs.slides.add_slide(self.layout)
        tb = s.shapes.add_textbox(Inches(Q1[0]), Inches(Q1[1]), Inches(Q1[2]), Inches(Q1[3]))
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

    def add_caption(self, text):
        """Tiny reference line under the question, still inside Quadrant 1."""
        s = self.prs.slides[-1]
        tb = s.shapes.add_textbox(Inches(Q1[0]), Inches(SLIDE_H / 2 - INSET - 0.3), Inches(Q1[2]), Inches(0.28))
        r = tb.text_frame.paragraphs[0].add_run()
        r.text = text
        _set_fonts(r, 11, RGBColor(0x66, 0x66, 0x66))

    @staticmethod
    def _notes(slide, notes):
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    def save(self, path):
        self.prs.save(path)
