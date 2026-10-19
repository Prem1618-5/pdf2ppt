"""Web app: upload -> async job -> editable project -> export .pptx.

* One worker thread processes one analysis job at a time (fits a 512 MB free host).
* No long HTTP request: the browser polls /api/jobs/<id>.
* Gemini keys come from the job form (any number) or GEMINI_API_KEYS. Form keys stay
  in memory only and are dropped when the job finishes.
* Uploaded papers stay only as long as the project is editable (JOB_TTL_MIN,
  default 6 h, or until the user deletes the job), then everything is removed.
API contract: docs/EDITOR_API.md
"""
import os
import queue
import shutil
import threading
import time
import traceback
import uuid

import requests
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .ingest import IMAGE_EXT, count_pages
from .keypool import mask, split_keys
from .pipeline import run_job
from .project import Project, copy_into

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_DIR = os.environ.get("JOBS_DIR", os.path.join(ROOT, "jobs"))
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "60"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "80"))
TTL_MIN = int(os.environ.get("JOB_TTL_MIN", "360"))
MAX_KEYS = int(os.environ.get("MAX_KEYS", "200"))

app = FastAPI(title="Question paper to slides")
settings = Settings()
jobs = {}
q = queue.Queue()
lock = threading.Lock()


def _set(jid, **kw):
    with lock:
        jobs[jid].update(kw)
        jobs[jid]["updated"] = time.time()


def _job_dir(jid):
    return os.path.join(JOBS_DIR, jid)


def _project(jid):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "This job no longer exists (results are deleted after a while). Please upload again.")
    if j["state"] != "done":
        raise HTTPException(409, "The job is not finished yet")
    j["updated"] = time.time()
    return Project(os.path.join(_job_dir(jid), "out"))


def worker():
    while True:
        jid = q.get()
        job = jobs.get(jid)
        if not job:
            continue
        _set(jid, state="running", stage="starting")
        try:
            def progress(stage, i, n):
                _set(jid, stage=stage, i=i, n=n)

            def log(msg):
                with lock:
                    jobs[jid].setdefault("log", []).append(msg)

            def on_pool(pool):
                with lock:
                    jobs[jid]["pool"] = pool

            res = run_job(job["files"], os.path.join(_job_dir(jid), "out"), settings, job["options"],
                          progress=progress, log=log, keys=job.pop("keys", None) or None, on_pool=on_pool)
            _set(jid, state="done", stage="done", result={
                "slides": res["slides"], "integrity": res["integrity"], "summary": res["summary"]})
        except Exception as e:
            traceback.print_exc()
            _set(jid, state="error", message=str(e)[:500])
        finally:
            with lock:
                jobs.get(jid, {}).pop("keys", None)          # keys never outlive the job


def janitor():
    while True:
        time.sleep(60)
        now = time.time()
        for jid in list(jobs):
            j = jobs[jid]
            if j["state"] in ("done", "error") and now - j["updated"] > TTL_MIN * 60:
                shutil.rmtree(_job_dir(jid), ignore_errors=True)
                jobs.pop(jid, None)


@app.on_event("startup")
def _start():
    shutil.rmtree(JOBS_DIR, ignore_errors=True)   # ephemeral disk: nothing survives a restart
    os.makedirs(JOBS_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()


app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/healthz")
def health():
    return {"ok": True, "server_keys": len(settings.key_list), "queue": q.qsize()}


# ---------------------------------------------------------------------- create
@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...), keys: str = Form(""), lang: str = Form("en"),
                     render: str = Form("auto"), watermark: str = Form("remove"), layout: str = Form("fill"),
                     blank_after: str = Form("on")):
    jid = uuid.uuid4().hex[:12]
    in_dir = os.path.join(_job_dir(jid), "in")
    os.makedirs(in_dir, exist_ok=True)
    paths, total = [], 0
    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext != ".pdf" and ext not in IMAGE_EXT:
            shutil.rmtree(_job_dir(jid), ignore_errors=True)
            raise HTTPException(400, f"Unsupported file: {f.filename}")
        p = os.path.join(in_dir, f"{i:02d}_{os.path.basename(f.filename)}")
        with open(p, "wb") as out:
            while True:
                chunk = await f.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MB << 20:
                    shutil.rmtree(_job_dir(jid), ignore_errors=True)
                    raise HTTPException(413, f"Upload larger than {MAX_MB} MB")
                out.write(chunk)
        paths.append(p)
    try:
        n = count_pages(paths)
    except Exception:
        shutil.rmtree(_job_dir(jid), ignore_errors=True)
        raise HTTPException(400, "Could not read the file(s)")
    if n > MAX_PAGES:
        shutil.rmtree(_job_dir(jid), ignore_errors=True)
        raise HTTPException(413, f"{n} pages; the limit is {MAX_PAGES}")
    opts = {"watermark": "keep" if watermark == "keep" else "remove",
            "layout": "top-right" if layout == "top-right" else "fill",
            "blank_after": blank_after != "off",
            "lang": lang if lang in ("en", "hi", "both") else "en",
            "render": render if render in ("auto", "snapshot", "rebuild") else "auto"}
    user_keys = split_keys(keys)[:MAX_KEYS]
    with lock:
        jobs[jid] = {"id": jid, "state": "queued", "stage": "queued", "i": 0, "n": n, "files": paths,
                     "options": opts, "keys": user_keys, "created": time.time(), "updated": time.time()}
    q.put(jid)
    return {"id": jid, "pages": n, "keys": len(user_keys)}


@app.post("/api/import")
async def import_deck(file: UploadFile = File(...)):
    from .pptx_import import import_pptx
    if not (file.filename or "").lower().endswith(".pptx"):
        raise HTTPException(400, "Please choose a .pptx file")
    jid = uuid.uuid4().hex[:12]
    root = os.path.join(_job_dir(jid), "out")
    src_dir = os.path.join(_job_dir(jid), "in")
    os.makedirs(src_dir, exist_ok=True)
    path = os.path.join(src_dir, "deck.pptx")
    with open(path, "wb") as out:
        out.write(await file.read())
    try:
        res = import_pptx(path, src_dir)
    except Exception as e:
        shutil.rmtree(_job_dir(jid), ignore_errors=True)
        raise HTTPException(400, f"Could not read this deck: {type(e).__name__}")
    pages = []
    from PIL import Image
    pages_dir = os.path.join(root, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    for pg in res["pages"]:
        with Image.open(pg["path"]) as im:
            im = im.convert("RGB")
            thumb = os.path.join(pages_dir, f"{pg['seq']:04d}.jpg")
            t = im.copy()
            t.thumbnail((1536, 1536))
            t.save(thumb, "JPEG", quality=85)
            w_in = im.width / 200.0
            pages.append({"seq": pg["seq"], "ref": pg["ref"], "kind": "image", "src": pg["path"], "index": 0,
                          "w_in": w_in, "h_in": im.height / 200.0, "thumb": thumb,
                          "thumb_w": t.width, "thumb_h": t.height})
    items = res["items"]
    for it in items:
        it.setdefault("erase", [])
    proj = Project.create(root, pages, items, res.get("settings"), summary=f"Imported {file.filename}: "
                          f"{len(items)} question slides.")
    for it in proj.data["items"]:
        proj.item_png(it["id"])
    proj.save()
    with lock:
        jobs[jid] = {"id": jid, "state": "done", "stage": "done", "i": len(items), "n": len(items), "files": [],
                     "options": {}, "created": time.time(), "updated": time.time(),
                     "result": {"slides": len(items), "integrity": {}, "summary": ""}}
    return {"id": jid}


@app.post("/api/keys/check")
def check_keys(body: dict = Body(...)):
    """Validate keys with the free models.list call (does not use generate quota)."""
    keys = split_keys("\n".join(body.get("keys") or []))[:MAX_KEYS]
    out = []
    for k in keys:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                             headers={"x-goog-api-key": k}, timeout=20)
            ok = r.status_code == 200
            err = None if ok else (r.json().get("error", {}).get("message", "")[:120] or f"HTTP {r.status_code}")
        except Exception as e:
            ok, err = False, f"network error ({type(e).__name__})"
        out.append({"masked": mask(k), "ok": ok, "error": err})
    return out


# ---------------------------------------------------------------------- read
@app.get("/api/jobs/{jid}")
def status(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "Job not found (finished jobs are deleted after a while)")
    ahead = sum(1 for k in jobs.values() if k["state"] == "queued" and k["created"] < j["created"])
    pool = j.get("pool")
    return JSONResponse({k: j.get(k) for k in ("id", "state", "stage", "i", "n", "message", "result")}
                        | {"queue_ahead": ahead, "log": (j.get("log") or [])[-3:],
                           "keys": pool.stats() if pool else []})


@app.get("/api/jobs/{jid}/project")
def project(jid: str):
    p = _project(jid)
    out = p.public()
    out.update(id=jid, state="done", keys=jobs[jid]["pool"].stats() if jobs[jid].get("pool") else [])
    return out


@app.get("/api/jobs/{jid}/pages/{seq}.jpg")
def page_image(jid: str, seq: int):
    p = _project(jid)
    if not 0 <= seq < len(p.data["pages"]):
        raise HTTPException(404, "No such page")
    return FileResponse(p.page_jpeg(seq), media_type="image/jpeg")


@app.get("/api/jobs/{jid}/items/{iid}.png")
def item_image(jid: str, iid: str):
    p = _project(jid)
    with p.lock:
        p = Project(p.root)            # re-read inside the lock: never save a stale copy
        try:
            path = p.item_png(iid)
        except KeyError:
            raise HTTPException(404, "No such question")
        p.save()
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


_FILES = {"pptx": ("questions.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
          "report": ("questions_qa.html", "text/html"), "csv": ("questions_qa.csv", "text/csv")}


@app.get("/api/jobs/{jid}/{kind}")
def download(jid: str, kind: str):
    j = jobs.get(jid)
    if not j or j["state"] != "done" or kind not in _FILES:
        raise HTTPException(404, "Not available")
    name, mime = _FILES[kind]
    path = os.path.join(_job_dir(jid), "out", name)
    if not os.path.exists(path):
        _project(jid).export(os.path.join(_job_dir(jid), "out"))
    return FileResponse(path, media_type=mime,
                        filename="questions-slides.pptx" if kind == "pptx" else (name if kind == "csv" else None))


# ---------------------------------------------------------------------- edit
def _edit(jid, fn):
    """Run one edit under the project lock with an undo checkpoint."""
    p = _project(jid)
    with p.lock:
        p = Project(p.root)            # re-read inside the lock
        p.checkpoint()
        result = fn(p)
        p.save()
        return result


def _item_out(p, it):
    p.item_png(it["id"])
    return {"ok": True, "item": {k: it.get(k) for k in ("id", "label", "number", "lang", "render", "stem", "options",
                                                         "parts", "flags", "confidence", "blank_after",
                                                         "custom_image", "rev", "img_w", "img_h")}}


def _check_parts(p, parts):
    if not isinstance(parts, list) or not parts:
        raise HTTPException(400, "A question needs at least one box")
    clean = []
    for part in parts:
        seq = int(part.get("page", -1))
        if not 0 <= seq < len(p.data["pages"]):
            raise HTTPException(400, "Unknown page")
        x0, y0, x1, y1 = [min(1.0, max(0.0, float(v))) for v in part.get("box", [])[:4]]
        if x1 - x0 < 0.005 or y1 - y0 < 0.003:
            raise HTTPException(400, "The box is too small")
        clean.append({"page": seq, "box": [round(x0, 5), round(y0, 5), round(x1, 5), round(y1, 5)]})
    return clean


@app.patch("/api/jobs/{jid}/items/{iid}")
def patch_item(jid: str, iid: str, body: dict = Body(...)):
    def fn(p):
        try:
            it = p.item(iid)
        except KeyError:
            raise HTTPException(404, "No such question")
        for k in ("label", "stem"):
            if k in body:
                it[k] = str(body[k] or "")[:4000]
        if "label" in body:
            digits = "".join(ch for ch in it["label"] if ch.isdigit())
            it["number"] = int(digits) if digits else None
        if "options" in body:
            it["options"] = [str(o)[:1000] for o in (body["options"] or []) if str(o).strip()][:8]
        if "render" in body:
            it["render"] = "text" if body["render"] == "text" else "snapshot"
        if "blank_after" in body:
            v = body["blank_after"]
            it["blank_after"] = None if v is None else bool(v)
        if "parts" in body:
            it["parts"] = _check_parts(p, body["parts"])
            it["custom_image"] = False
            it["erase"] = [e for e in it.get("erase") or []
                           if any(e["page"] == pt["page"] for pt in it["parts"])]
            it["flags"] = [f for f in it.get("flags") or [] if f not in ("low_confidence",)] + ["edited"]
            it["flags"] = list(dict.fromkeys(it["flags"]))
            it["confidence"] = 1.0
            p.touch(it)
        return _item_out(p, it)
    return _edit(jid, fn)


@app.post("/api/jobs/{jid}/items")
def add_item(jid: str, body: dict = Body(...)):
    def fn(p):
        parts = _check_parts(p, body.get("parts"))
        label = str(body.get("label") or "?")[:40]
        digits = "".join(ch for ch in label if ch.isdigit())
        it = {"id": p.new_id(), "label": label, "number": int(digits) if digits else None, "lang": "en",
              "render": "snapshot", "stem": "", "options": [], "parts": parts, "erase": [], "flags": ["added"],
              "confidence": 1.0, "blank_after": None, "custom_image": False, "rev": 1}
        items = p.data["items"]
        after = body.get("after")
        pos = next((i + 1 for i, x in enumerate(items) if x["id"] == after), len(items)) if after else len(items)
        items.insert(pos, it)
        return _item_out(p, it)
    return _edit(jid, fn)


@app.delete("/api/jobs/{jid}/items/{iid}")
def delete_item(jid: str, iid: str):
    def fn(p):
        before = len(p.data["items"])
        p.data["items"] = [x for x in p.data["items"] if x["id"] != iid]
        if len(p.data["items"]) == before:
            raise HTTPException(404, "No such question")
        return {"ok": True, "project": p.public()}
    return _edit(jid, fn)


@app.post("/api/jobs/{jid}/items/{iid}/duplicate")
def duplicate_item(jid: str, iid: str):
    def fn(p):
        import copy
        items = p.data["items"]
        i = next((k for k, x in enumerate(items) if x["id"] == iid), None)
        if i is None:
            raise HTTPException(404, "No such question")
        it = copy.deepcopy(items[i])
        it["id"] = p.new_id()
        it["flags"] = list(dict.fromkeys((it.get("flags") or []) + ["edited"]))
        items.insert(i + 1, it)
        return {"ok": True, "project": p.public()}
    return _edit(jid, fn)


@app.post("/api/jobs/{jid}/items/{iid}/merge")
def merge_items(jid: str, iid: str, body: dict = Body(...)):
    def fn(p):
        other = body.get("with")
        try:
            a, b = p.item(iid), p.item(other)
        except KeyError:
            raise HTTPException(404, "No such question")
        if a is b:
            raise HTTPException(400, "Choose two different questions")
        if a.get("custom_image") or b.get("custom_image"):
            raise HTTPException(400, "Questions with a replaced picture cannot be merged")
        a["parts"] = (a.get("parts") or []) + (b.get("parts") or [])
        a["erase"] = (a.get("erase") or []) + (b.get("erase") or [])
        a["stem"] = " ".join(x for x in (a.get("stem"), b.get("stem")) if x)
        a["options"] = (a.get("options") or []) + (b.get("options") or [])
        a["flags"] = list(dict.fromkeys((a.get("flags") or []) + ["edited"]))
        p.data["items"] = [x for x in p.data["items"] if x["id"] != other]
        p.touch(a)
        return {"ok": True, "project": p.public()}
    return _edit(jid, fn)


@app.post("/api/jobs/{jid}/items/{iid}/image")
async def replace_image(jid: str, iid: str, file: UploadFile = File(...)):
    from PIL import Image
    data = await file.read()
    if len(data) > 15 << 20:
        raise HTTPException(413, "Picture larger than 15 MB")
    tmp = os.path.join(_job_dir(jid), f"upload_{uuid.uuid4().hex[:8]}")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        with Image.open(tmp) as im:
            im = im.convert("RGB")
            im.thumbnail((3000, 3000))
            png = tmp + ".png"
            im.save(png)
    except Exception:
        os.remove(tmp)
        raise HTTPException(400, "That file is not a picture")

    def fn(p):
        try:
            it = p.item(iid)
        except KeyError:
            raise HTTPException(404, "No such question")
        it["custom_image"] = copy_into(p.root, png, f"custom_{iid}_{uuid.uuid4().hex[:6]}.png")
        it["custom_ppi"] = 200
        it["render"] = "snapshot"
        it["flags"] = list(dict.fromkeys((it.get("flags") or []) + ["edited"]))
        p.touch(it)
        return _item_out(p, it)
    try:
        return _edit(jid, fn)
    finally:
        for x in (tmp, tmp + ".png"):
            if os.path.exists(x):
                os.remove(x)


@app.post("/api/jobs/{jid}/order")
def reorder(jid: str, body: dict = Body(...)):
    def fn(p):
        ids = body.get("ids") or []
        cur = {x["id"]: x for x in p.data["items"]}
        if sorted(ids) != sorted(cur):
            raise HTTPException(400, "The new order must contain every question exactly once")
        p.data["items"] = [cur[i] for i in ids]
        return {"ok": True, "project": p.public()}
    return _edit(jid, fn)


@app.patch("/api/jobs/{jid}/settings")
def patch_settings(jid: str, body: dict = Body(...)):
    def fn(p):
        s = p.settings
        if "layout" in body:
            s["layout"] = "top-right" if body["layout"] == "top-right" else "fill"
        if "blank_after" in body:
            s["blank_after"] = bool(body["blank_after"])
        if "watermark" in body:
            s["watermark"] = "keep" if body["watermark"] == "keep" else "remove"
            for it in p.data["items"]:
                it["rev"] = int(it.get("rev") or 0) + 1      # pictures re-render lazily
        return {"ok": True, "project": p.public()}
    return _edit(jid, fn)


@app.post("/api/jobs/{jid}/undo")
def undo(jid: str):
    p = _project(jid)
    with p.lock:
        p = Project(p.root)
        if not p.undo():
            raise HTTPException(400, "Nothing to undo")
        for it in p.data["items"]:
            it["rev"] = int(it.get("rev") or 0) + 1
        p.save()
        return {"ok": True, "project": p.public()}


@app.post("/api/jobs/{jid}/export")
def export(jid: str):
    p = _project(jid)
    with p.lock:
        p = Project(p.root)
        path, n = p.export(os.path.join(_job_dir(jid), "out"))
    return {"ok": True, "slides": n, "url": f"/api/jobs/{jid}/pptx"}


@app.delete("/api/jobs/{jid}")
def delete(jid: str):
    shutil.rmtree(_job_dir(jid), ignore_errors=True)
    jobs.pop(jid, None)
    return {"deleted": True}
