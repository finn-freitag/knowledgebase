#!/bin/sh
# Derives the Caddy configuration from the PKDB .env variables.
set -e

AUTH_MODE="$(echo "${AUTH_MODE:-builtin}" | tr '[:upper:]' '[:lower:]')"
WIKI_PUBLIC="$(echo "${WIKI_PUBLIC:-false}" | tr '[:upper:]' '[:lower:]')"
export MAX_UPLOAD_MB="${MAX_UPLOAD_MB:-200}"

if [ -n "$PKDB_HOST" ]; then
  # Public host name -> automatic HTTPS (Let's Encrypt), or internal CA with TLS_MODE=internal
  export SITE_ADDRESS="$PKDB_HOST"
  case "$TLS_MODE" in
    internal) export CADDY_GLOBAL="local_certs" ;;
    *)        export CADDY_GLOBAL="${ACME_EMAIL:+email $ACME_EMAIL}" ;;
  esac
else
  export SITE_ADDRESS=":80"
  export CADDY_GLOBAL="auto_https off"
fi

if [ "$AUTH_MODE" = "authelia" ]; then
  if [ -z "$PKDB_HOST" ] || [ -z "$AUTH_HOST" ]; then
    echo "ERROR: AUTH_MODE=authelia requires PKDB_HOST and AUTH_HOST (real domain names with HTTPS)." >&2
    exit 1
  fi
  if [ "$WIKI_PUBLIC" = "true" ]; then
    export CADDY_AUTH="none"
    echo "Authelia enabled, but WIKI_PUBLIC=true: wiki pages are readable without Authelia."
  else
    export CADDY_AUTH="authelia"
  fi
else
  export CADDY_AUTH="none"
fi

# The Authelia portal site is needed whenever Authelia runs, even if the wiki itself is public
if [ "$AUTH_MODE" = "authelia" ]; then
  export CADDY_SITES="authelia"
else
  export CADDY_SITES="none"
fi

echo "PKDB proxy: site=$SITE_ADDRESS auth=$CADDY_AUTH"
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
