#!/usr/bin/env bash
# Unpaid, local S3 protocol smoke test for Gitea Git LFS. Moto is an
# open-source AWS S3 emulator; no real cloud provider credential is used.
set -euo pipefail
cd "$(dirname "$0")/../.."
need() { command -v "$1" >/dev/null || { echo "Missing $1"; exit 1; }; }
need docker; need openssl; need sha256sum
suffix="$RANDOM-$$"
network="gtm-lfs-$suffix"
moto="gtm-moto-$suffix"
history="gtm-history-$suffix"
sync="gtm-sync-$suffix"
root="$(mktemp -d)"
cleanup() {
  docker rm -f "$sync" "$history" "$moto" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$root" 2>/dev/null || true
}
trap cleanup EXIT
docker network create "$network" >/dev/null
mkdir -p "$root"/{tls,history-data,history-config,sync/files,sync/git,sync/syncthing}
chmod -R 0777 "$root"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$root/tls/tls.key" -out "$root/tls/tls.crt" -days 1 \
  -subj "/CN=history" -addext "subjectAltName=DNS:history" >/dev/null 2>&1
chmod 0644 "$root/tls/tls.key" "$root/tls/tls.crt"

moto_image="ghcr.io/getmoto/motoserver:5.1.22"
docker run -d --name "$moto" --network "$network" --network-alias moto \
  -e MOTO_PORT=5000 "$moto_image" >/dev/null
for i in $(seq 1 35); do
  if docker run --rm --network "$network" --entrypoint python "$moto_image" \
    -c "import urllib.request; urllib.request.urlopen('http://moto:5000/moto-api/config',timeout=2)" \
    >/dev/null 2>&1; then break; fi
  sleep 1
done
docker run --rm --network "$network" --entrypoint python "$moto_image" \
  -c "import boto3; boto3.client('s3',endpoint_url='http://moto:5000',aws_access_key_id='test',aws_secret_access_key='test',region_name='us-east-1').create_bucket(Bucket='gtm-lfs-ci')" \
  >/dev/null

docker run -d --name "$history" --network "$network" --network-alias history \
  -v "$root/history-data:/var/lib/gitea" \
  -v "$root/history-config:/etc/gitea" \
  -v "$root/tls:/tls:ro" \
  -e GTM_GITEA_ADMIN_PASSWORD="ci-test-history-admin-$(openssl rand -hex 16)" \
  -e GTM_GITEA_SYNC_PASSWORD="CI-SYNC-$(openssl rand -hex 16)" \
  -e GITEA__server__DOMAIN=history \
  -e GITEA__server__PROTOCOL=https \
  -e GITEA__server__ROOT_URL=https://history:3000/ \
  -e GITEA__server__CERT_FILE=/tls/tls.crt \
  -e GITEA__server__KEY_FILE=/tls/tls.key \
  -e GITEA__security__INSTALL_LOCK=true \
  -e GITEA__service__DISABLE_REGISTRATION=true \
  -e GITEA__service__REQUIRE_SIGNIN_VIEW=true \
  -e GITEA__server__DISABLE_SSH=true \
  -e GITEA__repository__FORCE_PRIVATE=true \
  -e GITEA__database__DB_TYPE=sqlite3 \
  -e GITEA__actions__ENABLED=false \
  -e GITEA__server__LFS_START_SERVER=true \
  -e GITEA__storage__STORAGE_TYPE=minio \
  -e GITEA__storage__MINIO_ENDPOINT=moto:5000 \
  -e GITEA__storage__MINIO_BUCKET=gtm-lfs-ci \
  -e GITEA__storage__MINIO_LOCATION=us-east-1 \
  -e GITEA__storage__MINIO_ACCESS_KEY_ID=test \
  -e GITEA__storage__MINIO_SECRET_ACCESS_KEY=test \
  -e GITEA__storage__MINIO_USE_SSL=false \
  -e GITEA__storage__MINIO_BUCKET_LOOKUP_TYPE=path \
  -e GITEA__lfs__MINIO_BASE_PATH=tenants/gtm-ci/lfs/ \
  gtm/history:ci >/dev/null

for i in $(seq 1 90); do
  if docker logs "$history" 2>&1 | grep -q "Gitea archive initialized"; then break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$history")" != true ]; then
    docker logs "$history" --tail=100 >&2; exit 1
  fi
  sleep 2
done
docker logs "$history" 2>&1 | grep -q "Gitea archive initialized" || {
  docker logs "$history" --tail=100 >&2; exit 1
}

# Fetch the just-generated archive writer password from the container config
# only in this disposable CI scenario. Production secrets are separate per tenant.
writer_password="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$history" | \
  sed -n 's/^GTM_GITEA_SYNC_PASSWORD=//p')"
docker run -d --name "$sync" --network "$network" \
  -v "$root/sync:/data" -v "$root/tls/tls.crt:/tls/tls.crt:ro" \
  -e GTM_SYNCTHING_API_KEY="$(openssl rand -hex 32)" \
  -e GTM_GIT_URL=https://history:3000/archive/history.git \
  -e GTM_GIT_USERNAME=sync \
  -e GTM_GIT_PASSWORD="$writer_password" \
  -e GTM_GIT_LFS_ENABLED=true \
  gtm/sync:ci >/dev/null
for i in $(seq 1 40); do
  if docker logs "$sync" 2>&1 | grep -q 'GUI and API listening'; then break; fi
  sleep 1
done
dd if=/dev/urandom of="$root/sync/files/opaque.bin" bs=1024 count=1024 status=none
source_sha="$(sha256sum "$root/sync/files/opaque.bin" | awk '{print $1}')"

ok=0
for i in $(seq 1 75); do
  if docker exec "$history" git \
      --git-dir=/var/lib/gitea/git/repositories/archive/history.git \
      rev-parse --verify main >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  docker logs "$sync" --tail=80 >&2
  docker logs "$history" --tail=80 >&2
  echo "FAIL: LFS Git push did not reach Gitea" >&2
  exit 1
fi
docker exec "$history" git \
  --git-dir=/var/lib/gitea/git/repositories/archive/history.git \
  show main:opaque.bin | grep -q 'version https://git-lfs.github.com/spec/v1'

# Verify S3 actually contains opaque Git LFS objects under the tenant prefix.
docker run --rm --network "$network" --entrypoint python "$moto_image" \
  -c "import boto3; s=boto3.client('s3',endpoint_url='http://moto:5000',aws_access_key_id='test',aws_secret_access_key='test',region_name='us-east-1'); a=s.list_objects_v2(Bucket='gtm-lfs-ci',Prefix='tenants/gtm-ci/lfs/'); assert a.get('KeyCount',0)>0,a" >/dev/null

# Restore a real LFS object through Gitea HTTPS (not direct source file copy).
docker exec "$sync" sh -ec '
  export GIT_SSL_CAINFO=/tls/tls.crt
  export GIT_ASKPASS=/usr/local/bin/askpass.sh
  export GIT_TERMINAL_PROMPT=0
  git clone -q "$GTM_GIT_URL" /data/git/ci-restore
  # The server intentionally does not version a plaintext .gitattributes
  # inside the encrypted folder. Restore must explicitly supply the LFS
  # attributes and initialize the Git LFS checkout on the trusted host.
  git -C /data/git/ci-restore config core.attributesfile /data/git/info/attributes
  git -C /data/git/ci-restore lfs install --local
  git -C /data/git/ci-restore lfs pull
  git -C /data/git/ci-restore lfs checkout
'
restored_sha="$(docker exec "$sync" sha256sum /data/git/ci-restore/opaque.bin | awk '{print $1}')"
[ "$source_sha" = "$restored_sha" ] || {
  echo "FAIL: Git LFS restore differed from encrypted source" >&2
  docker exec "$sync" sh -c 'git -C /data/git/ci-restore lfs ls-files && ls -lh /data/git/ci-restore/opaque.bin' || true
  docker logs "$sync" --tail=70 >&2
  exit 1
}
echo "PASS: Git LFS pushed to emulated S3 tenant prefix and restored identical bytes via Gitea HTTPS."
