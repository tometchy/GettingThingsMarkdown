FROM ghcr.io/syncthing/syncthing:2.1.7 AS syncthing
FROM alpine:3.23
RUN apk add --no-cache ca-certificates git curl bash tini && \
    addgroup -g 1000 gtm && adduser -D -u 1000 -G gtm -h /home/gtm gtm && \
    mkdir -p /data/files /data/git /data/syncthing && chown -R 1000:1000 /data /home/gtm
COPY --from=syncthing /bin/syncthing /usr/local/bin/syncthing
COPY server/sync/entrypoint.sh server/sync/commit-loop.sh server/sync/askpass.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/commit-loop.sh /usr/local/bin/askpass.sh
USER 1000:1000
WORKDIR /data/files
EXPOSE 8384 22000/tcp 22000/udp
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
