"""Authentication.

* API (/pkdb/api/*): always requires a bearer token. Tokens are issued by logging in with a Wiki.js account
  (POST /pkdb/api/auth/login) or configured statically via PKDB_API_TOKENS.
* Embeds (/pkdb/embed/*, map iframes inside wiki pages): public if WIKI_PUBLIC=true, protected by Authelia
  (in front, via Caddy) if AUTH_MODE=authelia, otherwise the Wiki.js session cookie ("jwt") is verified.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings

_cert_cache: dict = {"pem": None, "at": 0.0}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_token(name: str, wiki_user: str) -> str:
    token = "pkdb_" + secrets.token_urlsafe(32)
    await db.p().execute(
        "INSERT INTO pkdb.api_tokens(token_hash, name, wiki_user) VALUES($1,$2,$3)",
        hash_token(token), name, wiki_user,
    )
    return token


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


async def _check_api_token(token: str) -> dict | None:
    for st in settings.static_api_tokens:
        if hmac.compare_digest(st, token):
            return {"user": "static-token", "token_id": None}
    row = await db.p().fetchrow(
        "UPDATE pkdb.api_tokens SET last_used_at=now() WHERE token_hash=$1 RETURNING id, wiki_user",
        hash_token(token),
    )
    if row:
        return {"user": row["wiki_user"], "token_id": row["id"]}
    return None


async def require_api(request: Request) -> dict:
    token = _bearer(request)
    if not token:
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    principal = await _check_api_token(token)
    if not principal:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return principal


async def _wiki_public_cert() -> str | None:
    now = time.time()
    if _cert_cache["pem"] and now - _cert_cache["at"] < 300:
        return _cert_cache["pem"]
    try:
        val = await db.p().fetchval("SELECT value FROM public.settings WHERE key='certs'")
    except Exception:  # noqa: BLE001 - wiki tables may not exist yet
        return None
    if isinstance(val, dict) and val.get("public"):
        _cert_cache.update(pem=val["public"], at=now)
        return val["public"]
    return None


async def _check_wiki_cookie(request: Request) -> bool:
    token = request.cookies.get("jwt")
    if not token:
        return False
    pem = await _wiki_public_cert()
    if not pem:
        return False
    try:
        # Wiki.js tokens expire after 30 min but are silently renewed by the wiki for 14 days.
        payload = pyjwt.decode(
            token, pem, algorithms=["RS256"], audience="urn:wiki.js", issuer="urn:wiki.js",
            leeway=14 * 24 * 3600,
        )
    except pyjwt.PyJWTError:
        return False
    # Guests never get a cookie; any valid user session may view embeds.
    return bool(payload.get("id"))


async def require_embed(request: Request) -> None:
    if settings.wiki_public or settings.auth_mode == "authelia":
        return
    if await _check_wiki_cookie(request):
        return
    token = _bearer(request)
    if token and await _check_api_token(token):
        return
    raise HTTPException(401, "Please log in to the wiki first")


ApiUser = Depends(require_api)
EmbedUser = Depends(require_embed)
