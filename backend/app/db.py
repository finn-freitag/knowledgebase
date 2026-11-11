"""Database pool and schema for the pkdb schema (Wiki.js owns the public schema)."""
from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from .config import settings

log = logging.getLogger("pkdb.db")

pool: asyncpg.Pool | None = None

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS pkdb;

CREATE TABLE IF NOT EXISTS pkdb.state (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS pkdb.api_tokens (
    id           serial PRIMARY KEY,
    token_hash   text UNIQUE NOT NULL,
    name         text NOT NULL DEFAULT '',
    wiki_user    text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);

CREATE TABLE IF NOT EXISTS pkdb.submissions (
    id          serial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL DEFAULT '',
    title       text NOT NULL DEFAULT '',
    text        text NOT NULL,
    source_url  text NOT NULL DEFAULT '',
    apply_mode  text NOT NULL DEFAULT 'review',
    status      text NOT NULL DEFAULT 'queued',   -- queued|processing|review|applying|applied|rejected|failed
    feedback    text NOT NULL DEFAULT '',
    error       text NOT NULL DEFAULT '',
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    result      jsonb
);

CREATE TABLE IF NOT EXISTS pkdb.images (
    id              serial PRIMARY KEY,
    submission_id   int REFERENCES pkdb.submissions(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    filename        text NOT NULL,
    original_name   text NOT NULL,
    mime            text NOT NULL,
    title           text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    width           int,
    height          int,
    corners         jsonb,          -- [[lat,lng] x4] TL, TR, BR, BL  (null = not georeferenced)
    wiki_asset_path text
);

CREATE TABLE IF NOT EXISTS pkdb.maps (
    id            serial PRIMARY KEY,
    submission_id int REFERENCES pkdb.submissions(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    title         text NOT NULL DEFAULT '',
    description   text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pkdb.layers (
    id          serial PRIMARY KEY,
    map_id      int NOT NULL REFERENCES pkdb.maps(id) ON DELETE CASCADE,
    sort        int NOT NULL DEFAULT 0,
    kind        text NOT NULL DEFAULT 'vector',   -- vector|image
    name        text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    source_file text NOT NULL DEFAULT '',
    style       jsonb NOT NULL DEFAULT '{}'::jsonb,
    image_id    int REFERENCES pkdb.images(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pkdb.features (
    id       bigserial PRIMARY KEY,
    layer_id int NOT NULL REFERENCES pkdb.layers(id) ON DELETE CASCADE,
    name     text NOT NULL DEFAULT '',
    props    jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom     geometry(Geometry, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS features_layer_idx ON pkdb.features(layer_id);
CREATE INDEX IF NOT EXISTS features_geom_idx ON pkdb.features USING gist(geom);
CREATE INDEX IF NOT EXISTS layers_map_idx ON pkdb.layers(map_id);
"""


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect() -> None:
    global pool
    delay = 1.0
    while True:
        try:
            pool = await asyncpg.create_pool(settings.dsn, min_size=1, max_size=10, init=_init_conn)
            break
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("Database not ready (%s), retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
    async with pool.acquire() as conn:
        # Serialize schema creation if several workers start at once
        await conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            await conn.execute(SCHEMA_SQL)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(424242)")
    log.info("Database ready")


async def close() -> None:
    if pool:
        await pool.close()


def p() -> asyncpg.Pool:
    assert pool is not None, "database not connected"
    return pool


async def get_state(key: str, default=None):
    row = await p().fetchrow("SELECT value FROM pkdb.state WHERE key=$1", key)
    return row["value"] if row else default


async def set_state(key: str, value) -> None:
    await p().execute(
        "INSERT INTO pkdb.state(key, value) VALUES($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, value,
    )
