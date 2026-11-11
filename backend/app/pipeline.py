"""Submission processing: store geodata/images, ask the LLM how to extend the wiki, review/apply the result."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import traceback
from pathlib import Path

from . import db, geo, llm, search, wikijs
from .config import settings

log = logging.getLogger("pkdb.pipeline")

_queue: asyncio.Queue[int] = asyncio.Queue()
_worker_task: asyncio.Task | None = None

PLACEHOLDER_RE = re.compile(r"\{\{\s*(map|image)\s*:\s*(\d+)\s*\}\}")
LANG_NAMES = {"de": "German", "en": "English", "fr": "French", "nl": "Dutch", "it": "Italian", "es": "Spanish",
              "pl": "Polish", "da": "Danish", "sv": "Swedish", "cs": "Czech", "pt": "Portuguese"}


def language_name() -> str:
    code = settings.wiki_language.lower()
    return LANG_NAMES.get(code[:2], settings.wiki_language)


# --------------------------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------------------------

async def start_worker() -> None:
    global _worker_task
    # Re-queue jobs interrupted by a restart
    rows = await db.p().fetch("SELECT id FROM pkdb.submissions WHERE status IN ('queued','processing') ORDER BY id")
    for r in rows:
        await _queue.put(r["id"])
    _worker_task = asyncio.create_task(_worker())


async def enqueue(submission_id: int) -> None:
    await db.p().execute("UPDATE pkdb.submissions SET status='queued', error='', updated_at=now() WHERE id=$1",
                         submission_id)
    await _queue.put(submission_id)


async def _worker() -> None:
    while True:
        sid = await _queue.get()
        try:
            await wikijs.wait_ready()
            await process(sid)
        except Exception as e:  # noqa: BLE001
            log.error("Submission %s failed: %s\n%s", sid, e, traceback.format_exc())
            await db.p().execute("UPDATE pkdb.submissions SET status='failed', error=$2, updated_at=now() WHERE id=$1",
                                 sid, str(e)[:2000])
        finally:
            _queue.task_done()


async def _set_status(sid: int, status: str, **extra) -> None:
    sets, args = ["status=$2", "updated_at=now()"], [sid, status]
    for k, v in extra.items():
        args.append(v)
        sets.append(f"{k}=${len(args)}")
    await db.p().execute(f"UPDATE pkdb.submissions SET {', '.join(sets)} WHERE id=$1", *args)


# --------------------------------------------------------------------------------------------
# Step 1: store attachments (once per submission)
# --------------------------------------------------------------------------------------------

def submission_dir(sid: int) -> Path:
    return geo.ensure_dir("submissions", str(sid))


async def prepare_attachments(sid: int, sub: dict) -> dict:
    meta = sub["meta"] or {}
    if meta.get("prepared"):
        return meta["prepared"]

    sdir = submission_dir(sid)
    geo_dir = sdir / "geo"
    geo_files = sorted(geo_dir.iterdir()) if geo_dir.exists() else []
    datasets = await asyncio.to_thread(geo.convert_files, geo_files) if geo_files else []
    file_meta = {g.get("key"): g for g in meta.get("geofiles", [])}
    drawn = (meta.get("drawn") or {}).get("features") or []
    images = await db.p().fetch("SELECT * FROM pkdb.images WHERE submission_id=$1 ORDER BY id", sid)
    geo_images = [im for im in images if im["corners"]]
    warnings = [f"{d.key}: {d.error}" for d in datasets if d.error]

    map_id = None
    if any(d.layers for d in datasets) or drawn or geo_images:
        title = meta.get("map_title") or sub["title"] or _first_line(sub["text"])
        async with db.p().acquire() as conn, conn.transaction():
            map_id = await conn.fetchval(
                "INSERT INTO pkdb.maps(submission_id, title, description) VALUES($1,$2,$3) RETURNING id",
                sid, title[:300], meta.get("map_description", ""))
            sort = 0
            for im in geo_images:
                await conn.execute(
                    "INSERT INTO pkdb.layers(map_id, sort, kind, name, description, image_id) "
                    "VALUES($1,$2,'image',$3,$4,$5)",
                    map_id, sort, im["title"] or im["original_name"], im["description"], im["id"])
                sort += 1
            for d in datasets:
                fm = file_meta.get(d.key, {})
                for lyr in d.layers:
                    name = fm.get("title") or d.key
                    if len(d.layers) > 1:
                        name = f"{name} – {lyr.name}"
                    lid = await conn.fetchval(
                        "INSERT INTO pkdb.layers(map_id, sort, kind, name, description, source_file) "
                        "VALUES($1,$2,'vector',$3,$4,$5) RETURNING id",
                        map_id, sort, name, fm.get("description", ""), ", ".join(d.files))
                    sort += 1
                    await _insert_features(conn, lid, lyr.features)
            if drawn:
                lid = await conn.fetchval(
                    "INSERT INTO pkdb.layers(map_id, sort, kind, name, description) VALUES($1,$2,'vector',$3,$4) "
                    "RETURNING id",
                    map_id, sort, meta.get("drawn_title") or "Drawn shapes", meta.get("drawn_description", ""))
                await _insert_features(conn, lid, drawn)

    # Upload images to the wiki media folder (fallback: serve them via the embed endpoint)
    for im in images:
        if im["wiki_asset_path"]:
            continue
        path = Path(settings.data_dir, "images", im["filename"])
        try:
            asset_path = await wikijs.upload_asset(im["filename"], path.read_bytes(), im["mime"])
        except Exception as e:  # noqa: BLE001
            log.warning("Uploading image %s to Wiki.js failed: %s", im["id"], e)
            warnings.append(f"Image {im['original_name']} could not be uploaded to the wiki, using direct link")
            asset_path = f"/pkdb/embed/image/{im['id']}"
        await db.p().execute("UPDATE pkdb.images SET wiki_asset_path=$2 WHERE id=$1", im["id"], asset_path)

    prepared = {"map_id": map_id, "image_ids": [im["id"] for im in images], "warnings": warnings}
    meta["prepared"] = prepared
    await db.p().execute("UPDATE pkdb.submissions SET meta=$2 WHERE id=$1", sid, meta)
    return prepared


async def _insert_features(conn, layer_id: int, features: list[dict]) -> None:
    rows = []
    for ft in features:
        g = ft.get("geometry")
        if not g:
            continue
        props = ft.get("properties") or {}
        rows.append((layer_id, geo.feature_name(props), json.dumps(props, default=str), json.dumps(g)))
    if not rows:
        return
    await conn.executemany(
        "INSERT INTO pkdb.features(layer_id, name, props, geom) "
        "VALUES($1, $2, $3::text::jsonb, ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON($4), 4326)))",
        rows,
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip(" #*\t")
        if line:
            return line[:120]
    return "Untitled"


# --------------------------------------------------------------------------------------------
# Step 2: LLM
# --------------------------------------------------------------------------------------------

async def _attachment_summary(prepared: dict) -> str:
    parts = []
    if prepared.get("map_id"):
        mid = prepared["map_id"]
        m = await db.p().fetchrow("SELECT title FROM pkdb.maps WHERE id=$1", mid)
        layers = await db.p().fetch("""
            SELECT l.id, l.kind, l.name, l.description, count(f.id) AS n,
                   array_remove(array_agg(DISTINCT GeometryType(f.geom)), NULL) AS types,
                   (array_remove(array_agg(f.name ORDER BY f.id), ''))[1:15] AS names
            FROM pkdb.layers l LEFT JOIN pkdb.features f ON f.layer_id = l.id
            WHERE l.map_id=$1 GROUP BY l.id ORDER BY l.sort""", mid)
        ext = await db.p().fetchrow("""
            SELECT ST_XMin(e) x1, ST_YMin(e) y1, ST_XMax(e) x2, ST_YMax(e) y2 FROM (
              SELECT ST_Extent(f.geom) e FROM pkdb.features f JOIN pkdb.layers l ON l.id=f.layer_id WHERE l.map_id=$1) s""",
            mid)
        lines = [f'- MAP {{{{map:{mid}}}}} "{m["title"]}" – an interactive map containing these layers:']
        for lyr in layers:
            if lyr["kind"] == "image":
                lines.append(f'  - georeferenced image overlay "{lyr["name"]}": {lyr["description"]}')
            else:
                names = ", ".join(n for n in (lyr["names"] or []) if n)
                lines.append(f'  - layer "{lyr["name"]}" ({lyr["n"]} features: {", ".join(lyr["types"] or [])})'
                             + (f' – {lyr["description"]}' if lyr["description"] else "")
                             + (f" – feature names: {names}" if names else ""))
        if ext and ext["x1"] is not None:
            lines.append(f"  - bounding box (lon/lat): {ext['x1']:.5f},{ext['y1']:.5f} – {ext['x2']:.5f},{ext['y2']:.5f}")
        parts.append("\n".join(lines))
    if prepared.get("image_ids"):
        rows = await db.p().fetch("SELECT * FROM pkdb.images WHERE id = ANY($1::int[]) ORDER BY id",
                                  prepared["image_ids"])
        for im in rows:
            parts.append(f'- IMAGE {{{{image:{im["id"]}}}}} "{im["title"] or im["original_name"]}"'
                         + (f": {im['description']}" if im["description"] else "")
                         + (" (also shown georeferenced on the map)" if im["corners"] else ""))
    return "\n".join(parts) if parts else "(none)"


SELECT_SYSTEM = """You maintain a personal knowledge wiki about a town: local politics, councils and committees,
nature conservation, places, projects, people and organisations. You decide which EXISTING wiki pages are
related to new information. Answer with JSON only: {"related": ["path1", "path2"]} – at most %d paths,
most relevant first, only paths from the given list. Return an empty list if nothing is related."""

WRITE_SYSTEM = """You maintain a personal knowledge wiki (Wiki.js, Markdown) about a town: local politics, councils
and committees, nature conservation, places, projects, people and organisations.

You receive NEW INFORMATION from the user and some EXISTING PAGES. Integrate the new information into the wiki:
- Prefer extending existing pages when the topic fits. Create new pages for new topics (places, projects,
  organisations, decisions, ...). Several operations are allowed, e.g. update one page and create another.
- Write in %(lang)s. Use clean Markdown with headings (##), lists and tables where useful. Never invent facts.
  Keep dates, numbers, names and sources exact. Mention the source URL if one is given.
- For "update" you must return the COMPLETE new page content, keeping all existing information unless it is
  clearly outdated or contradicted (then say so). Pages marked TRUNCATED may only receive "append" operations,
  whose content is added at the end of the page.
- Attachments: embed every attachment exactly once using its placeholder on its own line, e.g. {{map:12}} or
  {{image:5}}, at the most fitting position (usually near the text describing the place). Do not write image or
  iframe markup yourself.
- Paths: lowercase, words separated by "-", optional hierarchy with "/", e.g. "naturschutz/streuobstwiese-nord".
  Reuse the existing hierarchy where it fits.
- Cross-link related pages with Markdown links like [Title](/path).

Answer with ONE JSON object only:
{"summary": "one or two sentences describing the changes",
 "operations": [
   {"action": "create" | "update" | "append", "path": "...", "title": "...", "description": "short summary",
    "tags": ["..."], "content": "markdown"}
 ]}"""


async def run_llm(sid: int, sub: dict, prepared: dict) -> dict:
    text = sub["text"]
    attach = await _attachment_summary(prepared)
    query_text = " ".join([sub["title"] or "", text, attach])
    candidates = await search.related_pages_for_text(query_text, limit=60)

    chosen: list[str] = []
    if candidates:
        index = "\n".join(f'- {c["path"]} | {c["title"]} | {(c["description"] or "")[:150]}' for c in candidates)
        user = (f"NEW INFORMATION:\nTitle: {sub['title'] or '-'}\n{text[:6000]}\n\nAttachments:\n{attach}\n\n"
                f"EXISTING PAGES (path | title | description):\n{index}")
        try:
            res = await llm.chat_json(SELECT_SYSTEM % settings.llm_max_related_pages, user)
            valid = {c["path"] for c in candidates}
            chosen = [p for p in res.get("related", []) if isinstance(p, str) and p in valid]
        except llm.LLMError as e:
            log.warning("Page selection failed (%s), using search ranking", e)
            chosen = [c["path"] for c in candidates[:3]]
    chosen = chosen[: settings.llm_max_related_pages]

    pages = []
    for path in chosen:
        pg = await wikijs.get_page_by_path(path)
        if pg:
            pages.append(pg)

    page_txt = []
    for pg in pages:
        content = pg["content"] or ""
        trunc = len(content) > settings.llm_context_chars
        shown = content[: settings.llm_context_chars]
        tags = ", ".join(t["tag"] for t in pg.get("tags") or [])
        page_txt.append(f"=== PAGE path={pg['path']} title=\"{pg['title']}\" tags=[{tags}]"
                        f"{' TRUNCATED' if trunc else ''} ===\n{shown}\n=== END PAGE ===")
    other = [c for c in candidates if c["path"] not in chosen][:40]
    other_txt = "\n".join(f"- /{c['path']} ({c['title']})" for c in other)

    extra = settings.llm_extra_instructions
    feedback = sub.get("feedback") or ""
    user = "\n\n".join(filter(None, [
        f"NEW INFORMATION\nTitle: {sub['title'] or '-'}\nSource: {sub['source_url'] or '-'}\n\n{text}",
        f"ATTACHMENTS (use the placeholders):\n{attach}",
        "EXISTING PAGES:\n" + ("\n\n".join(page_txt) if page_txt else "(no related pages found)"),
        f"OTHER EXISTING PAGES you may link to:\n{other_txt}" if other_txt else "",
        f"ADDITIONAL INSTRUCTIONS:\n{extra}" if extra else "",
        f"FEEDBACK ON A PREVIOUS ATTEMPT (follow it):\n{feedback}" if feedback else "",
    ]))
    system = WRITE_SYSTEM % {"lang": language_name()}
    result = await llm.chat_json(system, user)
    return await postprocess(result, sub, prepared, {p["path"]: p for p in pages})


def _expand(content: str, maps: dict[int, str], images: dict[int, dict]) -> str:
    def repl(m: re.Match) -> str:
        kind, num = m.group(1), int(m.group(2))
        if kind == "map":
            title = maps.get(num, "Map").replace('"', "'")
            return (f'\n\n<iframe src="/pkdb/embed/map/{num}" title="{title}" class="pkdb-map" width="100%" '
                    f'height="450" style="border:0;border-radius:4px" allowfullscreen></iframe>\n\n')
        im = images.get(num)
        if not im:
            return ""
        alt = (im["title"] or im["original_name"]).replace("]", ")").replace("[", "(")
        return f'![{alt}]({im["wiki_asset_path"] or f"/pkdb/embed/image/{num}"})'
    out = PLACEHOLDER_RE.sub(repl, content)
    return re.sub(r"\n{4,}", "\n\n\n", out)


async def postprocess(result: dict, sub: dict, prepared: dict, known_pages: dict[str, dict]) -> dict:
    ops_in = result.get("operations") or []
    if not isinstance(ops_in, list):
        ops_in = []
    map_titles = {}
    if prepared.get("map_id"):
        map_titles[prepared["map_id"]] = await db.p().fetchval("SELECT title FROM pkdb.maps WHERE id=$1",
                                                               prepared["map_id"]) or "Map"
    images = {r["id"]: dict(r) for r in await db.p().fetch(
        "SELECT * FROM pkdb.images WHERE id = ANY($1::int[])", prepared.get("image_ids") or [])}

    ops: list[dict] = []
    for raw in ops_in:
        if not isinstance(raw, dict) or not raw.get("content") or not raw.get("path"):
            continue
        action = str(raw.get("action", "create")).lower()
        if action not in ("create", "update", "append"):
            action = "create"
        path = wikijs.normalize_path(str(raw["path"]))
        title = str(raw.get("title") or path.split("/")[-1].replace("-", " ").title())[:250]
        existing = known_pages.get(path) or await wikijs.get_page_by_path(path)
        if existing and action == "create":
            action = "append"
        if not existing and action in ("update", "append"):
            action = "create"
        if existing and action == "update" and len(existing["content"] or "") > settings.llm_context_chars:
            action = "append"  # the LLM did not see the whole page
        tags = [str(t)[:60] for t in (raw.get("tags") or []) if str(t).strip()][:15]
        ops.append({
            "action": action, "path": path, "title": title,
            "description": str(raw.get("description") or "")[:500],
            "tags": tags, "content": str(raw["content"]),
            "page_id": existing["id"] if existing else None,
            "old_content": existing["content"] if existing else "",
            "old_title": existing["title"] if existing else "",
            "old_tags": [t["tag"] for t in existing.get("tags") or []] if existing else [],
        })

    if not ops:
        # Fallback: store the raw information as a new page
        title = sub["title"] or _first_line(sub["text"])
        ops.append({"action": "create", "path": wikijs.normalize_path("inbox/" + title), "title": title,
                    "description": "", "tags": ["inbox"], "content": sub["text"], "page_id": None,
                    "old_content": "", "old_title": "", "old_tags": []})

    # Make sure every attachment is embedded somewhere
    used = {(k, int(n)) for op in ops for k, n in PLACEHOLDER_RE.findall(op["content"])}
    missing = []
    if prepared.get("map_id") and ("map", prepared["map_id"]) not in used:
        missing.append(f"{{{{map:{prepared['map_id']}}}}}")
    missing += [f"{{{{image:{i}}}}}" for i in prepared.get("image_ids") or [] if ("image", i) not in used]
    if missing:
        ops[0]["content"] = ops[0]["content"].rstrip() + "\n\n" + "\n\n".join(missing) + "\n"

    for i, op in enumerate(ops):
        op["id"] = i
        op["content"] = _expand(op["content"], map_titles, images)
        if op["action"] == "append":
            op["new_content"] = (op["old_content"] or "").rstrip() + "\n\n" + op["content"].strip() + "\n"
        else:
            op["new_content"] = op["content"]
    return {"summary": str(result.get("summary") or "")[:2000], "operations": ops,
            "warnings": prepared.get("warnings", [])}


# --------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------

async def load(sid: int) -> dict | None:
    row = await db.p().fetchrow("SELECT * FROM pkdb.submissions WHERE id=$1", sid)
    return dict(row) if row else None


async def process(sid: int) -> None:
    sub = await load(sid)
    if not sub or sub["status"] not in ("queued", "processing"):
        return
    await _set_status(sid, "processing")
    prepared = await prepare_attachments(sid, sub)
    result = await run_llm(sid, sub, prepared)
    await _set_status(sid, "review", result=result)
    if sub["apply_mode"] == "auto":
        await apply(sid, None)


async def apply(sid: int, edited_ops: list[dict] | None) -> dict:
    """Apply proposed operations. edited_ops (from the review UI) may change path/title/content/tags and
    carries "accepted": bool. None = apply everything as proposed."""
    sub = await load(sid)
    if not sub or not sub["result"]:
        raise ValueError("Nothing to apply")
    result = sub["result"]
    proposed = {op["id"]: op for op in result["operations"]}
    if edited_ops is None:
        todo = list(proposed.values())
    else:
        todo = []
        for e in edited_ops:
            if not e.get("accepted", True) or e.get("id") not in proposed:
                continue
            op = dict(proposed[e["id"]])
            for k in ("path", "title", "description", "tags", "new_content"):
                if k in e and e[k] is not None:
                    op[k] = e[k]
            todo.append(op)
    await _set_status(sid, "applying")
    applied = []
    for op in todo:
        path = wikijs.normalize_path(op["path"])
        entry = {"id": op["id"], "path": path, "title": op["title"], "url": f"{settings.public_url}/{path}"}
        try:
            existing = await wikijs.get_page_by_path(path)
            if op["action"] == "append" and existing:
                # Re-read the page: it may have changed since the proposal was made
                content = (existing["content"] or "").rstrip() + "\n\n" + op["content"].strip() + "\n"
                if edited_ops is not None and op.get("new_content") != proposed[op["id"]].get("new_content"):
                    content = op["new_content"]
            else:
                content = op["new_content"]
            if existing:
                tags = sorted(set(op.get("tags") or []) | {t["tag"] for t in existing.get("tags") or []})
                await wikijs.update_page(existing, content, title=op["title"],
                                         description=op.get("description") or existing["description"], tags=tags)
                entry["action"] = "updated"
            else:
                await wikijs.create_page(path, op["title"], content, op.get("description", ""), op.get("tags"))
                entry["action"] = "created"
            entry["ok"] = True
        except Exception as e:  # noqa: BLE001
            log.error("Applying %s failed: %s", path, e)
            entry.update(ok=False, error=str(e))
        applied.append(entry)
    result["applied"] = applied
    ok = all(a["ok"] for a in applied)
    await _set_status(sid, "applied" if ok else "failed", result=result,
                      error="" if ok else "Some pages could not be saved")
    return result


async def delete_submission_files(sid: int) -> None:
    shutil.rmtree(Path(settings.data_dir, "submissions", str(sid)), ignore_errors=True)
