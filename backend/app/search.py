"""Full text search over wiki pages (Wiki.js tables, same database) and map data (pkdb schema)."""
from __future__ import annotations

import html
import math
import re

from . import db
from .config import settings

MAP_EMBED_RE = re.compile(r"/pkdb/embed/map/(\d+)")


async def query_lexemes(q: str) -> list[str]:
    """Normalize a search query into stemmed lexemes (stop words removed) using the wiki language."""
    rows = await db.p().fetch(
        "SELECT DISTINCT lexeme FROM unnest(to_tsvector($1::regconfig, $2))", settings.ts_config, q[:500]
    )
    return [r["lexeme"] for r in rows][:12]


def _min_hits(n: int) -> int:
    return max(1, math.ceil(n * settings.search_min_match))


def _clean_snippet(s: str) -> str:
    s = s or ""
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)            # markdown links/images -> text
    s = re.sub(r"[#*_`|]+|^\s*>|(?<=\s)>", " ", s)                  # markdown syntax
    s = html.escape(s)
    s = s.replace("&lt;&lt;", "<mark>").replace("&gt;&gt;", "</mark>")
    return re.sub(r"\s+", " ", s).strip()


_LEX_MATCH = "ARRAY(SELECT l FROM unnest($1::text[]) l WHERE {tsv} @@ to_tsquery('simple', quote_literal(l)))"
_OR_QUERY = "to_tsquery('simple', array_to_string(ARRAY(SELECT quote_literal(l) FROM unnest($1::text[]) l), ' | '))"


async def search_pages(lexemes: list[str], limit: int) -> list[dict]:
    cfg = settings.ts_config
    tsv = (
        f"(setweight(to_tsvector('{cfg}', coalesce(p.title,'')), 'A') || "
        f"setweight(to_tsvector('{cfg}', coalesce(p.description,'')), 'B') || "
        f"setweight(to_tsvector('{cfg}', regexp_replace(coalesce(p.content,''), '<[^>]+>', ' ', 'g')), 'C'))"
    )
    sql = f"""
      WITH d AS (
        SELECT p.id, p.path, p.title, p.description, p.content, p."localeCode" AS locale, p."updatedAt" AS updated,
               {tsv} AS tsv
        FROM public.pages p
        WHERE p."isPublished" = true
      ), s AS (
        SELECT d.*, {_LEX_MATCH.format(tsv='d.tsv')} AS matched,
               ts_rank_cd(d.tsv, {_OR_QUERY}) AS rank
        FROM d WHERE d.tsv @@ {_OR_QUERY}
      )
      SELECT id, path, title, description, locale, updated, matched, rank,
             ts_headline('{cfg}', regexp_replace(content, '<[^>]+>', ' ', 'g'), {_OR_QUERY},
               'MaxFragments=2, MaxWords=24, MinWords=8, StartSel=<<, StopSel=>>, FragmentDelimiter=" … "') AS snippet,
             ARRAY(SELECT DISTINCT (regexp_matches(content, '/pkdb/embed/map/([0-9]+)', 'g'))[1]::int) AS maps
      FROM s
      WHERE cardinality(matched) >= $2
      ORDER BY cardinality(matched) DESC, rank DESC
      LIMIT $3
    """
    rows = await db.p().fetch(sql, lexemes, _min_hits(len(lexemes)), limit)
    return [
        {
            "type": "page",
            "id": r["id"],
            "title": r["title"],
            "description": r["description"] or "",
            "path": r["path"],
            "url": f"{settings.public_url}/{r['path']}",
            "snippet": _clean_snippet(r["snippet"]),
            "matched": list(r["matched"]),
            "score": len(r["matched"]) / max(1, len(lexemes)),
            "maps": list(r["maps"] or []),
            "updated": r["updated"],
        }
        for r in rows
    ]


async def search_maps(lexemes: list[str], limit: int) -> list[dict]:
    cfg = settings.ts_config
    map_tsv = (
        f"(setweight(to_tsvector('{cfg}', m.title), 'A') || setweight(to_tsvector('{cfg}', m.description), 'B') || "
        f"setweight(to_tsvector('{cfg}', coalesce((SELECT string_agg(l.name || ' ' || l.description, ' ') "
        f"FROM pkdb.layers l WHERE l.map_id = m.id), '')), 'B'))"
    )
    maps = await db.p().fetch(f"""
        SELECT id, title, description, matched FROM (
          SELECT m.id, m.title, m.description, {_LEX_MATCH.format(tsv=map_tsv)} AS matched FROM pkdb.maps m
        ) x WHERE cardinality(matched) > 0
    """, lexemes)
    feat_tsv = f"to_tsvector('{cfg}', f.name || ' ' || f.props::text)"
    feats = await db.p().fetch(f"""
        SELECT * FROM (
          SELECT f.id, f.name, l.map_id, {_LEX_MATCH.format(tsv=feat_tsv)} AS matched
          FROM pkdb.features f JOIN pkdb.layers l ON l.id = f.layer_id
          WHERE {feat_tsv} @@ {_OR_QUERY}
          LIMIT 2000
        ) x WHERE cardinality(matched) > 0
    """, lexemes)

    by_map: dict[int, dict] = {}
    for m in maps:
        by_map[m["id"]] = {"matched": set(m["matched"]), "features": []}
    for f in feats:
        e = by_map.setdefault(f["map_id"], {"matched": set(), "features": []})
        e["features"].append({"id": f["id"], "name": f["name"], "matched": set(f["matched"])})

    need = _min_hits(len(lexemes))
    scored = []
    for map_id, e in by_map.items():
        # A map counts as a hit if map metadata + its best features cover enough of the query
        best = sorted(e["features"], key=lambda x: len(x["matched"]), reverse=True)
        covered = set(e["matched"])
        for f in best[:10]:
            covered |= f["matched"]
        if len(covered) < need:
            continue
        top = [f for f in best if len(f["matched"] | e["matched"]) >= need][:20] or best[:5]
        scored.append((len(covered), len(top), map_id, top, covered))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    scored = scored[:limit]
    if not scored:
        return []

    ids = [s[2] for s in scored]
    meta = {r["id"]: r for r in await db.p().fetch("""
        SELECT m.id, m.title, m.description,
               (SELECT ST_AsGeoJSON(ST_Extent(f.geom))::json FROM pkdb.features f JOIN pkdb.layers l ON l.id=f.layer_id
                WHERE l.map_id = m.id) AS extent,
               (SELECT count(*) FROM pkdb.layers l WHERE l.map_id = m.id) AS layers
        FROM pkdb.maps m WHERE m.id = ANY($1::int[])""", ids)}
    pages = await pages_embedding_maps(ids)
    out = []
    for covered, _, map_id, top, matched in scored:
        m = meta.get(map_id)
        if not m:
            continue
        out.append({
            "type": "map",
            "id": map_id,
            "title": m["title"] or f"Map #{map_id}",
            "description": m["description"],
            "features": [{"id": f["id"], "name": f["name"]} for f in top],
            "matched": sorted(matched),
            "score": covered / max(1, len(lexemes)),
            "pages": pages.get(map_id, []),
            "embed_url": f"{settings.public_url}/pkdb/embed/map/{map_id}",
        })
    return out


async def pages_embedding_maps(map_ids: list[int]) -> dict[int, list[dict]]:
    if not map_ids:
        return {}
    rows = await db.p().fetch("""
        SELECT id, path, title, content FROM public.pages
        WHERE content ~ '/pkdb/embed/map/[0-9]+' AND "isPublished" = true
    """)
    result: dict[int, list[dict]] = {}
    wanted = set(map_ids)
    for r in rows:
        for mid in {int(x) for x in MAP_EMBED_RE.findall(r["content"] or "")} & wanted:
            result.setdefault(mid, []).append(
                {"id": r["id"], "title": r["title"], "path": r["path"], "url": f"{settings.public_url}/{r['path']}"}
            )
    return result


async def search(q: str, limit: int | None = None) -> dict:
    limit = limit or settings.search_max_results
    lexemes = await query_lexemes(q)
    if not lexemes:
        return {"query": q, "lexemes": [], "pages": [], "maps": []}
    pages = await search_pages(lexemes, limit)
    maps = await search_maps(lexemes, limit)
    return {"query": q, "lexemes": lexemes, "pages": pages, "maps": maps}


async def related_pages_for_text(text: str, limit: int = 40) -> list[dict]:
    """Candidate pages for the LLM: rank pages by overlap with the submitted text."""
    rows = await db.p().fetch(
        "SELECT lexeme FROM unnest(to_tsvector($1::regconfig, $2)) ORDER BY cardinality(positions) DESC LIMIT 40",
        settings.ts_config, text[:20000],
    )
    lexemes = [r["lexeme"] for r in rows]
    if not lexemes:
        return []
    cfg = settings.ts_config
    rows = await db.p().fetch(f"""
        SELECT p.id, p.path, p.title, p.description,
               ts_rank_cd(setweight(to_tsvector('{cfg}', p.title), 'A') ||
                          setweight(to_tsvector('{cfg}', coalesce(p.description,'')), 'B') ||
                          setweight(to_tsvector('{cfg}', coalesce(p.content,'')), 'C'), {_OR_QUERY}) AS rank
        FROM public.pages p
        ORDER BY rank DESC, p."updatedAt" DESC
        LIMIT $2
    """, lexemes, limit)
    return [dict(r) for r in rows]
