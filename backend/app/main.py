"""PKDB backend: API for the browser extension + map embeds for wiki pages."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import auth, db, geo, pipeline, search, wikijs
from .auth import ApiUser, EmbedUser
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pkdb")

STATIC = Path(__file__).parent / "static"
BROWSER_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    geo.ensure_dir("images")
    geo.ensure_dir("submissions")
    boot = asyncio.create_task(wikijs.bootstrap())
    await pipeline.start_worker()
    yield
    boot.cancel()
    await db.close()


app = FastAPI(title="PKDB", lifespan=lifespan, docs_url="/pkdb/api/docs", openapi_url="/pkdb/api/openapi.json")
# The extension calls the API from moz-extension:// pages; auth is via bearer token, never cookies.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   allow_credentials=False)
app.mount("/pkdb/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------------------------
# Health & auth
# --------------------------------------------------------------------------------------------

@app.get("/pkdb/api/health")
async def health():
    return {"ok": True, "wiki": wikijs.status()}


class LoginBody(BaseModel):
    username: str
    password: str
    device: str = "browser extension"


@app.post("/pkdb/api/auth/login")
async def login(body: LoginBody):
    if not wikijs.is_ready():
        raise HTTPException(503, "Wiki is still starting, try again in a minute")
    try:
        await wikijs.login(body.username, body.password)
    except wikijs.WikiError as e:
        raise HTTPException(401, f"Login failed: {e}") from e
    token = await auth.issue_token(body.device[:100], body.username)
    return {"token": token, "user": body.username}


@app.get("/pkdb/api/auth/me")
async def me(user=ApiUser):
    return {"user": user["user"], "wiki": wikijs.status(), "config": client_config()}


@app.post("/pkdb/api/auth/logout")
async def logout(request: Request, user=ApiUser):
    if user["token_id"]:
        await db.p().execute("DELETE FROM pkdb.api_tokens WHERE id=$1", user["token_id"])
    return {"ok": True}


def client_config() -> dict:
    return {"apply_mode": settings.llm_apply_mode, "wiki_url": settings.public_url,
            "wiki_language": settings.wiki_language, "llm_model": settings.llm_model,
            "max_upload_mb": settings.max_upload_mb}


@app.get("/pkdb/api/config")
async def get_config(user=ApiUser):
    return client_config()


# --------------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------------

@app.get("/pkdb/api/search")
async def do_search(q: str, limit: int | None = None, user=ApiUser):
    q = q.strip()
    if not q:
        return {"query": q, "pages": [], "maps": []}
    return await search.search(q, min(limit or settings.search_max_results, 20))


# --------------------------------------------------------------------------------------------
# Geodata preview
# --------------------------------------------------------------------------------------------

async def _save_uploads(files: list[StarletteUploadFile], target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    out = []
    limit = settings.max_upload_mb * 1024 * 1024
    for f in files:
        name = Path(f.filename or "upload").name or "upload"
        name = re.sub(r"[^\w.\- ()äöüÄÖÜß]", "_", name)
        dest = target / name
        size = 0
        with dest.open("wb") as fh:
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"{name} is larger than {settings.max_upload_mb} MB")
                fh.write(chunk)
        out.append(dest)
    return out


@app.post("/pkdb/api/geodata/preview")
async def geodata_preview(files: list[UploadFile] = File(...), user=ApiUser):
    tmp = Path(tempfile.mkdtemp(prefix="pkdb-preview-"))
    try:
        paths = await _save_uploads(files, tmp)
        datasets = await asyncio.to_thread(geo.convert_files, paths)
        return {"datasets": [d.as_dict() for d in datasets]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------------
# Submissions
# --------------------------------------------------------------------------------------------

def _store_image(data: bytes, original_name: str, content_type: str) -> tuple[str, str, int | None, int | None]:
    """Store an image file, converting formats browsers can't show (TIFF, BMP, ...) to PNG/JPEG."""
    from PIL import Image  # noqa: PLC0415

    width = height = None
    mime = content_type if content_type in BROWSER_IMAGE_TYPES else (mimetypes.guess_type(original_name)[0] or "")
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if mime not in BROWSER_IMAGE_TYPES:
                buf = io.BytesIO()
                if im.mode in ("RGBA", "LA", "P"):
                    im.convert("RGBA").save(buf, "PNG")
                    mime = "image/png"
                else:
                    im.convert("RGB").save(buf, "JPEG", quality=90)
                    mime = "image/jpeg"
                data = buf.getvalue()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{original_name} is not a supported image ({e})") from e
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}[mime]
    stem = wikijs.slugify(Path(original_name).stem, 50)
    filename = f"{uuid.uuid4().hex[:8]}-{stem}{ext}"
    Path(settings.data_dir, "images", filename).write_bytes(data)
    return filename, mime, width, height


@app.post("/pkdb/api/submissions")
async def create_submission(request: Request, user=ApiUser):
    form = await request.form(max_files=500, max_fields=1000, max_part_size=settings.max_upload_mb * 1024 * 1024)
    try:
        meta = json.loads(form.get("meta") or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(400, "meta must be JSON") from e
    text = (meta.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "A description / text is required")
    apply_mode = meta.get("apply_mode") or settings.llm_apply_mode
    if apply_mode not in ("review", "auto"):
        apply_mode = "review"
    stored_meta = {k: meta.get(k) for k in ("drawn", "drawn_title", "drawn_description", "geofiles", "images",
                                            "map_title", "map_description") if meta.get(k) is not None}
    sid = await db.p().fetchval(
        "INSERT INTO pkdb.submissions(created_by, title, text, source_url, apply_mode, status, meta) "
        "VALUES($1,$2,$3,$4,$5,'receiving',$6) RETURNING id",
        user["user"], (meta.get("title") or "")[:300], text, (meta.get("source_url") or "")[:2000],
        apply_mode, stored_meta)
    try:
        geo_files = [f for f in form.getlist("geofiles") if isinstance(f, StarletteUploadFile)]
        if geo_files:
            await _save_uploads(geo_files, pipeline.submission_dir(sid) / "geo")
        for im_meta in meta.get("images") or []:
            f = form.get(im_meta.get("field", ""))
            if not isinstance(f, StarletteUploadFile):
                continue
            data = await f.read()
            filename, mime, w, h = await asyncio.to_thread(
                _store_image, data, f.filename or "image", f.content_type or "")
            corners = im_meta.get("corners")
            if not (isinstance(corners, list) and len(corners) == 4):
                corners = None
            await db.p().execute(
                "INSERT INTO pkdb.images(submission_id, filename, original_name, mime, title, description, width, "
                "height, corners) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                sid, filename, f.filename or filename, mime, (im_meta.get("title") or "")[:300],
                im_meta.get("description") or "", w, h, corners)
    except Exception:
        await db.p().execute("DELETE FROM pkdb.submissions WHERE id=$1", sid)
        await pipeline.delete_submission_files(sid)
        raise
    await pipeline.enqueue(sid)
    return {"id": sid, "status": "queued"}


def _sub_summary(r) -> dict:
    return {"id": r["id"], "created_at": r["created_at"], "updated_at": r["updated_at"], "title": r["title"],
            "text": r["text"][:300], "status": r["status"], "apply_mode": r["apply_mode"], "error": r["error"]}


@app.get("/pkdb/api/submissions")
async def list_submissions(limit: int = 50, status: str | None = None, user=ApiUser):
    if status:
        rows = await db.p().fetch("SELECT * FROM pkdb.submissions WHERE status = ANY($1::text[]) "
                                  "ORDER BY id DESC LIMIT $2", status.split(","), min(limit, 500))
    else:
        rows = await db.p().fetch("SELECT * FROM pkdb.submissions ORDER BY id DESC LIMIT $1", min(limit, 500))
    counts = {r["status"]: r["n"] for r in
              await db.p().fetch("SELECT status, count(*) n FROM pkdb.submissions GROUP BY status")}
    return {"items": [_sub_summary(r) for r in rows], "counts": counts}


@app.get("/pkdb/api/submissions/{sid}")
async def get_submission(sid: int, user=ApiUser):
    sub = await pipeline.load(sid)
    if not sub:
        raise HTTPException(404, "Not found")
    out = _sub_summary(sub)
    out.update(text=sub["text"], source_url=sub["source_url"], feedback=sub["feedback"], result=sub["result"],
               map_id=((sub["meta"] or {}).get("prepared") or {}).get("map_id"))
    return out


class ApplyBody(BaseModel):
    operations: list[dict] | None = None


@app.post("/pkdb/api/submissions/{sid}/apply")
async def apply_submission(sid: int, body: ApplyBody, user=ApiUser):
    sub = await pipeline.load(sid)
    if not sub:
        raise HTTPException(404, "Not found")
    if sub["status"] not in ("review", "failed") or not sub["result"]:
        raise HTTPException(409, f"Submission is {sub['status']}, nothing to apply")
    try:
        return await pipeline.apply(sid, body.operations)
    except wikijs.WikiError as e:
        raise HTTPException(502, str(e)) from e


class RetryBody(BaseModel):
    feedback: str = ""


@app.post("/pkdb/api/submissions/{sid}/retry")
async def retry_submission(sid: int, body: RetryBody, user=ApiUser):
    sub = await pipeline.load(sid)
    if not sub:
        raise HTTPException(404, "Not found")
    if sub["status"] in ("queued", "processing", "applying"):
        raise HTTPException(409, "Submission is already being processed")
    await db.p().execute("UPDATE pkdb.submissions SET feedback=$2, result=NULL WHERE id=$1", sid, body.feedback)
    await pipeline.enqueue(sid)
    return {"id": sid, "status": "queued"}


@app.post("/pkdb/api/submissions/{sid}/reject")
async def reject_submission(sid: int, user=ApiUser):
    await db.p().execute("UPDATE pkdb.submissions SET status='rejected', updated_at=now() WHERE id=$1 "
                         "AND status IN ('review','failed')", sid)
    return {"ok": True}


@app.delete("/pkdb/api/submissions/{sid}")
async def delete_submission(sid: int, user=ApiUser):
    sub = await pipeline.load(sid)
    if not sub:
        raise HTTPException(404, "Not found")
    if sub["status"] in ("queued", "processing", "applying"):
        raise HTTPException(409, "Submission is being processed")
    await db.p().execute("DELETE FROM pkdb.submissions WHERE id=$1", sid)  # maps/images stay (they may be embedded)
    await pipeline.delete_submission_files(sid)
    return {"ok": True}


# --------------------------------------------------------------------------------------------
# Maps (API for the extension, embeds for the wiki)
# --------------------------------------------------------------------------------------------

async def map_data(map_id: int, image_base: str) -> dict:
    m = await db.p().fetchrow("SELECT * FROM pkdb.maps WHERE id=$1", map_id)
    if not m:
        raise HTTPException(404, "Map not found")
    layers = await db.p().fetch("""
        SELECT l.*, i.corners, i.width, i.height, i.title AS image_title
        FROM pkdb.layers l LEFT JOIN pkdb.images i ON i.id = l.image_id
        WHERE l.map_id=$1 ORDER BY l.sort, l.id""", map_id)
    out_layers = []
    for lyr in layers:
        entry = {"id": lyr["id"], "kind": lyr["kind"], "name": lyr["name"], "description": lyr["description"],
                 "style": lyr["style"] or {}}
        if lyr["kind"] == "image":
            if not lyr["image_id"] or not lyr["corners"]:
                continue
            entry["image"] = {"id": lyr["image_id"], "url": f"{image_base}/{lyr['image_id']}",
                              "corners": lyr["corners"], "width": lyr["width"], "height": lyr["height"]}
        else:
            fc = await db.p().fetchval("""
                SELECT json_build_object('type','FeatureCollection','features', coalesce(json_agg(
                  json_build_object('type','Feature','id',f.id,'geometry',ST_AsGeoJSON(f.geom, 7)::json,
                                    'properties', f.props) ORDER BY f.id), '[]'::json))
                FROM pkdb.features f WHERE f.layer_id=$1""", lyr["id"])
            entry["geojson"] = fc
        out_layers.append(entry)
    pages = (await search.pages_embedding_maps([map_id])).get(map_id, [])
    return {"id": map_id, "title": m["title"], "description": m["description"], "layers": out_layers,
            "pages": pages}


@app.get("/pkdb/api/maps/{map_id}")
async def api_map(map_id: int, user=ApiUser):
    return await map_data(map_id, "/pkdb/api/images")


@app.get("/pkdb/api/images/{image_id}")
async def api_image(image_id: int, user=ApiUser):
    return await _image_response(image_id)


@app.get("/pkdb/embed/map/{map_id}/data")
async def embed_map_data(map_id: int, _=EmbedUser):
    return await map_data(map_id, "/pkdb/embed/image")


@app.get("/pkdb/embed/image/{image_id}")
async def embed_image(image_id: int, _=EmbedUser):
    return await _image_response(image_id)


async def _image_response(image_id: int) -> FileResponse:
    row = await db.p().fetchrow("SELECT filename, mime FROM pkdb.images WHERE id=$1", image_id)
    if not row:
        raise HTTPException(404, "Image not found")
    path = Path(settings.data_dir, "images", row["filename"])
    if not path.exists():
        raise HTTPException(404, "Image file missing")
    return FileResponse(path, media_type=row["mime"], headers={"Cache-Control": "private, max-age=86400"})


@app.get("/pkdb/embed/map/{map_id}", response_class=HTMLResponse)
async def embed_map(map_id: int, request: Request):
    try:
        await auth.require_embed(request)
    except HTTPException:
        login = f"{settings.public_url}/login"
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8"><body style="font-family:sans-serif;padding:1em">'
            f'Please <a href="{login}" target="_top">log in</a> to view this map.</body>', status_code=401)
    html = (STATIC / "embed.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__MAP_ID__", str(int(map_id))))


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
