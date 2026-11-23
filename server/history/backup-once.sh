#!/bin/sh
# Per-tenant encrypted Git metadata plus incremental encrypted LFS WORM backup.
# Must be invoked by the already-running History container (not a third pod).
set -eu
: "${GTM_TENANT_NAMESPACE:?}"
: "${GTM_WORM_ENDPOINT:?}"
: "${GTM_WORM_BUCKET:?}"
: "${GTM_WORM_REGION:?}"
: "${GTM_WORM_ACCESS_KEY_ID:?}"
: "${GTM_WORM_SECRET_ACCESS_KEY:?}"
: "${GTM_WORM_BACKUP_PASSPHRASE:?}"
: "${GTM_LFS_PREFIX:?}"
: "${GITEA__storage__MINIO_ENDPOINT:?}"
: "${GITEA__storage__MINIO_BUCKET:?}"
: "${GITEA__storage__MINIO_ACCESS_KEY_ID:?}"
: "${GITEA__storage__MINIO_SECRET_ACCESS_KEY:?}"

# The target bucket MUST have Object Lock enabled since creation, and default
# COMPLIANCE retention >=30 days. Abort before uploading if not configured.
export AWS_ACCESS_KEY_ID="$GTM_WORM_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$GTM_WORM_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="$GTM_WORM_REGION"
lock="$(
  aws --endpoint-url "$GTM_WORM_ENDPOINT" s3api get-object-lock-configuration \
    --bucket "$GTM_WORM_BUCKET" --output json
)"
printf '%s\n' "$lock" | jq -e '
  .ObjectLockEnabled == "Enabled" and
  .Rule.DefaultRetention.Mode == "COMPLIANCE" and
  ((.Rule.DefaultRetention.Days // 0) >= 30 or
   (.Rule.DefaultRetention.Years // 0) >= 1)
' >/dev/null || { echo "Refusing backup: WORM COMPLIANCE >=30d absent" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
chmod 0700 "$tmp"
repo=/var/lib/gitea/git/repositories/archive/history.git
db="$(find /var/lib/gitea -type f -name gitea.db -print -quit)"
[ -n "$db" ] && [ -f "$db" ] || {
  echo "Gitea SQLite database unavailable; no consistent snapshot" >&2
  exit 1
}
sqlite3 "$db" ".backup '$tmp/gitea.sqlite'"
if git --git-dir="$repo" rev-parse --verify main >/dev/null 2>&1; then
  git --git-dir="$repo" bundle create "$tmp/history.bundle" --all
else
  echo "No committed Git history yet; recording SQLite only."
fi
date -u +%FT%TZ > "$tmp/backup-utc.txt"
(
  cd "$tmp"
  sha256sum gitea.sqlite *bundle 2>/dev/null > checksums.txt || sha256sum gitea.sqlite > checksums.txt
  tar -czf snapshot.tar.gz gitea.sqlite backup-utc.txt checksums.txt \
    $(test -f history.bundle && printf '%s' history.bundle)
)
# GPG symmetric AES256 with integrity protection; passphrase through stdin,
# never command-line args. Operators must protect the separate recovery key.
printf '%s\n' "$GTM_WORM_BACKUP_PASSPHRASE" |
  gpg --homedir "$tmp" --batch --yes --pinentry-mode loopback \
    --passphrase-fd 0 --symmetric --cipher-algo AES256 \
    --output "$tmp/snapshot.tar.gz.gpg" "$tmp/snapshot.tar.gz"
rm -f "$tmp/snapshot.tar.gz" "$tmp/gitea.sqlite" "$tmp/history.bundle"
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
key="${GTM_TENANT_NAMESPACE}/metadata/${stamp}.tar.gz.gpg"
retention="$(date -u -d '+31 days' +%Y-%m-%dT%H:%M:%SZ)"
aws --endpoint-url "$GTM_WORM_ENDPOINT" s3api put-object \
  --bucket "$GTM_WORM_BUCKET" --key "$key" \
  --body "$tmp/snapshot.tar.gz.gpg" \
  --object-lock-mode COMPLIANCE --object-lock-retain-until-date "$retention" >/dev/null

# Verify WORM mode on the actual newly uploaded object.
status="$(aws --endpoint-url "$GTM_WORM_ENDPOINT" s3api head-object \
  --bucket "$GTM_WORM_BUCKET" --key "$key" --output json)"
printf '%s\n' "$status" | jq -e '.ObjectLockMode == "COMPLIANCE"' >/dev/null

# Copy *only encrypted* Git LFS blobs (opaque content-addressed objects)
# from the tenant-isolated active LFS S3 bucket into immutable WORM storage.
# Never use sync/delete against a WORM bucket. Source/target scoped keys
# cannot be shared with other tenant pods.
export RCLONE_CONFIG_LFS_TYPE=s3
export RCLONE_CONFIG_LFS_PROVIDER=Other
export RCLONE_CONFIG_LFS_ENV_AUTH=false
export RCLONE_CONFIG_LFS_ACCESS_KEY_ID="$GITEA__storage__MINIO_ACCESS_KEY_ID"
export RCLONE_CONFIG_LFS_SECRET_ACCESS_KEY="$GITEA__storage__MINIO_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_LFS_ENDPOINT="https://$GITEA__storage__MINIO_ENDPOINT"
export RCLONE_CONFIG_WORM_TYPE=s3
export RCLONE_CONFIG_WORM_PROVIDER=Other
export RCLONE_CONFIG_WORM_ENV_AUTH=false
export RCLONE_CONFIG_WORM_ACCESS_KEY_ID="$GTM_WORM_ACCESS_KEY_ID"
export RCLONE_CONFIG_WORM_SECRET_ACCESS_KEY="$GTM_WORM_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_WORM_ENDPOINT="$GTM_WORM_ENDPOINT"
rclone copy "lfs:${GITEA__storage__MINIO_BUCKET}/${GTM_LFS_PREFIX}" \
  "worm:${GTM_WORM_BUCKET}/${GTM_TENANT_NAMESPACE}/lfs" \
  --immutable --transfers 2 --checkers 4
echo "WORM backup verified for tenant ${GTM_TENANT_NAMESPACE} at $key"
