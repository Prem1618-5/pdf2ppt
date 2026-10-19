"""Command-line entry: python cli.py paper.pdf [more files...] -o out_dir [--watermark remove] ..."""
import argparse
import sys

from qdeck.config import Settings
from qdeck.pipeline import run_job


def main():
    ap = argparse.ArgumentParser(description="Question paper -> one-question-per-slide PPTX")
    ap.add_argument("files", nargs="+", help="PDF and/or image files, in order")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--watermark", choices=["keep", "remove"], default="keep")
    ap.add_argument("--no-axes", action="store_true")
    ap.add_argument("--lang", choices=["en", "hi", "both"], default="en")
    ap.add_argument("--render", choices=["auto", "snapshot", "rebuild"], default="auto")
    ap.add_argument("--theme", choices=["light", "dark"], default="light")
    ap.add_argument("--no-llm", action="store_true", help="classical layout only (no API calls)")
    a = ap.parse_args()
    s = Settings()
    if a.no_llm:
        s.use_llm = False
    last = {}

    def progress(stage, i, n):
        if last.get(stage) != i:
            last[stage] = i
            print(f"\r{stage}: {i}/{n}   ", end="", file=sys.stderr, flush=True)
            if i == n:
                print(file=sys.stderr)

    res = run_job(a.files, a.out, s, {"watermark": a.watermark, "axes": not a.no_axes, "lang": a.lang,
                                      "render": a.render, "theme": a.theme}, progress=progress)
    print(res["pptx"])
    print(res["qa_html"])


if __name__ == "__main__":
    main()
