# Editor API contract (v2)

The server keeps one **project** per job. The project is JSON, and the deck is rebuilt from it on export. Every edit is a small JSON call; the server re-renders only the item that changed. All paths are relative to the site root. All JSON bodies are UTF-8.

## Project JSON (`GET /api/jobs/{jid}/project`)
```json
{
  "id": "a1b2c3d4e5f6",
  "state": "done",
  "settings": {"layout": "fill", "blank_after": true, "watermark": "remove", "theme": "light"},
  "integrity": {"detected": 75, "expected": 75, "ok": true, "gaps": [], "duplicates": [], "out_of_order": []},
  "summary": "75 slides from 22 pages ...",
  "pages": [ {"seq": 0, "ref": "paper.pdf#p1", "w": 1086, "h": 1536} ],
  "items": [
    {"id": "q0007", "label": "7.", "number": 7, "lang": "en",
     "render": "snapshot",            // "snapshot" (picture) | "text" (native, editable text)
     "stem": "A current loop ...", "options": ["1.0 x 10^-7 T", "..."],
     "parts": [ {"page": 3, "box": [0.08, 0.31, 0.49, 0.47]} ],   // fractions x0,y0,x1,y1 of that page
     "flags": ["split_stitched", "low_confidence"], "confidence": 0.62,
     "blank_after": null,             // null = follow settings.blank_after; true/false = override
     "custom_image": false,           // true after "replace image"
     "rev": 3,                        // bumps on every change of the rendered picture
     "img_w": 1310, "img_h": 702}
  ],
  "can_undo": true,
  "keys": [ {"masked": "…a9F2", "state": "ok", "calls": 4} ]
}
```
Item order in `items` is the slide order. With `blank_after`, every question slide is followed by one empty slide for working.

## Read
| Method & path | Returns |
|---|---|
| `GET /api/jobs/{jid}` | Status: `{id,state:"queued|running|done|error",stage,i,n,message,result,queue_ahead,log,keys}` |
| `GET /api/jobs/{jid}/project` | The project JSON above. Only valid once `state=="done"`. |
| `GET /api/jobs/{jid}/pages/{seq}.jpg` | Page image (long side about 1536 px) for the crop editor |
| `GET /api/jobs/{jid}/items/{iid}.png?rev=N` | Current rendered question picture (cleaned, watermark removed) |
| `GET /api/jobs/{jid}/pptx` | Last exported deck (call export first) |
| `GET /api/jobs/{jid}/report` · `/csv` | QA report |

## Edit (each returns `{"ok":true,"item":{...}}` or `{"ok":true,"project":{...}}`)
| Method & path | Body | Effect |
|---|---|---|
| `PATCH /api/jobs/{jid}/items/{iid}` | any of `label, stem, options, render, parts, blank_after` | Update the item. Changing `parts` re-crops from the source page(s). Returns `item`. |
| `POST /api/jobs/{jid}/items` | `{"after": iid or null, "label": "12.", "parts": [{"page":2,"box":[..]}]}` | New question from a box drawn on a page. Returns `item`. |
| `DELETE /api/jobs/{jid}/items/{iid}` | none | Remove the slide. Returns `project`. |
| `POST /api/jobs/{jid}/items/{iid}/duplicate` | none | Copy inserted after it (use it to split one question into two, then adjust each crop). Returns `project`. |
| `POST /api/jobs/{jid}/items/{iid}/merge` | `{"with": iid2}` | Append iid2's parts below iid's; delete iid2. Returns `project`. |
| `POST /api/jobs/{jid}/items/{iid}/image` | multipart `file` (png/jpg) | Replace the picture with an uploaded image. Returns `item`. |
| `POST /api/jobs/{jid}/order` | `{"ids": [iid, ...]}` | New slide order (must be a permutation). Returns `project`. |
| `PATCH /api/jobs/{jid}/settings` | any of `layout ("fill"|"top-right"), blank_after, watermark ("remove"|"keep")` | Returns `project`. |
| `POST /api/jobs/{jid}/undo` | none | Undo the last edit (up to 50). Returns `project`. |
| `POST /api/jobs/{jid}/export` | none | Build the .pptx from the project. Returns `{"ok":true,"slides":N,"url":"/api/jobs/{jid}/pptx"}` |

## Create
| Method & path | Body | Returns |
|---|---|---|
| `POST /api/jobs` | multipart: `files` (1..n), `keys` (text, one Gemini key per line, optional), `lang`, `render` | `{"id", "pages"}`. Then poll `GET /api/jobs/{jid}`. |
| `POST /api/import` | multipart: `file` (.pptx made by this tool or any deck with one picture per slide) | `{"id"}`. The job is immediately `done` and editable. |
| `POST /api/keys/check` | `{"keys": ["...", "..."]}` | `[{"masked":"…a9F2","ok":true,"error":null}]`. Validates keys without spending generate quota. |

Errors use HTTP 4xx/5xx with `{"detail": "message"}`.

## Key handling
- Keys typed in the browser are sent with the job. The server keeps them **in memory only** (never on disk, never logged) and drops them when the job finishes.
- The browser may remember them in `localStorage`, but only if the user ticks "Remember on this computer".
- Server-wide keys can be configured as `GEMINI_API_KEYS` (newline/comma separated) and are used when the user supplies none.
