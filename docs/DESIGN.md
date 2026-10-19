# Question paper → one-question-per-slide PPTX: design brief

This brief comes with a working implementation (`qdeck/`, `cli.py`, `static/index.html`). The numbers below come from running it on the two attached papers and on samples A-D. See `docs/TEST_RESULTS.md`.

---

## 0. Interpretation and assumptions

- **One slide per question, in printed reading order.** Slide k is the k-th question as a reader meets it, not sorted by number. The printed label stays visible because it is part of the snapshot.
- **"Clean white paper" vs "carry the watermark forward".** Paper tint, shadows and dark frames are normalised to white. The watermark stays at its original faint tone inside the snapshot, so it shows on every slide that had it. `watermark=remove` is a per-job flag. The master-slide re-apply option is in the v2 backlog.
- **Bilingual papers that print each question twice** (NDA Part B: Hindi page on the left, English page on the right) produce one slide per question number. English is kept by default. `lang=hi|both` are options. Papers where both languages sit in the same block (Electrician, Sample D) keep both.
- **Shared passages** are stitched above the first question of their group. The later questions in the group get a small "Passage: see slide k" line inside Q1. This keeps slides == questions.
- **Fit policy (C9).** The question is scaled to fill Q1 (6.36 × 3.45 in after a 0.15 in inset), keeping its aspect ratio, anchored top-left, never crossing the centre lines. The estimated on-slide font size is computed from glyph heights. Below 10 pt the question is flagged `small_text`, and below 7 pt `too_small_to_read`. Content is never shrunk silently and never spills into another quadrant.
- **Gemini free tier is far smaller than the docs suggest.** This key had **20 requests/day per Flash model**, Pro models were at 0, and models returned frequent 503 "high demand" errors. Requests per day is therefore the scarcest resource, so the design minimises calls: classical parsing first, several pages per call, a per-page cache, and rotation across models.
- **Free-tier data terms.** Google may use prompts and images sent on the free tier to improve its products. Teachers should not upload papers that cannot be shared on those terms (UI note, v2 consent tick-box).
- **The educator's laptop only uploads and downloads.** It needs no install. The page is plain ES5 + XHR so it works on old Chrome/Edge.

---

## Solution 1: Vision-LLM layout pipeline (cloud, always-on free web service)

**a. Idea.** Gemini looks at each page once and returns the role, label, continuation flags and box of every fragment (schema-constrained JSON). The CPU crops, stitches and builds the deck.

**b. Architecture**
```
Browser (upload) ──► FastAPI /api/jobs ──► job queue (1 worker)
  page stream: PyMuPDF/PIL → 1536-px JPEG → dHash (duplicate-page skip)
  ─► Gemini (3 pages per request, JSON schema, cache by image hash)
  ─► per page: padded box → clip-render at 220 dpi → ink-snap → tighten → light-normalise
  ─► assemble: continuations · passages · bilingual twins · duplicates · sections
  ─► python-pptx (axes on master) + QA HTML/CSV + manifest.json
Browser polls /api/jobs/<id> every 3 s ─► download .pptx / report ─► files auto-deleted
```

**c. Stack.** Python 3.12, FastAPI + uvicorn, PyMuPDF, Pillow, OpenCV-headless, NumPy, python-pptx, Gemini REST (`generateContent` with `responseSchema`).

**d. Hosting / free-tier fit.** Render free web service (512 MB, sleeps after 15 min idle, about 1 min cold start, ephemeral disk, which is fine because files are deleted anyway). Gemini free tier: 20 requests/day per Flash model on this key. With 3 pages per call and about 4 usable models, that is roughly 80 calls/day, or about 240 pages/day. When quota runs out the job continues with the classical parser (Solution 2 path) and is flagged `low_confidence`.

**e. Laptop.** Any browser. Upload size is capped at 60 MB.

**f. C1-C12**
| | |
|---|---|
| C1 clutter | LLM roles `clutter/instructions/section_header/solution/answer_key` are dropped. Crops never include neighbouring boxes. |
| C2 columns | The LLM returns reading order across 1-4 columns and spreads. Padding is clamped at the midpoint to neighbours. |
| C3 column split | `continuation` fragments are attached to the open question (same language first) and stacked vertically into one image. |
| C4 page split | Same mechanism across pages. `continues_on_next` is preferred, so col 2 → next page col 1 works. Flag `page_break_stitched`. |
| C5 watermark | Kept inside the snapshot by default (normalisation keeps faint tones). `remove` whitens pale/tinted pixels. Flag `watermark_kept`. |
| C6 fidelity | Snapshot by default, so maths, figures beside text, tables, match-the-following and Devanagari are pixel-exact. |
| C7 numbering | Labels come from the LLM. Integrity check: sections (restarts), gaps, duplicates, out-of-order, detected vs expected. |
| C8 source type | Background division + levels gives white paper. Dark frames are inverted to grey. Auto-rebuild as native text for dark plain-text frames. |
| C9 fit | Fill Q1, estimate on-slide pt, flag below 10/7 pt. Blank bands between stacked options are collapsed before fitting. |
| C10 hardware | Everything runs server side. Peak RSS measured at about 250 MB. |
| C11 free tier | Batching, cache, RPM throttle, 429 per-minute vs per-day detection, 503 failover, classical fallback. |
| C12 dark theme | `manifest.json` stores boxes, crops and text per question. `theme=dark` already exists as a switch (master background + inverted snapshots). |

**g. Failure modes.** The LLM occasionally returns slightly tight boxes (mitigated by padding + ink-snap). It can mislabel a continuation as a new question on very dense pages, which shows up as a numbering gap or duplicate in the report. 503 storms slow jobs down. Requests per day is the hard ceiling.

**h. Cost/time/RAM.** $0. NDA 22 photo pages: 7 requests, about 4-6 min. Peak RAM about 250 MB.

**i. Effort.** About 2 weeks for one student with AI help (most of it in assembly rules and QA).

**j. Pros/cons.** + Handles photos, spreads, dark frames and mixed layouts. − Depends on a fragile free quota, and latency is in minutes.

---

## Solution 2 (recommended): Classical first, Gemini only where needed (hybrid)

**a. Idea.** Use the CPU for anything with a text layer. Number anchors + column profile + "repeated text is clutter" + the integrity check decide whether the result can be trusted. Only pages that fail (scans, photos, frames, broken numbering) go to Gemini.

**b. Architecture**
```
file ─► digital? ── yes ─► text lines + figures ─► repeated-text/marks/page-no filter
                          ─► column profile (merge thin/low-mass strips) ─► anchors that
                             continue the sequence ─► regions ─► integrity OK? ── yes ─► crops
                                                                                └─ no ─► Gemini
      └─ no (scan/photo) ─► Gemini (Solution 1 path) ─► quota gone ─► projection-profile blocks
```

**c. Stack.** Same as Solution 1. The classical parser is `qdeck/layout_classical.py`.

**d. Hosting.** Same as Solution 1 (Render free). A born-digital paper costs **0 LLM calls**. The Electrician paper (22 pages, 75 questions) converted in about 17 s with 0 calls.

**e. Laptop.** Browser only.

**f. C1-C12.** Same as Solution 1 for image pages. For text pages:
- **C1:** repeated-text, page-number, URL and marks-cell filters. Answer-key pages are detected (header plus mostly-short lines) and skipped. `Ans./Sol.` lines end a question region.
- **C2:** x-projection of line boxes with thin and low-text-mass strips merged, so table cells such as Sr.No and Marks are not mistaken for columns.
- **C3/C4:** the top of a column or page before the first anchor becomes a continuation.
- **C7:** anchors are accepted only when they continue the sequence (n = expected … expected+3, or a restart to 1). This kills "(a) 1 (b) 2" lines and numbers inside the stem.

**g. Failure modes.** Text-layer papers with an unusual label style (e.g. "(1)") are rejected by the integrity check and sent to the LLM, which costs quota but stays correct. Image-only pages without quota become low-confidence blocks.

**h. Cost/time/RAM.** $0. Digital paper: about 1 s/page CPU, peak about 200 MB.

**i. Effort.** About 2.5 weeks (Solution 1 + about 3 days of heuristics).

**j. Pros/cons.** + Fastest, quota-proof for digital PDFs, deterministic. − Two code paths to maintain.

---

## Solution 3: No always-on server (GitHub Actions / HF Space / Cloud Run jobs)

**a. Idea.** The same `cli.py` runs as an on-demand job. Nothing stays awake, so there is no sleeping instance and no credit burn.

**b. Architecture**
```
Teacher: form (Cloudflare Pages static) ─► presigned upload (R2/Drive link)
  ─► GitHub workflow_dispatch(url) ─► ubuntu runner: pip install, cli.py ─► artifact (1-day retention)
  (alt: Google Cloud Run *job* triggered per upload; output to GCS with 1-day lifecycle rule)
```
`.github/workflows/convert.yml` is included and works as-is with a `GEMINI_API_KEY` repo secret.

**c. Stack.** GitHub Actions (2,000 free minutes/month on private repos, unlimited on public), or Cloud Run jobs with the $300 trial / always-free 180k vCPU-s, or HF Spaces (CPU basic, 16 GB RAM, sleeps after 48 h).

**d. Free-tier fit.** One NDA run is about 6 runner-minutes, so about 300 papers/month fit. Artifact download needs a GitHub login, which is awkward for teachers, so wrap it behind a small Cloudflare Worker.

**e. Laptop.** Browser only.

**f. C1-C12.** Identical to Solution 2 (same code). C10/C11: the runner has 7 GB RAM, so higher DPI is fine and there are no request timeouts.

**g. Failure modes.** Queue latency of 20-60 s to start a runner. Artifacts need auth. Public repos expose logs, so never log paper text.

**h. Cost/time/RAM.** $0. Adds about 1 min of overhead per job.

**i. Effort.** +3 days on top of Solution 2.

**j. Pros/cons.** + Zero idle cost, generous RAM. − Clunkier delivery, not interactive.

---

## Solution 4: Local-lite fallback (old i3, < 500 MB RAM, no Python)

**a. Idea.** A single-file offline web page (`file://`): pdf.js renders pages, the text layer gives anchors (the same rules as `layout_classical`), canvas crops, and PptxGenJS writes the deck. No server, no ML, digital PDFs only.

**b. Architecture**
```
offline.html ─► pdf.js (getTextContent + render at 150 dpi, one page at a time)
  ─► anchors/columns/repeated-text (JS port of layout_classical) ─► canvas crop → JPEG
  ─► PptxGenJS (master with axes) ─► download
```

**c. Stack.** pdf.js 3.x legacy build (works on old Chrome) and PptxGenJS 3.12, both inlined.

**d. Hosting.** None (USB stick / email attachment). It could also be served from Cloudflare Pages.

**e. Laptop.** Chrome or Edge from about 2019 onwards. About 150-300 MB tab RAM for a 30-page PDF if pages are released after cropping.

**f. C1-C12.** Digital PDFs only. Scans and photos produce one slide per detected block plus a big warning. No watermark removal. No bilingual twin logic beyond "same number twice".

**g. Failure modes.** Scans, photos and frames are not supported. On an i3, expect about 2-4 s per page.

**h. Cost.** $0. Works offline.

**i. Effort.** About 1.5 weeks (porting the classical parser to JS).

**j. Pros/cons.** + Works with no internet and no quota. − Limited to text-layer PDFs.

---

## Comparison

| Criterion | S1 LLM layout | **S2 Hybrid** | S3 Serverless job | S4 Local-lite |
|---|---|---|---|---|
| Digital PDF accuracy | high | **high (0 calls)** | high | medium-high |
| Scans/photos/frames | high | high | high | ✗ |
| LLM calls / 50-q digital paper | ~5-8 | **0** | 0 | 0 |
| Works when quota is gone | degraded | **digital: fully** | digital: fully | n/a |
| Teacher UX | web page | **web page** | form + artifact | offline file |
| Always-on infra | yes (sleeps) | yes (sleeps) | none | none |
| Build effort | 2 wk | 2.5 wk | +3 d | 1.5 wk |

## Recommendation: Solution 2 (hybrid), deployed like Solution 1, with Solution 3 as the overflow path

It is the only option where the scarce resource (Gemini requests per day) is spent only on pages that need it, and where most clean PDFs work with no LLM at all. The integrity check doubles as both the routing rule and the teacher-facing QA.

**MVP milestones**
1. **CLI, digital only:** classical parser + crops + deck + QA. Acceptance: Electrician paper gives 75/75 slides with 0 LLM calls. *(done)*
2. **Vision path + assembly:** batched Gemini layout, continuation/passage/twin/duplicate logic, quota-aware fallback. Acceptance: NDA paper gives 150 slides with a clean integrity report. *(done; see results)*
3. **Web + deploy:** FastAPI jobs, polling page, auto-delete, Dockerfile and render.yaml, key as a secret. Acceptance: upload-to-download on Render free from an old browser. *(code done, deploy is your step)*

## v2 backlog
- Dark theme (switch already wired: `--theme dark`) and teacher-chosen accent colours.
- Review/fix UI: thumbnails grid; drag box edges; merge/split; re-run one page.
- `solutions_in_notes`: keep `solution` fragments and put their text/crop in speaker notes.
- Watermark re-applied from master as an image/text instead of baked in.
- Rebuild mode for more content (LaTeX → OMML for maths); keep both representations per question.
- Overflow policy option: "continue on Q4" (extend downward) for very long questions, behind an explicit toggle.
- Consent/terms tick-box for free-tier data use; optional paid-tier key per school.
- Port classical parser to JS (Solution 4) to share rules client-side.

## Test plan (acceptance)
| Input | Criterion |
|---|---|
| A (JEE mock p1) | Banner, starburst, marks line and section band absent. Q8 rejoined with "(a) 1 (b) 2 (c) 3 (d) 4" from column 2 (`split_stitched`). 14 slides. |
| B (MathonGo p) | Starts at "7". Watermark visible in slides (`watermark_kept`). Diagrams included beside text. Q7-Q12 gives 6 slides. Header/footer absent. |
| C (Vedantu) | Only stems+options. `Ans.`/`Sol.` absent. Options (1)-(4) not mistaken for questions. Q1-Q4 gives 4 slides. |
| D (Adda frame) | Light slide. Text dark on white (inverted or rebuilt). Bilingual kept. Label "Q.43". 1 slide. No UI clutter. |
| Electrician PDF | 75 slides, answer-key page excluded, page-split questions stitched, watermark kept, 0 LLM calls. |
| NDA PDF | 150 slides. Duplicate photo of pages 1-3 dropped. English kept for Part B. Directions excluded. Integrity OK. |
| All | `slides == detected`; QA flags shown; 50-question deck < 15 MB; opens in PowerPoint 2013+/LibreOffice. |
