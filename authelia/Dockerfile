FROM authelia/authelia:4.39

COPY configuration.yml /etc/pkdb/configuration.yml
COPY entrypoint.sh /pkdb-entrypoint.sh
RUN chmod +x /pkdb-entrypoint.sh

VOLUME /config
ENTRYPOINT ["/pkdb-entrypoint.sh"]
