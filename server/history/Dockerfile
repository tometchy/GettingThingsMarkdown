FROM docker.gitea.com/gitea:1.27.3-rootless
USER root
COPY server/history/entrypoint.sh /usr/local/bin/gtm-history-entrypoint
RUN chmod 755 /usr/local/bin/gtm-history-entrypoint
USER 1000:1000
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/local/bin/gtm-history-entrypoint"]
