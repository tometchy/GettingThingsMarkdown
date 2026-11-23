#!/usr/bin/env bash
# Confirms a 30-day immutable backup cannot accidentally start without a
# COMPLIANCE-mode Object Lock bucket. This test never calls real S3.
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat >"$tmp/bin/aws" <<'AWS'
#!/bin/sh
case "$*" in
  *get-object-lock-configuration*)
    printf '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"%s","Days":%s}}}\n' "$STUB_MODE" "$STUB_DAYS"
    ;;
  *) echo "UNEXPECTED remote S3 call" >&2; exit 88 ;;
esac
AWS
chmod +x "$tmp/bin/aws"
export PATH="$tmp/bin:$PATH"
export GTM_TENANT_NAMESPACE=gtm-example
export GTM_LFS_PREFIX=tenants/gtm-example/lfs
export GTM_WORM_ENDPOINT=https://invalid.example.com
export GTM_WORM_BUCKET=example-test
export GTM_WORM_REGION=fsn1
export GTM_WORM_ACCESS_KEY_ID=fake
export GTM_WORM_SECRET_ACCESS_KEY=fake
export GTM_WORM_BACKUP_PASSPHRASE=example-not-real
export GITEA__storage__MINIO_ENDPOINT=invalid.example.com
export GITEA__storage__MINIO_BUCKET=isolated
export GITEA__storage__MINIO_ACCESS_KEY_ID=fake
export GITEA__storage__MINIO_SECRET_ACCESS_KEY=fake
for spec in 'GOVERNANCE 30' 'COMPLIANCE 29' 'GOVERNANCE 365'; do
  read -r STUB_MODE STUB_DAYS <<<"$spec"
  export STUB_MODE STUB_DAYS
  if sh server/history/backup-once.sh >"$tmp/stdout" 2>"$tmp/stderr"; then
    echo "FAIL: weak retention accepted: $spec" >&2; exit 1
  fi
  if ! grep -q 'Refusing backup: WORM COMPLIANCE >=30d absent' "$tmp/stderr"; then
    cat "$tmp/stderr" >&2
    echo "FAIL: weak retention not rejected at validation: $spec" >&2; exit 1
  fi
done
echo 'PASS: backup refuses governance or <30-day retention before any upload.'
