# Test results (2026-10-19)

Run on a 4-core cloud container. The Gemini key is an AI Studio free-tier key. Outputs are in `out/final/<name>/` (git-ignored, because the papers are copyrighted).

**Quota reality during the test.** The full Flash models each allow 20 requests/day. 3.8-flash, 3.7-flash, 3.6-flash and 3-flash-preview used theirs up during development, and 3.7-flash returned frequent 503s. So **all the image inputs below were laid out by `gemini-3.5-flash-lite`**, the last-resort model. Its boxes are noticeably worse than full Flash. An early prototype on 3.8-flash produced tight, correct boxes, including detecting NDA's Q28 split across columns. Lite results are cached as "soft" entries, so the next run on a day with Flash quota redoes them automatically.

| Input | Pages | Slides | Expected | Integrity | LLM calls | Size | Time |
|---|---|---|---|---|---|---|---|
| Electrician (digital PDF) | 22 | **75** | 75 | ✅ complete | **0** (text layer) | 1.5 MB | 15 s |
| NDA GAT (22 phone photos of spreads) | 22 | 152 | 150 | ⚠ Q29 ×3, 98→96 order | 7 (3 pages/call) | 2.3 MB | ~3 min |
| A: JEE mock (image) | 1 | **14** | 14 | ✅ | 1 | 0.15 MB | 18 s |
| B: MathonGo (image) | 1 | **6** (Q7-12) | 6 | ✅ | 1 | 0.13 MB | 10 s |
| C: Vedantu solutions (image) | 1 | **4** | 4 | ✅ (4 Ans/Sol blocks excluded) | 1 | 0.05 MB | 22 s |
| D: Adda247 video frame | 1 | **1** (Q.43) | 1 | ✅ | 1 | 0.03 MB | 12 s |

Peak RSS was 263 MB (Electrician, measured with `resource`). The offline test suite passes: `python -m pytest -q tests` (6 tests).

## Acceptance criteria

| Criterion | Result |
|---|---|
| Electrician: 75 slides, answer-key page excluded, page splits stitched, watermark kept, 0 LLM calls | ✅ All met. 18 questions page-stitched with parts aligned; the Marks columns are trimmed. |
| Electrician: `--watermark remove`, `--theme dark`, `--no-axes` | ✅ All build 75 slides. "remove" clears the ellipse and most of the diagonal text, but leaves faint edges and drops the light table rules. |
| NDA: duplicate photo (pages 1/2) skipped | ✅ dHash duplicate detection; no LLM call spent on it. |
| NDA: English kept for the bilingual Part B | ✅ 100 Hindi twins dropped. Language is decided from the script ratio, not the model's label. |
| NDA: directions, PART headers, prepp logo, footers excluded | ✅ mostly. Two crops once grabbed the "NPSS-B-TGA/65B" footer; refinement now stops at clutter. |
| NDA: 150 slides | ⚠ 152. The lite model labelled neighbouring blocks "29" twice. The integrity report shows it as duplicate `S1:29`, and the teacher deletes 2 slides. |
| A: banner/starburst/section band absent; Q8 rejoined | ✅ Clutter absent. Q8's stem and its "(a) 1 (b) 2 (c) 3 (d) 4" from column 2 are on one slide. ⚠ The lite boxes put Q1+Q2 on slide 1 and Q2's slide shows Q3; flagged `low_confidence`. |
| B: starts at 7, diagrams included, watermark carried, header/footer absent | ✅ for Q7, Q8, Q12 (with diagrams). ⚠ Q9-Q11 crops are offset by about one line (lite boxes). |
| C: only stems+options, Ans./Sol. excluded, (1)-(4) not questions | ✅ 4 slides, 4 solution blocks excluded. ⚠ The Q1 crop misses its options; the Q4 crop shows the last line of the previous solution. |
| D: light slide, bilingual, "Q.43", no platform UI | ✅ Rebuilt as native text (dark frame + plain text gives rebuild). Options listed once, Hindi renders. |
| Every question inside Quadrant 1, never crossing the centre lines | ✅ Checked programmatically on all decks. |
| 50-question deck < 15 MB | ✅ The largest is 2.3 MB for 152 slides. |

## Known gaps → next steps
1. Re-run NDA/A/B/C on a day with full-Flash quota (automatic via the soft cache) and compare. The expectation, based on the prototype, is that the remaining box errors disappear.
2. For lite-only days: cross-check box tops against an OCR'd question label (Tesseract is ~30 MB and fits the free host) before accepting a lite box.
3. A review UI to fix the flagged slides in the browser (v2 backlog).
