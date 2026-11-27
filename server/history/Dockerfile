FROM docker.gitea.com/gitea:1.27.3-rootless
USER root
COPY server/history/entrypoint.sh /usr/local/bin/gtm-history-entrypoint
COPY server/history/backup-once.sh server/history/backup-loop.sh /usr/local/bin/
COPY server/integrity/git_integrity.py server/integrity/physical_scrub.py server/integrity/authorized_rebuild.py /usr/local/bin/
RUN apk add --no-cache openssl python3 aws-cli jq rclone gnupg sqlite coreutils && \
    chmod 755 /usr/local/bin/gtm-history-entrypoint /usr/local/bin/backup-once.sh /usr/local/bin/backup-loop.sh
USER 1000:1000
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/local/bin/gtm-history-entrypoint"]
