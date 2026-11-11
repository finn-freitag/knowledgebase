FROM caddy:2.9-alpine

COPY Caddyfile /etc/caddy/Caddyfile
COPY auth /etc/caddy/auth
COPY sites /etc/caddy/sites
COPY entrypoint.sh /docker-entrypoint-pkdb.sh
RUN chmod +x /docker-entrypoint-pkdb.sh

ENTRYPOINT ["/docker-entrypoint-pkdb.sh"]
