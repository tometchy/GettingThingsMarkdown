FROM alpine:3.23
RUN apk add --no-cache python3 ca-certificates && addgroup -g 1000 gtmd && adduser -D -u 1000 -G gtmd gtmd
COPY server/storage-controller/*.py /app/
USER 1000:1000
ENTRYPOINT ["python3", "-B", "/app/controller.py"]
