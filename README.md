# PKDB – Personal Knowledge Database

A self-hosted knowledge base for everything you learn about your town – council and committee work, nature
conservation, places, projects – with first-class support for **geodata**.

* **Wiki** – [Wiki.js 2](https://js.wiki) (Markdown, page history, own login, MCP-compatible).
* **Firefox extension**
  * shows matching wiki pages and maps **at the top of Google, DuckDuckGo, Ecosia, Bing, Startpage, Brave Search
    and Qwant** results,
  * lets you send text, images and geodata to the knowledge base. An **LLM** (local or remote) then extends existing
    articles or writes new ones.
* **Geodata** – drop GeoJSON, GPX, KML/KMZ, GeoPackage, Shapefile (loose files or .zip), FlatGeobuf, GML, … or draw
  points, lines and polygons on a map. Images can be **georeferenced** by dragging their four corners onto the map.
  Everything ends up as an interactive Leaflet map embedded in the wiki articles and stored in PostGIS.
* **Login** – Wiki.js login (default) or **Authelia** single sign-on in front of everything. The extension API
  always requires a token.
* Everything runs in Docker. Images are built by GitHub Actions and published to GHCR.

```
 Firefox extension ──token──▶ ┌────────── Caddy (proxy, HTTPS) ──────────┐ ◀── browser (wiki)
                              │  /pkdb/api/*  ─▶ backend (FastAPI, GDAL)  │
                              │  /pkdb/embed/* ─▶ backend  (map iframes)  │──▶ LLM (OpenAI-compatible / Claude)
                              │  /*           ─▶ Wiki.js                  │
                              └───────── optional: Authelia ──────────────┘
                                         backend + Wiki.js ──▶ PostgreSQL + PostGIS
```

## Quick start

1. Download [`docker-compose.yml`](docker-compose.yml) and [`.env.example`](.env.example) into a folder and
   rename `.env.example` to `.env`.
2. Edit `.env`. At minimum set `POSTGRES_PASSWORD`, `WIKI_ADMIN_EMAIL`, `WIKI_ADMIN_PASSWORD`, `WIKI_LANGUAGE`
   and the `LLM_*` settings. Use `PKDB_HOST` if you have a domain.
3. Start it:

   ```bash
   docker compose up -d
   ```

4. Open `http://<server>` (or `https://PKDB_HOST`). Wiki.js is set up automatically on first start (this takes up
   to a minute). Log in with `WIKI_ADMIN_EMAIL` / `WIKI_ADMIN_PASSWORD`.
5. Install the Firefox extension (see below). Open its settings, enter the server URL and log in with your wiki
   account.

Check the backend's progress with `docker compose logs -f backend`.

### Deployment variants

| Scenario | `.env` |
|---|---|
| LAN / localhost, plain HTTP | `PKDB_HOST=` (empty), `HTTP_PORT=80` |
| LAN with self-signed HTTPS | `PKDB_HOST=wiki.home.arpa`, `TLS_MODE=internal` |
| Own domain, Let's Encrypt | `PKDB_HOST=wiki.example.com` (ports 80/443 reachable) |
| Own domain + Authelia SSO | additionally `AUTH_MODE=authelia`, `COMPOSE_PROFILES=authelia`, `AUTH_HOST=auth.example.com`, `AUTH_ADMIN_USER`, `AUTH_ADMIN_PASSWORD` |

`WIKI_PUBLIC=true` makes pages and maps readable without logging in. Editing always needs a login, and the
extension always needs a token.

With Authelia, the wiki and map embeds are protected by the Authelia portal. The extension API (`/pkdb/api/*`)
bypasses Authelia and uses its own tokens instead. Wiki.js keeps its own accounts for editing.

### LLM

Any OpenAI-compatible endpoint works (OpenAI, Ollama, LM Studio, OpenRouter, vLLM, …), as does the Anthropic API.
See the examples in `.env.example`. For Ollama on the Docker host, uncomment `extra_hosts` in `docker-compose.yml`
and use `LLM_BASE_URL=http://host.docker.internal:11434/v1`. Larger models (≥ 14B) give much better results, because
the LLM has to return the complete updated article as JSON.

`LLM_APPLY_MODE=review` (default) keeps proposed changes in a review queue. `auto` writes them directly, and the
Wiki.js page history lets you roll back. The extension can override the mode per submission.

## Using it

### Adding information

Click the toolbar icon → **Add information**, or right-click selected text, a page or an image → *Add … to
knowledge base*.

* **Text** (required) – paste or write what you learned, plus an optional title and source URL.
* **Files** – drop images and geodata anywhere on the page. Geodata is previewed on the map immediately, and every
  file gets a title and description. Shapefiles need `.shp`, `.dbf`, `.shx` and `.prj` together, or one `.zip`.
* **Draw** – use the toolbar on the map to add markers, lines, polygons and rectangles, and give each shape a name and
  description.
* **Georeference images** – click *📍 Georeference* on an image, then drag the four corner handles onto the matching
  places (the satellite layer helps). The image becomes a map overlay.
* **Send** – the backend stores everything and creates a map from the geodata. The LLM then picks related wiki pages
  and proposes new or updated articles with the map and images embedded.

### Reviewing

**Review queue** in the extension shows each proposed change as a diff. You can edit path, title, tags and text,
deselect changes you don't want, apply them, reject them, or let the LLM try again with feedback. The toolbar badge
shows how many submissions are waiting.

### Searching

Search as usual. When your wiki has matching pages or map features, a *Knowledge base* box appears above the normal
results, with snippets, links and a map preview that highlights the matching features. Search uses PostgreSQL full
text search with stemming for `WIKI_LANGUAGE`. `SEARCH_MIN_MATCH` sets how many search words must match.

### Editing the wiki directly

The wiki is a normal Wiki.js, so you can edit it as you like. Maps are embedded with

```html
<iframe src="/pkdb/embed/map/12" width="100%" height="450" style="border:0"></iframe>
```

and you can move or copy that line to any page. Map search results link to the pages that embed the map.

## Firefox extension

* **From a release**: download the `.xpi` (signed, if AMO keys are configured as repository secrets
  `AMO_JWT_ISSUER` / `AMO_JWT_SECRET`) or the `.zip` from the GitHub release.
* **For development**:

  ```bash
  cd extension && npm install && npm run vendor
  ```

  Then load it in Firefox via `about:debugging` → *This Firefox* → *Load Temporary Add-on…* → `extension/manifest.json`
  (or run `npm start`).

Unsigned add-ons can only be installed permanently in Firefox Developer Edition / Nightly
(`xpinstall.signatures.required = false`). For normal Firefox, sign the add-on as *unlisted* on
addons.mozilla.org. The release workflow does this automatically when the secrets are set.

If search results don't appear, check that the extension may access the search engines and your server:
`about:addons` → PKDB → *Permissions*.

## Building the images

`.github/workflows/docker.yml` builds `pkdb-backend`, `pkdb-proxy` and `pkdb-authelia` for amd64 and arm64 on every
push to `main` and on `v*` tags, and pushes them to `ghcr.io/<owner>/…`. `docker-compose.yml` references
`ghcr.io/${PKDB_IMAGE_OWNER}/…:${PKDB_VERSION}`. Make the packages public in the GitHub package settings, or run
`docker login ghcr.io` on the server.

On ARM (Raspberry Pi), set `POSTGIS_IMAGE` to a multi-arch PostGIS image (see `.env.example`).

## Data & backup

| Volume | Content |
|---|---|
| `db-data` | PostgreSQL: Wiki.js pages, history and assets, plus PKDB maps, features and submissions (schema `pkdb`) |
| `pkdb-data` | uploaded images and original geodata files |
| `caddy-data` | TLS certificates |
| `authelia-config` | Authelia users, secrets, sessions |

Backup example:

```bash
docker compose exec db pg_dump -U pkdb pkdb > pkdb.sql
```

Also back up the `pkdb-data` volume.

## Repository layout

```
backend/     FastAPI backend (Wiki.js automation, GDAL import, LLM pipeline, search, map embeds)
caddy/       reverse proxy image (HTTP / automatic HTTPS / Authelia forward auth)
authelia/    Authelia image with env-based configuration
extension/   Firefox extension (Manifest V3)
shared/      map rendering code shared by the embed viewer and the extension
```

## API (for scripts)

All endpoints need `Authorization: Bearer <token>`. Interactive docs are at `/pkdb/api/docs`.

| Method | Path | |
|---|---|---|
| POST | `/pkdb/api/auth/login` | `{username, password}` → `{token}` (Wiki.js account) |
| GET | `/pkdb/api/search?q=` | wiki pages + map results |
| POST | `/pkdb/api/submissions` | multipart: `meta` (JSON), `geofiles`, `image_<n>` |
| GET | `/pkdb/api/submissions[/{id}]` | status / proposed changes |
| POST | `/pkdb/api/submissions/{id}/apply` | apply (optionally edited) operations |
| POST | `/pkdb/api/submissions/{id}/retry` | `{feedback}` |
| GET | `/pkdb/api/maps/{id}` | map layers as GeoJSON |
| POST | `/pkdb/api/geodata/preview` | convert files to GeoJSON without saving |
