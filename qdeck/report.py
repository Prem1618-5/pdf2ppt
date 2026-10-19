"""QA report: CSV for spreadsheets + a small self-contained HTML page with thumbnails."""
import base64
import csv
import html
import io

from PIL import Image


def write_csv(path, rows):
    cols = ["slide", "label", "number", "section", "lang", "pages", "render", "confidence",
            "display_font_pt", "flags", "text"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _thumb(img_bytes):
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    im.thumbnail((260, 160))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=60)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def write_html(path, title, summary, integrity, rows, thumbs):
    e = html.escape
    ok = integrity.get("ok")
    head = f"""<!doctype html><html><head><meta charset="utf-8"><title>QA report</title>
<style>body{{font:14px Arial,sans-serif;margin:16px;color:#111;background:#fff}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}
th{{background:#f2f2f2}}.bad{{color:#b00020;font-weight:bold}}.ok{{color:#11772d;font-weight:bold}}
.flag{{display:inline-block;background:#fff3cd;border:1px solid #e0c060;border-radius:3px;padding:0 4px;margin:1px;font-size:12px}}
img{{max-width:260px;border:1px solid #ddd}}</style></head><body>
<h2>QA report - {e(title)}</h2>
<p>Questions detected: <b>{integrity['detected']}</b> &nbsp; expected from numbering: <b>{integrity['expected']}</b>
 &nbsp; <span class="{'ok' if ok else 'bad'}">{'numbering complete' if ok else 'check the flags below'}</span></p>
<p>Sections: {e(', '.join(f"S{s['section']}: {s['first']}-{s['last']} ({s['count']})" for s in integrity['sections']))}</p>
<p>Gaps: {e(', '.join(integrity['gaps']) or 'none')} &nbsp; Duplicates: {e(', '.join(integrity['duplicates']) or 'none')}
 &nbsp; Out of order: {e(', '.join(integrity['out_of_order']) or 'none')}</p>
<p>{e(summary)}</p>
<table><tr><th>Slide</th><th>Label</th><th>Source</th><th>Render</th><th>Conf.</th><th>Font on slide</th><th>Flags</th><th>Preview</th></tr>"""
    body = []
    for r, t in zip(rows, thumbs):
        flags = "".join(f'<span class="flag">{e(x)}</span>' for x in r["flags"].split(";") if x)
        body.append(f"<tr><td>{r['slide']}</td><td>{e(str(r['label']))}</td><td>{e(r['pages'])}</td>"
                    f"<td>{r['render']}</td><td>{r['confidence']}</td><td>{r['display_font_pt']}</td>"
                    f"<td>{flags}</td><td><img src=\"{t}\"></td></tr>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "".join(body) + "</table></body></html>")
