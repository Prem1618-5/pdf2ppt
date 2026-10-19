# Question paper to slides (qdeck)

This tool turns a question paper (a PDF, scans or phone photos) into a PowerPoint deck with
**one question per slide**, in reading order. Each question goes in the **top-right quadrant**, so the
rest of the slide is free for writing. Solutions, answer keys and repeated pages are left out.
Every run also writes a **QA report** that flags anything the educator should check.
Design rationale and trade-offs: [docs/DESIGN.md](docs/DESIGN.md).

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
  └─ crop at 220 dpi → pad → ink-snap → tighten → normalise to white paper
  └─ assemble: continuations, column/page splits, passages, bilingual twins
  └─ questions.pptx + QA report (HTML/CSV) + manifest.json
```

The server processes one job at a time and streams pages one by one, so it fits on a 512 MB instance.

## Quick start (local)

Requires Python 3.12 (3.10+ should work).

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key        # optional: without it only the classical parser runs
python cli.py paper.pdf -o out        # writes out/questions.pptx and out/questions_qa.html
```

CLI flags (`python cli.py FILES... [options]`; several files are processed in the order given):

| Flag | Values (default first) | Meaning |
|---|---|---|
| `-o, --out` | `out` | Output directory |
| `--watermark` | `keep`, `remove` | Carry the paper's watermark into the snapshots, or remove it |
| `--no-axes` | (switch) | Hide the faint centre cross on the slide master |
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
| `GEMINI_API_KEY` | (empty) | Google AI Studio key. Read on the server only. Empty means classical parsing only |
| `GEMINI_MODELS` | `gemini-3.8-flash,gemini-3.7-flash,gemini-3-flash-preview,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` | Models tried in order. The next one is used on 404, quota exhaustion or 503 |
| `GEMINI_RPM` | `8` | Client-side request-per-minute throttle |
| `GEMINI_WORKERS` | `2` | Parallel Gemini requests |
| `PAGES_PER_CALL` | `3` | Pages sent in one request (the main way to save quota) |
| `CLASSICAL_FIRST` | `true` | Try the text-layer parser on digital PDFs first |
| `LAYOUT_PX` | `1536` | Long side (px) of the page image sent to Gemini |
| `CROP_DPI` | `220` | Resolution of the question snapshots |
| `CACHE_DIR` | `.cache` | Per-page layout cache (re-running a paper costs 0 calls) |
| `USE_LLM` | `true` | `false` turns off Gemini entirely (same as `--no-llm`) |
| `JOBS_DIR` | `./jobs` | Server scratch space. It is wiped at every startup |
| `MAX_UPLOAD_MB` | `60` | Total upload size limit per job |
| `MAX_PAGES` | `80` | Page limit per job |
| `JOB_TTL_MIN` | `60` | Minutes a finished job's outputs are kept |

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

- Uploaded files are deleted as soon as the job finishes, whether it succeeds or fails.
- Outputs are deleted `JOB_TTL_MIN` minutes after the job finishes, or right away when the user presses *delete*.
- Nothing is kept across restarts: `JOBS_DIR` is wiped at startup and the job list lives in memory.
- The Gemini key stays on the server and is never sent to the browser.
- Keep the key in `.env` or host secrets. **Never commit `.env`**; it is listed in `.gitignore`.
- The layout cache (`CACHE_DIR`) holds only box/label JSON, never page images.

## Outputs

| File | Contents |
|---|---|
| `questions.pptx` | The deck: one question per slide in the top-right quadrant. Speaker notes hold the source page, confidence, estimated font size, flags and extracted text |
| `questions_qa.html` | QA report: a summary (slides, pages, excluded blocks, LLM calls, cache hits), a numbering integrity check, and one row per slide with a thumbnail and flags |
| `questions_qa.csv` | The same rows as the HTML report, for a spreadsheet |
| `questions_manifest.json` | Options, integrity, page notes, and each question's fragments (page, box, crop), stem and options. It lets you re-render the deck (for example the v2 dark theme) without calling Gemini again |

The web app offers the pptx, HTML report and CSV for download. The CLI also writes the manifest.

## Tips for the educator

- The deck opens in **PowerPoint 2013+**, **LibreOffice Impress** and **WPS**.
- Before class, open the QA report and look at the flagged rows:
  - `small_text`: the question shows below about 10 pt on the slide. Zoom in, or split it by hand.
  - `too_small_to_read`: the question shows below about 7 pt (usually a very long question). Fix this one.
  - `low_confidence`: the layout was uncertain. Check that the crop holds the whole question.
  - `no_number`: no question number was found, so the order may be wrong.
- Retyped Devanagari text uses **Nirmala UI** (Windows 8+). On older Windows, PowerPoint falls back to **Mangal**.

## Limitations

- **Image-only pages with no Gemini quota left** go through the classical image parser. It produces
  question blocks with no numbers and low confidence (`no_number`, `low_confidence`). The order is
  usually right, but check those slides.
- **Very tall questions** (long passages, big tables) are flagged `small_text` / `too_small_to_read`.
  They are never shrunk silently or allowed to spill out of the quadrant.
- **Gemini boxes are sometimes too tight** and can clip an option or the edge of a figure. Padding
  toward neighbouring blocks and ink-snapping reduce this, but check `low_confidence` rows.
- The free-tier quota is the bottleneck: about 60 scanned pages per Flash model per day at 3 pages per call.
