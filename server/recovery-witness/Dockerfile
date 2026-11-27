FROM alpine:3.23
RUN apk add --no-cache python3 openssl ca-certificates && addgroup -g 1000 gtmd && adduser -D -u 1000 -G gtmd gtmd
COPY server/integrity/recovery_crypto.py server/integrity/recovery_witness.py server/integrity/witness_api.py server/integrity/witness_etcd.py /app/
USER 1000:1000
ENTRYPOINT ["python3", "-B", "/app/witness_api.py"]
