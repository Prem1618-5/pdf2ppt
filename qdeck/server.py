"""Tiny async job server. Upload -> job id -> poll status -> download.

* One worker thread processes one job at a time (fits a 512 MB free instance).
* No long HTTP request: the browser polls /api/jobs/<id> every few seconds.
* Uploaded papers are deleted as soon as the job finishes; results after JOB_TTL_MIN
  (default 60) or right after the user presses "delete".
* The Gemini key is read from the environment only.
"""
import os
import queue
import shutil
import threading
import time
import traceback
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import Settings
from .ingest import IMAGE_EXT, count_pages
from .pipeline import run_job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_DIR = os.environ.get("JOBS_DIR", os.path.join(ROOT, "jobs"))
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "60"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "80"))
TTL_MIN = int(os.environ.get("JOB_TTL_MIN", "60"))

app = FastAPI(title="Question paper to slides")
settings = Settings()
jobs = {}
q = queue.Queue()
lock = threading.Lock()


def _set(jid, **kw):
    with lock:
        jobs[jid].update(kw)
        jobs[jid]["updated"] = time.time()


def worker():
    while True:
        jid = q.get()
        job = jobs.get(jid)
        if not job:
            continue
        _set(jid, state="running", stage="starting")
        in_dir = os.path.join(JOBS_DIR, jid, "in")
        try:
            def progress(stage, i, n):
                _set(jid, stage=stage, i=i, n=n)

            def log(msg):
                with lock:
                    jobs[jid].setdefault("log", []).append(msg)

            res = run_job(job["files"], os.path.join(JOBS_DIR, jid, "out"), settings, job["options"],
                          progress=progress, log=log)
            _set(jid, state="done", stage="done", result={
                "slides": res["slides"], "integrity": res["integrity"], "summary": res["summary"]})
        except Exception as e:
            traceback.print_exc()
            _set(jid, state="error", message=str(e)[:500])
        finally:
            shutil.rmtree(in_dir, ignore_errors=True)            # copyrighted inputs: gone now
            shutil.rmtree(os.path.join(JOBS_DIR, jid, "out", "work"), ignore_errors=True)


def janitor():
    while True:
        time.sleep(60)
        now = time.time()
        for jid in list(jobs):
            j = jobs[jid]
            if j["state"] in ("done", "error") and now - j["updated"] > TTL_MIN * 60:
                shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
                jobs.pop(jid, None)


@app.on_event("startup")
def _start():
    shutil.rmtree(JOBS_DIR, ignore_errors=True)   # ephemeral disk: nothing survives a restart
    os.makedirs(JOBS_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/healthz")
def health():
    return {"ok": True, "llm": bool(settings.gemini_api_key), "queue": q.qsize()}


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...), watermark: str = Form("keep"),
                     axes: str = Form("on"), lang: str = Form("en"), render: str = Form("auto")):
    jid = uuid.uuid4().hex[:12]
    in_dir = os.path.join(JOBS_DIR, jid, "in")
    os.makedirs(in_dir, exist_ok=True)
    paths, total = [], 0
    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext != ".pdf" and ext not in IMAGE_EXT:
            shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
            raise HTTPException(400, f"Unsupported file: {f.filename}")
        p = os.path.join(in_dir, f"{i:02d}_{os.path.basename(f.filename)}")
        with open(p, "wb") as out:
            while True:
                chunk = await f.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MB << 20:
                    shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
                    raise HTTPException(413, f"Upload larger than {MAX_MB} MB")
                out.write(chunk)
        paths.append(p)
    try:
        n = count_pages(paths)
    except Exception:
        shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
        raise HTTPException(400, "Could not read the file(s)")
    if n > MAX_PAGES:
        shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
        raise HTTPException(413, f"{n} pages; the limit is {MAX_PAGES}")
    opts = {"watermark": "remove" if watermark == "remove" else "keep", "axes": axes != "off",
            "lang": lang if lang in ("en", "hi", "both") else "en",
            "render": render if render in ("auto", "snapshot", "rebuild") else "auto"}
    with lock:
        jobs[jid] = {"id": jid, "state": "queued", "stage": "queued", "i": 0, "n": n, "files": paths,
                     "options": opts, "created": time.time(), "updated": time.time()}
    q.put(jid)
    return {"id": jid, "pages": n}


@app.get("/api/jobs/{jid}")
def status(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "Job not found (finished jobs are deleted after a while)")
    ahead = sum(1 for k in jobs.values() if k["state"] == "queued" and k["created"] < j["created"])
    return JSONResponse({k: j.get(k) for k in ("id", "state", "stage", "i", "n", "message", "result")}
                        | {"queue_ahead": ahead, "log": (j.get("log") or [])[-3:]})


_FILES = {"pptx": ("questions.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
          "report": ("questions_qa.html", "text/html"), "csv": ("questions_qa.csv", "text/csv")}


@app.get("/api/jobs/{jid}/{kind}")
def download(jid: str, kind: str):
    j = jobs.get(jid)
    if not j or j["state"] != "done" or kind not in _FILES:
        raise HTTPException(404, "Not available")
    name, mime = _FILES[kind]
    path = os.path.join(JOBS_DIR, jid, "out", name)
    dl = "questions-slides.pptx" if kind == "pptx" else name
    return FileResponse(path, media_type=mime, filename=dl if kind != "report" else None)


@app.delete("/api/jobs/{jid}")
def delete(jid: str):
    shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
    jobs.pop(jid, None)
    return {"deleted": True}
