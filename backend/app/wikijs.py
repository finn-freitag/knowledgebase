"""Wiki.js 2.x integration: automatic first-run setup, API key provisioning, page CRUD, asset upload."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata

import httpx

from . import db
from .config import settings

log = logging.getLogger("pkdb.wiki")

GUEST_GROUP_ID = 2
_ready = asyncio.Event()
_status = {"state": "starting", "message": ""}


class WikiError(Exception):
    pass


def status() -> dict:
    return dict(_status)


def is_ready() -> bool:
    return _ready.is_set()


async def wait_ready(timeout: float | None = None) -> None:
    await asyncio.wait_for(_ready.wait(), timeout)


# --------------------------------------------------------------------------------------------
# Low level GraphQL
# --------------------------------------------------------------------------------------------

async def graphql(query: str, variables: dict | None = None, token: str | None = None) -> dict:
    """Run a GraphQL request. Uses the stored API key unless a token is given."""
    if token is None:
        token = await db.get_state("wiki_api_key")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{settings.wiki_internal_url}/graphql",
                         json={"query": query, "variables": variables or {}}, headers=headers)
    if r.status_code >= 400:
        raise WikiError(f"Wiki.js GraphQL HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("errors"):
        raise WikiError("Wiki.js GraphQL error: " + "; ".join(e.get("message", "?") for e in data["errors"]))
    return data.get("data") or {}


def _check(result: dict | None, what: str) -> None:
    rr = (result or {}).get("responseResult") or {}
    if not rr.get("succeeded"):
        raise WikiError(f"{what} failed: {rr.get('message') or rr.get('slug') or 'unknown error'}")


async def login(username: str, password: str) -> str:
    """Log in with local credentials, returns a user JWT. Raises WikiError on failure."""
    q = """mutation($u:String!,$p:String!){ authentication { login(username:$u,password:$p,strategy:"local"){
            responseResult{succeeded message slug} jwt mustChangePwd mustProvideTFA mustSetupTFA } } }"""
    d = await graphql(q, {"u": username, "p": password}, token="")
    res = d["authentication"]["login"]
    _check(res, "Login")
    if res.get("mustProvideTFA") or res.get("mustSetupTFA"):
        raise WikiError("Accounts with two-factor authentication cannot be used for API login")
    if not res.get("jwt"):
        raise WikiError("Login did not return a token")
    return res["jwt"]


# --------------------------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------------------------

async def _wiki_tables_exist() -> bool:
    return bool(await db.p().fetchval("SELECT to_regclass('public.settings') IS NOT NULL"))


async def _setup_done() -> bool:
    return bool(await db.p().fetchval("SELECT count(*) FROM public.settings WHERE key='certs'"))


async def _wait_http(path: str, timeout: float = 300) -> None:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(settings.wiki_internal_url + path)
                if r.status_code < 500:
                    return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(3)
    raise WikiError(f"Wiki.js not reachable at {settings.wiki_internal_url}")


async def _wait_graphql(timeout: float = 300) -> None:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(settings.wiki_internal_url + "/graphql", json={"query": "{__typename}"})
                if r.status_code == 200 and "data" in r.json():
                    return
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(3)
    raise WikiError("Wiki.js GraphQL endpoint did not come up")


async def _run_setup() -> None:
    if not settings.wiki_admin_password or len(settings.wiki_admin_password) < 8:
        raise WikiError("WIKI_ADMIN_PASSWORD must be set (at least 8 characters) for the automatic setup")
    log.info("Wiki.js is in setup mode, running automatic setup for %s", settings.wiki_admin_email)
    body = {
        "adminEmail": settings.wiki_admin_email,
        "adminPassword": settings.wiki_admin_password,
        "adminPasswordConfirm": settings.wiki_admin_password,
        "siteUrl": settings.public_url,
        "telemetry": False,
    }
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(settings.wiki_internal_url + "/finalize", json=body)
    data = r.json()
    if not data.get("ok"):
        raise WikiError(f"Wiki.js setup failed: {data.get('error')}")
    log.info("Wiki.js setup finished, waiting for restart")
    await asyncio.sleep(5)


async def _validate_key(key: str) -> bool:
    try:
        await graphql("{ site { config { title } } }", token=key)
        return True
    except WikiError:
        return False


async def _provision_api_key() -> None:
    key = await db.get_state("wiki_api_key")
    if key and await _validate_key(key):
        return
    log.info("Provisioning a Wiki.js API key for the backend")
    jwt = await login(settings.wiki_admin_email, settings.wiki_admin_password)
    d = await graphql("mutation{ authentication { setApiState(enabled:true){ responseResult{succeeded message} } } }",
                      token=jwt)
    _check(d["authentication"]["setApiState"], "Enabling the Wiki.js API")
    d = await graphql(
        """mutation{ authentication { createApiKey(name:"pkdb-backend", expiration:"20y", fullAccess:true){
             responseResult{succeeded message} key } } }""", token=jwt)
    res = d["authentication"]["createApiKey"]
    _check(res, "Creating the Wiki.js API key")
    await db.set_state("wiki_api_key", res["key"])
    # API keys are reloaded asynchronously in Wiki.js; give it a moment
    for _ in range(10):
        if await _validate_key(res["key"]):
            return
        await asyncio.sleep(1)
    raise WikiError("New Wiki.js API key is not accepted")


async def _configure() -> None:
    applied = await db.get_state("wiki_configured", {}) or {}

    if not applied.get("base"):
        # Allow our map iframes in rendered pages (only the iframe tag; HTML is still sanitized)
        cfg = [{"key": k, "value": json.dumps({"v": True})} for k in ("safeHTML", "allowDrawIoUnsafe", "allowIFrames")]
        d = await graphql("""mutation($r:[RendererInput]){ rendering { updateRenderers(renderers:$r){
                             responseResult{succeeded message} } } }""",
                          {"r": [{"key": "htmlSecurity", "isEnabled": True, "config": cfg}]})
        _check(d["rendering"]["updateRenderers"], "Allowing iframes")
        d = await graphql("""mutation($t:String,$s:Int){ site { updateConfig(title:$t, uploadMaxFileSize:$s){
                             responseResult{succeeded message} } } }""",
                          {"t": settings.wiki_title, "s": settings.max_upload_mb * 1024 * 1024})
        _check(d["site"]["updateConfig"], "Updating site config")
        applied["base"] = True
        await db.set_state("wiki_configured", applied)

    if applied.get("public") != settings.wiki_public:
        roles = ["read:pages", "read:assets", "read:comments"]
        rules = [{"id": "guest", "roles": roles, "match": "START", "deny": False, "path": "", "locales": []}]
        d = await graphql("""mutation($p:[String]!,$r:[PageRuleInput]!){ groups { update(id:2, name:"Guests",
                             redirectOnLogin:"/", permissions:$p, pageRules:$r){ responseResult{succeeded message} } } }""",
                          {"p": roles if settings.wiki_public else [], "r": rules if settings.wiki_public else []})
        _check(d["groups"]["update"], "Updating guest permissions")
        applied["public"] = settings.wiki_public
        await db.set_state("wiki_configured", applied)
        log.info("Guest read access %s", "enabled" if settings.wiki_public else "disabled")


async def media_folder_id() -> int:
    fid = await db.get_state("wiki_media_folder_id")
    if fid:
        return int(fid)
    slug = settings.wiki_media_folder

    async def find() -> int | None:
        d = await graphql("{ assets { folders(parentFolderId:0){ id slug } } }")
        for f in d["assets"]["folders"] or []:
            if f["slug"] == slug:
                return f["id"]
        return None

    fid = await find()
    if fid is None:
        d = await graphql("""mutation($s:String!){ assets { createFolder(parentFolderId:0, slug:$s, name:$s){
                             responseResult{succeeded message} } } }""", {"s": slug})
        _check(d["assets"]["createFolder"], "Creating asset folder")
        fid = await find()
    if fid is None:
        raise WikiError("Could not create asset folder")
    await db.set_state("wiki_media_folder_id", fid)
    return fid


async def bootstrap() -> None:
    """Runs in the background until Wiki.js is set up and usable. Retries forever on errors."""
    delay = 5
    while True:
        try:
            _status.update(state="waiting", message="Waiting for Wiki.js")
            await _wait_http("/healthz")
            while not await _wiki_tables_exist():
                await asyncio.sleep(3)
            if not await _setup_done():
                _status.update(state="setup", message="Running Wiki.js setup")
                await _run_setup()
            await _wait_graphql()
            await _provision_api_key()
            await _configure()
            await media_folder_id()
            _status.update(state="ready", message="")
            _ready.set()
            log.info("Wiki.js integration ready")
            return
        except Exception as e:  # noqa: BLE001
            log.error("Wiki.js bootstrap failed: %s (retrying in %ss)", e, delay)
            _status.update(state="error", message=str(e))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


# --------------------------------------------------------------------------------------------
# Pages & assets
# --------------------------------------------------------------------------------------------

PAGE_FIELDS = "id path title description content locale scriptCss scriptJs tags { tag title } updatedAt"


async def get_page_by_path(path: str, locale: str | None = None) -> dict | None:
    page_id = await db.p().fetchval(
        'SELECT id FROM public.pages WHERE path=$1 AND "localeCode"=$2', path, locale or settings.wiki_locale)
    if page_id is None:
        return None
    return await get_page(page_id)


async def get_page(page_id: int) -> dict | None:
    q = f"query($i:Int!){{ pages {{ single(id:$i){{ {PAGE_FIELDS} }} }} }}"
    try:
        d = await graphql(q, {"i": page_id})
    except WikiError:
        return None
    return d["pages"]["single"]


async def create_page(path: str, title: str, content: str, description: str = "", tags: list[str] | None = None) -> dict:
    q = """mutation($c:String!,$d:String!,$l:String!,$p:String!,$t:[String]!,$ti:String!){ pages { create(
            content:$c, description:$d, editor:"markdown", isPublished:true, isPrivate:false, locale:$l, path:$p,
            tags:$t, title:$ti){ responseResult{succeeded message} page{ id path } } } }"""
    d = await graphql(q, {"c": content, "d": description or "", "l": settings.wiki_locale, "p": path,
                          "t": tags or [], "ti": title})
    res = d["pages"]["create"]
    _check(res, f"Creating page {path}")
    return res["page"]


async def update_page(page: dict, content: str, title: str | None = None, description: str | None = None,
                      tags: list[str] | None = None) -> None:
    """Update a page. Wiki.js overwrites every field on update, so unchanged values are passed through."""
    q = """mutation($i:Int!,$c:String,$ti:String,$d:String,$t:[String],$css:String,$js:String){ pages { update(
            id:$i, content:$c, title:$ti, description:$d, tags:$t, scriptCss:$css, scriptJs:$js, isPublished:true){
            responseResult{succeeded message} } } }"""
    d = await graphql(q, {
        "i": page["id"], "c": content,
        "ti": title or page["title"],
        "d": description if description is not None else (page.get("description") or ""),
        "t": tags if tags is not None else [t["tag"] for t in page.get("tags") or []],
        "css": page.get("scriptCss") or "", "js": page.get("scriptJs") or "",
    })
    _check(d["pages"]["update"], f"Updating page {page['path']}")


async def upload_asset(filename: str, data: bytes, mime: str) -> str:
    """Upload a file into the media folder. Returns the public path, e.g. /pkdb-media/foo.jpg"""
    fid = await media_folder_id()
    key = await db.get_state("wiki_api_key")
    # Field order matters: Wiki.js reads the folder JSON before the file part
    files = [
        ("mediaUpload", (None, json.dumps({"folderId": fid}))),
        ("mediaUpload", (filename, data, mime)),
    ]
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(settings.wiki_internal_url + "/u", files=files,
                         headers={"Authorization": f"Bearer {key}"})
    if r.status_code >= 400:
        raise WikiError(f"Asset upload failed: HTTP {r.status_code} {r.text[:200]}")
    # Wiki.js lowercases and sanitizes file names
    return f"/{settings.wiki_media_folder}/{sanitize_asset_name(filename)}"


def sanitize_asset_name(name: str) -> str:
    # Mirrors Wiki.js: lowercase, whitespace/,;# to "_", then sanitize-filename
    name = re.sub(r"[\s,;#]+", "_", name.lower())
    name = re.sub(r'[/\\?<>:*|"\x00-\x1f\x80-\x9f]', "", name)
    return name


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text)
    repl = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"}
    text = "".join(repl.get(ch, ch) for ch in unicodedata.normalize("NFC", text))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-") or "page"


RESERVED_FIRST = {"login", "logout", "register", "verify", "favicons", "fonts", "img", "js", "svg", "pkdb",
                  "graphql", "healthz", "u", "e", "h", "i", "p", "s", "t", "a"}


def normalize_path(path: str) -> str:
    parts = [slugify(seg) for seg in re.split(r"/+", path.strip().strip("/")) if seg.strip()]
    if not parts:
        return "page"
    first = parts[0]
    # Wiki.js treats short first segments (len<=1) and locale-like segments (xx, xx-yy) as reserved
    if (first in RESERVED_FIRST or first == settings.wiki_media_folder or len(first) <= 2
            or re.fullmatch(r"[a-z]{2}-[a-z]{2}", first)):
        parts[0] = "wiki-" + first
    return "/".join(parts)
