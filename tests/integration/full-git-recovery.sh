#!/usr/bin/env bash
# Self-contained two-direction cold recovery of full Git history with NO LFS,
# NO S3 and two independent age recipients. Uses the actual Sync image.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$(mktemp -d)"
cleanup() { rm -rf "$root" 2>/dev/null || sudo rm -rf "$root" 2>/dev/null || true; }
trap cleanup EXIT
mkdir -p "$root"/{original,rebuilt,store}
chmod -R 0777 "$root"
uid="$(id -u):$(id -g)"
run() {
  docker run --rm --user "$uid" \
    -v "$root/store:/store" -v "$root/$1:/data" \
    --entrypoint sh gtm/sync:ci -ec "$2"
}
run original '
  set -eu
  mkdir -p /data/files /data/git /data/syncthing /data/recovery/infra
  age-keygen -o /store/age-a.key >/dev/null 2>&1
  age-keygen -o /store/age-b.key >/dev/null 2>&1
  git init -q --bare /store/history.git
  export GIT_DIR=/data/git GIT_WORK_TREE=/data/files
  git init -q -b main
  git config user.name "Test archive"
  git config user.email archive@localhost.invalid
  git remote add origin /store/history.git
  dd if=/dev/urandom of=/data/files/full-encrypted-opaque-blob.bin bs=1048576 count=8 status=none
  sha256sum /data/files/full-encrypted-opaque-blob.bin > /store/expected.sha256
  git add --all
  git commit -q -m "Full encrypted bytes"
  git push -q origin HEAD:refs/heads/main
  echo "<configuration>STABLE_SYNCTHING_ID</configuration>" > /data/syncthing/config.xml
  printf "CERT_STABLE\n" > /data/syncthing/cert.pem
  printf "KEY_STABLE\n" > /data/syncthing/key.pem
  ra="$(sed -n "s/^# public key: //p" /store/age-a.key)"
  rb="$(sed -n "s/^# public key: //p" /store/age-b.key)"
  GTM_GIT_URL=/store/history.git GTM_RECOVERY_AGE_RECIPIENT="$ra,$rb" \
    /usr/local/bin/identity-backup-once.sh
  git -C /data/recovery/infra init -q -b gtm-infra
  git -C /data/recovery/infra config user.name "Test archive"
  git -C /data/recovery/infra config user.email archive@localhost.invalid
  printf "ENCRYPTED_SECRET_ONLY" > /data/recovery/infra/tenant-secrets.json.age
  git -C /data/recovery/infra add --all
  git -C /data/recovery/infra commit -q -m "Dummy encrypted secret for simulated disaster"
  git -C /data/recovery/infra push -q /store/history.git HEAD:refs/heads/gtm-infra
  test "$(git --git-dir=/store/history.git cat-file -s main:full-encrypted-opaque-blob.bin)" -eq 8388608
'
# Simulated TOTAL loss of A; reconstitute fresh A from B without plain keys
# and without any LFS object store.
run rebuilt '
  set -eu
  GTM_GIT_URL=/store/history.git \
    GTM_RECOVERY_AGE_IDENTITY_PATH=/store/age-b.key \
    /usr/local/bin/recover-from-git.sh
  sha256sum /data/files/full-encrypted-opaque-blob.bin > /store/restored.sha256
  cmp /store/expected.sha256 /store/restored.sha256
  grep -q STABLE_SYNCTHING_ID /data/syncthing/config.xml
  grep -q KEY_STABLE /data/syncthing/key.pem
  git -C /data/recovery/settings rev-parse HEAD >/dev/null
  git -C /data/recovery/infra rev-parse HEAD >/dev/null
'
# Simulated TOTAL loss of B; after explicit operator fencing, rebuild B
# from the A archive and both encrypted recovery branches.
run rebuilt '
  set -eu
  git init -q --bare /store/rehydrated-history.git
  git --git-dir=/data/git remote set-url origin /store/rehydrated-history.git
  git -C /data/recovery/settings remote set-url origin /store/rehydrated-history.git
  git -C /data/recovery/infra remote set-url origin /store/rehydrated-history.git
  GTM_GIT_URL=/store/rehydrated-history.git GTM_FENCE_ACKNOWLEDGED=true \
    /usr/local/bin/reseed-history.sh
  for ref in main gtm-recovery gtm-infra; do
    git --git-dir=/store/rehydrated-history.git rev-parse --verify "$ref^{commit}" >/dev/null
  done
  test "$(git --git-dir=/store/rehydrated-history.git cat-file -s main:full-encrypted-opaque-blob.bin)" -eq 8388608
  git --git-dir=/store/rehydrated-history.git show gtm-recovery:identity.tar.gz.age > /store/recovered.age
  age -d -i /store/age-a.key -o /store/recovered.tar.gz /store/recovered.age
  tar -tzf /store/recovered.tar.gz | grep -q key.pem
'
echo 'PASS: Full encrypted Git bytes, identity and two encrypted config branches recover A from B and B from A without any Git LFS.'
