FROM ghcr.io/syncthing/syncthing:2.1.7 AS syncthing
FROM alpine:3.23
RUN apk add --no-cache ca-certificates git git-lfs curl bash tini age python3 && \
    addgroup -g 1000 gtm && adduser -D -u 1000 -G gtm -h /home/gtm gtm && \
    mkdir -p /data/files /data/git /data/syncthing && chown -R 1000:1000 /data /home/gtm
COPY --from=syncthing /bin/syncthing /usr/local/bin/syncthing
COPY server/sync/mail-files.py server/sync/storage_budget.py /usr/local/bin/
COPY server/sync/entrypoint.sh server/sync/commit-loop.sh server/sync/askpass.sh \
     server/sync/identity-backup-once.sh server/sync/identity-backup-loop.sh \
     server/sync/lfs-backfill-once.sh server/sync/lfs-backfill-loop.sh \
     server/sync/recover-from-git.sh server/sync/reseed-history.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/commit-loop.sh /usr/local/bin/askpass.sh \
    /usr/local/bin/identity-backup-once.sh /usr/local/bin/identity-backup-loop.sh \
    /usr/local/bin/lfs-backfill-once.sh /usr/local/bin/lfs-backfill-loop.sh \
    /usr/local/bin/recover-from-git.sh /usr/local/bin/reseed-history.sh
USER 1000:1000
WORKDIR /data/files
EXPOSE 8384 8385 22000/tcp 22000/udp
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
