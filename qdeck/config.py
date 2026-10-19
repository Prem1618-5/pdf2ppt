"""Runtime settings. Everything comes from environment variables so the API key
never lives in the repo or the browser."""
import os
from dataclasses import dataclass, field, asdict


def _env(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(v)
    if isinstance(default, float):
        return float(v)
    return v


@dataclass
class Settings:
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY", ""))
    # Any number of keys (newline / comma separated). Used when a job brings none.
    gemini_api_keys: str = field(default_factory=lambda: _env("GEMINI_API_KEYS", ""))
    # First model that answers wins; later ones are used on 404 / quota exhaustion.
    gemini_models: str = field(default_factory=lambda: _env(
        "GEMINI_MODELS", "gemini-3.8-flash,gemini-3-flash-preview,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"))
    gemini_rpm: int = field(default_factory=lambda: _env("GEMINI_RPM", 8))
    gemini_workers: int = field(default_factory=lambda: _env("GEMINI_WORKERS", 2))
    # Pages per request. Free tier is counted in requests/day (20 per Flash model at the
    # time of writing), so batching pages is the main lever.
    pages_per_call: int = field(default_factory=lambda: _env("PAGES_PER_CALL", 3))
    # Digital PDFs: trust the classical parser when its numbering integrity is perfect.
    classical_first: bool = field(default_factory=lambda: _env("CLASSICAL_FIRST", True))
    layout_px: int = field(default_factory=lambda: _env("LAYOUT_PX", 1536))   # long side sent to the LLM
    crop_dpi: int = field(default_factory=lambda: _env("CROP_DPI", 220))       # dpi of the displayed snapshot
    cache_dir: str = field(default_factory=lambda: _env("CACHE_DIR", ".cache"))
    use_llm: bool = field(default_factory=lambda: _env("USE_LLM", True))

    # Output options (also settable per job from the web form)
    watermark: str = "remove"        # remove | keep  (v2: no watermark in output by default)
    layout: str = "fill"             # fill (whole slide) | top-right (v1 quadrant)
    blank_after: bool = True         # empty working slide after every question
    lang: str = "en"                 # en | hi | both  (for papers that print each question twice)
    render: str = "auto"             # auto | snapshot | rebuild
    theme: str = "light"             # light (v1); dark is a v2 switch, see deck.py
    solutions_in_notes: bool = False # v2 toggle; v1 always excludes solutions from slides

    def job_options(self):
        return {k: v for k, v in asdict(self).items()
                if k in ("watermark", "layout", "blank_after", "lang", "render", "theme", "solutions_in_notes")}

    @property
    def key_list(self):
        from .keypool import split_keys
        return split_keys(self.gemini_api_keys + "\n" + self.gemini_api_key)

    @property
    def model_list(self):
        return [m.strip() for m in self.gemini_models.split(",") if m.strip()]
