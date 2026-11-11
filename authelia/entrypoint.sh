#!/bin/sh
# Prepares Authelia from PKDB .env variables: generates persistent secrets and the admin user, then starts Authelia.
set -e

CONFIG_DIR=/config
SECRETS_DIR="$CONFIG_DIR/secrets"
mkdir -p "$SECRETS_DIR"

if [ -z "$PKDB_HOST" ] || [ -z "$AUTH_HOST" ]; then
  echo "ERROR: Authelia needs PKDB_HOST and AUTH_HOST to be set in .env" >&2
  exit 1
fi

# Cookie domain: common parent domain of PKDB_HOST and AUTH_HOST (default: PKDB_HOST without its first label)
if [ -z "$AUTH_COOKIE_DOMAIN" ]; then
  AUTH_COOKIE_DOMAIN="${PKDB_HOST#*.}"
fi
export AUTH_COOKIE_DOMAIN
export AUTH_LOG_LEVEL="${AUTH_LOG_LEVEL:-info}"
export AUTH_POLICY="${AUTH_POLICY:-one_factor}"
export AUTH_SESSION_EXPIRATION="${AUTH_SESSION_EXPIRATION:-12h}"
export AUTH_SESSION_INACTIVITY="${AUTH_SESSION_INACTIVITY:-2h}"

gen_secret() {
  f="$SECRETS_DIR/$1"
  if [ ! -s "$f" ]; then
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 64 >"$f"
    chmod 600 "$f"
  fi
}
gen_secret session
gen_secret storage
gen_secret jwt
export AUTHELIA_SESSION_SECRET_FILE="$SECRETS_DIR/session"
export AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE="$SECRETS_DIR/storage"
export AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE="$SECRETS_DIR/jwt"

# Admin user from .env. Set AUTH_MANAGE_USERS=false to maintain users_database.yml by hand.
USERS="$CONFIG_DIR/users_database.yml"
if [ "${AUTH_MANAGE_USERS:-true}" = "true" ]; then
  if [ -z "$AUTH_ADMIN_USER" ] || [ -z "$AUTH_ADMIN_PASSWORD" ]; then
    echo "ERROR: set AUTH_ADMIN_USER and AUTH_ADMIN_PASSWORD in .env" >&2
    exit 1
  fi
  FINGERPRINT="$(printf '%s:%s:%s' "$AUTH_ADMIN_USER" "$AUTH_ADMIN_PASSWORD" "$AUTH_ADMIN_EMAIL" | sha256sum | cut -d' ' -f1)"
  if [ ! -s "$USERS" ] || [ "$(cat "$SECRETS_DIR/users.fingerprint" 2>/dev/null)" != "$FINGERPRINT" ]; then
    echo "Generating Authelia user database for $AUTH_ADMIN_USER"
    HASH="$(authelia crypto hash generate argon2 --password "$AUTH_ADMIN_PASSWORD" | sed -n 's/^Digest: //p')"
    if [ -z "$HASH" ]; then
      echo "ERROR: could not hash the Authelia admin password" >&2
      exit 1
    fi
    cat >"$USERS" <<EOF
users:
  ${AUTH_ADMIN_USER}:
    disabled: false
    displayname: '${AUTH_ADMIN_USER}'
    password: '${HASH}'
    email: '${AUTH_ADMIN_EMAIL:-admin@${AUTH_COOKIE_DOMAIN}}'
    groups:
      - 'admins'
EOF
    chmod 600 "$USERS"
    echo "$FINGERPRINT" >"$SECRETS_DIR/users.fingerprint"
  fi
fi

export X_AUTHELIA_CONFIG=/etc/pkdb/configuration.yml
export X_AUTHELIA_CONFIG_FILTERS=template
exec authelia --config /etc/pkdb/configuration.yml
