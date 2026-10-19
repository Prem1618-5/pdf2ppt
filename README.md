# Question paper to slides (qdeck)

This tool turns a question paper (a PDF, scans or phone photos) into a PowerPoint deck with
**one question per slide**, in reading order.

- The question **fills the slide** as a sharp, high-resolution picture, with **no watermark** and no table borders.
- An **empty slide follows each question** for working out the solution.
- Solutions, answer keys and repeated pages are left out.
- A **web editor** lets the teacher fix anything before downloading: re-crop, reorder, merge, delete, retype or add questions.
- Any number of **Gemini API keys** can be supplied. When one key hits its limit, the next one takes over automatically.
- Every run also writes a **QA report** that flags anything to check.

Design: [docs/DESIGN.md](docs/DESIGN.md) · Editor API: [docs/EDITOR_API.md](docs/EDITOR_API.md).

## How it works

```
PDF / images
  └─ page stream (PyMuPDF / PIL) → 1536 px JPEG + dHash (skip duplicate pages)
       ├─ born-digital PDF: classical text-layer parser
       │     numbering complete (no gaps)? → keep it, 0 LLM calls
       └─ otherwise (scans, photos, messy PDFs):
             Gemini vision layout, 3 pages per request, JSON schema
             · per-page cache (.cache/, keyed by image hash)
             · model rotation on 404 / quota / 503, backoff on 429 / 5xx
             · quota gone → classical image parser fallback
  └─ watermark strip at the PDF level (objects stamped on every page)
  └─ boxes: pad → ink-snap → tighten → editable project (project.json)
  └─ render each question for its on-slide size (250 px per displayed inch; vector PDFs up to
     600 dpi; photos upscaled + ink-crisped) → remove table rules → lossless PNG
  └─ assemble: continuations, column/page splits, passages, bilingual twins
  └─ questions.pptx (question slide + blank slide) + QA report (HTML/CSV)
```

The server processes one job at a time and streams pages one by one, so it fits on a 512 MB instance.

## Quick start (local)

Requires Python 3.12 (3.10+ should work).

```bash
pip install -r requirements.txt
export GEMINI_API_KEYS="key1,key2,key3" # optional, any number; without keys only the classical parser runs
python cli.py paper.pdf -o out        # writes out/questions.pptx and out/questions_qa.html
```

CLI flags (`python cli.py FILES... [options]`; several files are processed in the order given):

| Flag | Values (default first) | Meaning |
|---|---|---|
| `-o, --out` | `out` | Output directory |
| `--watermark` | `remove`, `keep` | Remove watermarks (default), or keep them |
| `--layout` | `fill`, `top-right` | The question fills the slide, or sits in the top-right quarter |
| `--no-blank` | (switch) | Do not add the empty working slide after each question |
| `--keys-file` | path | Text file with Gemini keys, one per line |
| `--lang` | `en`, `hi`, `both` | For papers that print each question twice (Hindi + English) |
| `--render` | `auto`, `snapshot`, `rebuild` | `snapshot` always uses a picture of the question. `rebuild` retypes plain-text questions. `auto` retypes only dark-background ones |
| `--theme` | `light`, `dark` | `dark` is the v2 preview |
| `--no-llm` | (switch) | Classical layout only, no API calls |

Web app (the educator only needs a browser):

```bash
uvicorn qdeck.server:app --port 8080
# open http://localhost:8080   (health check: /healthz)
```

Tests (offline, no key needed):

```bash
python -m pytest -q tests
```

## Configuration

All settings come from environment variables. Defaults are shown below.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEYS` | (empty) | Server-wide keys, separated by newlines or commas, any number. Used when the user supplies none |
| `GEMINI_API_KEY` | (empty) | A single key; added to the pool above |
| `GEMINI_MODELS` | `gemini-3.8-flash,gemini-3.7-flash,gemini-3-flash-preview,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` | Models tried in order. The next one is used on 404, quota exhaustion or 503 |
| `GEMINI_RPM` | `8` | Requests per minute **per key** |
| `GEMINI_WORKERS` | `2` | Base parallelism. It grows with the number of live keys, up to 8 |
| `PAGES_PER_CALL` | `3` | Pages sent in one request (the main way to save quota) |
| `CLASSICAL_FIRST` | `true` | Try the text-layer parser on digital PDFs first |
| `LAYOUT_PX` | `1536` | Long side (px) of the page image sent to Gemini |
| `CROP_DPI` | `250` | Pixels per inch of the question picture **as shown on the slide** |
| `CACHE_DIR` | `.cache` | Per-page layout cache (re-running a paper costs 0 calls) |
| `USE_LLM` | `true` | `false` turns off Gemini entirely (same as `--no-llm`) |
| `JOBS_DIR` | `./jobs` | Server scratch space. It is wiped at every startup |
| `MAX_UPLOAD_MB` | `60` | Total upload size limit per job |
| `MAX_PAGES` | `80` | Page limit per job |
| `JOB_TTL_MIN` | `360` | Minutes an idle project stays editable before it and its uploads are deleted |
| `MAX_KEYS` | `200` | Maximum keys accepted per job |

## Deploy for $0

The `Dockerfile` listens on `$PORT` (default 8080) and runs a single uvicorn worker.
**Never put the key in the image or the repo.** Set it as a secret on the host.

**(a) Render (free web service).** Push the repo to GitHub. In Render, choose New → Blueprint and
select the repo; `render.yaml` sets up a Docker web service with `/healthz`. Add `GEMINI_API_KEY` in
the dashboard; it is marked `sync: false`, so it is never stored in the file. Free plan limits:
the service sleeps when idle, so the first request after a pause has a cold start of about 1 minute.
It has 512 MB RAM, and the disk is ephemeral.

**(b) Hugging Face Space.** Create a Space with the **Docker** SDK and push this repo to it.
Spaces read the port from the YAML front-matter of the *Space's* README. Add it at the top of the
README in the Space repo:

```yaml
---
title: Question paper to slides
sdk: docker
app_port: 8080
---
```

Then add `GEMINI_API_KEY` under Settings → Variables and secrets → *Secret*.

**(c) Google Cloud Run.**

```bash
gcloud run deploy qdeck --source . --region us-central1 --allow-unauthenticated \
  --memory 1Gi --timeout 3600 --max-instances 1 \
  --set-secrets GEMINI_API_KEY=gemini-key:latest   # or --set-env-vars GEMINI_API_KEY=...
```

Use `--max-instances 1` because job state lives in memory. With several instances, a status poll
can land on an instance that does not know the job.

**(d) GitHub Actions, no server.** Add `GEMINI_API_KEY` under repo Settings → Secrets → Actions.
Then go to Actions → *convert-paper* → Run workflow, and give it a direct download URL of the paper
and the watermark option. The deck and QA report come back as a workflow artifact, which is deleted
after 1 day. See `.github/workflows/convert.yml`.

## Many API keys: how the rotation works

Paste any number of keys in the web form (one per line), or set `GEMINI_API_KEYS` on the server.
Each **key × model** pair is tracked separately:

| Gemini says | What happens |
|---|---|
| 200 OK | The key stays in rotation. Keys are used round-robin, so the load spreads evenly |
| 429 per-minute limit | Only that key+model pauses, for Google's suggested delay; other keys continue |
| 429 daily quota (`PerDay`, or a retry hint over 10 min) | That key+model is retired until Google's reset time (midnight Pacific) |
| 400/403 bad key | The key is dropped for this job and shown as ✗ in the UI |
| 503 overloaded | That model pauses for 20 s for every key; a lower model is used meanwhile |
| All keys out for today | Remaining pages fall back to the classical parser and are flagged |

More keys also means more parallel requests (2 per live key, capped at 8 to protect a small server).
Key health is remembered across jobs, as a hash of the key, never the key itself, so a dead key is not retried.
**Keys from the same Google Cloud project share one quota.** Keys only add capacity if they come
from different projects. Use keys you are entitled to use (your own, your school's, a paid
key). Google's terms do not allow creating accounts just to get around usage limits.

## Gemini free tier: the reality

Observed on 2026-10-19 with an AI Studio key:

- **20 requests/day per Flash model.** Pro models had a quota of **0**.
- Frequent **503 "high demand"** errors, even when quota was left.

This is why pages are batched 3 per request and results are cached per page. Models are rotated,
and born-digital PDFs with clean numbering use **0 calls**. When every model is exhausted, the
remaining pages fall back to the classical parser; see Limitations.
Current limits: <https://ai.google.dev/gemini-api/docs/rate-limits>.

**Data use:** on the free tier, Google may use prompts and images to improve its products.
**Do not upload restricted or confidential papers** (unreleased exams, licensed material) through a
free-tier key. Use a paid key, or `--no-llm` for digital PDFs.

## Privacy

- Uploaded files are kept only while the project is editable. Re-cropping needs the source pages.
  Everything is deleted `JOB_TTL_MIN` minutes after the last activity, or right away on *delete*.
- Nothing is kept across restarts: `JOBS_DIR` is wiped at startup and the job list lives in memory.
- Keys typed in the browser live in server memory only for the duration of the analysis. They are never written to disk or logged.
  The browser remembers them only if *Remember keys on this computer* is ticked (localStorage).
- Keep the key in `.env` or host secrets. **Never commit `.env`**; it is listed in `.gitignore`.
- The layout cache (`CACHE_DIR`) holds only box/label JSON, never page images.

## Outputs

| File | Contents |
|---|---|
| `questions.pptx` | The deck: a question slide followed by an empty slide, for every question. Speaker notes hold the label, flags and extracted text |
| `questions_qa.html` | QA report: a summary (slides, pages, excluded blocks, LLM calls, cache hits), a numbering integrity check, and one row per slide with a thumbnail and flags |
| `questions_qa.csv` | The same rows as the HTML report, for a spreadsheet |
| `project.json` | The editable project: pages, and every question's boxes, label, text and flags. The editor works on this. The deck is rebuilt from it, with no new Gemini calls |

## The editor (web app)

After processing, **Review & edit** shows every slide:

- **Adjust crop:** drag the box edges on the original page, or add a second box from the next page for a question that runs over.
- **Add a missing question:** draw a box on any page.
- **Reorder** (drag or the ↑ ↓ buttons), **Delete**, **Duplicate** (to split one into two), **Merge with next**.
- **Label** editing, and **Picture / Text** style. Text style retypes the question as editable PowerPoint text.
- **Replace picture:** upload your own image for a slide.
- **Blank slide after:** on or off per slide, or for the whole deck.
- Question size: **fill the slide** or **top-right corner**. Watermark removal can be turned on or off.
- **Undo**, up to 50 steps (Ctrl+Z).
- **Download PowerPoint** rebuilds the deck from your edits.
- **Open an existing deck (.pptx)** to edit a deck made earlier.

## Tips for the educator

- The deck opens in **PowerPoint 2013+**, **LibreOffice Impress** and **WPS**.
- Before class, open the QA report and look at the flagged rows:
  - `small_text`: the question shows below about 12 pt on the slide. Consider splitting it with Duplicate + Adjust crop.
  - `too_small_to_read`: the question shows below about 7 pt (usually a very long question). Fix this one.
  - `low_confidence`: the layout was uncertain. Check that the crop holds the whole question.
  - `no_number`: no question number was found, so the order may be wrong.
- Retyped Devanagari text uses **Nirmala UI** (Windows 8+). On older Windows, PowerPoint falls back to **Mangal**.

## Limitations

- **Image-only pages with no Gemini quota left** go through the classical image parser. It produces
  question blocks with no numbers and low confidence (`no_number`, `low_confidence`). The order is
  usually right, but check those slides.
- **Very tall questions** (long passages, big tables) are flagged `small_text` / `too_small_to_read`.
- **Gemini boxes are sometimes off**, especially from the `-lite` fallback models. Fix them with
  Adjust crop in the editor; `low_confidence` marks the slides to check.
- **Photos cannot gain real detail.** Upscaling and ink crisping make the text solid and sharp,
  but a blurry photo stays blurry. Photograph pages flat, in good light, and fill the frame.
- The free-tier quota is the bottleneck: about 60 scanned pages per Flash model per day at 3 pages per call.
