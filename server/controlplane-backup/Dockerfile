FROM alpine:3.23
RUN apk add --no-cache python3 age aws-cli tar && addgroup -g 1000 gtm && adduser -D -u 1000 -G gtm gtm
COPY server/controlplane-backup/backup.py /usr/local/bin/gtm-backup.py
USER 1000:1000
ENTRYPOINT ["python3", "/usr/local/bin/gtm-backup.py"]
CMD ["run"]
