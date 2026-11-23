#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
mkdir -p "$root"/data/{syncthing,recovery}
chmod -R 0777 "$root"
# All age operations run in the exact production Sync image; do not install
# age on the CI runner or contact any external storage provider.
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/work" --entrypoint sh gtm/sync:ci -ec '
  age-keygen -o /work/identity-a.key >/dev/null 2>&1
  age-keygen -o /work/identity-b.key >/dev/null 2>&1
  git init -q --bare /work/history.git
'
recipient_a="$(sed -n 's/^# public key: //p' "$root/identity-a.key")"
recipient_b="$(sed -n 's/^# public key: //p' "$root/identity-b.key")"
[[ "$recipient_a" == age1* && "$recipient_b" == age1* && "$recipient_a" != "$recipient_b" ]] || {
  echo "Two distinct age recipients required" >&2; exit 1;
}
echo '<configuration><device>EXPECTED_IDENTITY</device></configuration>' >"$root/data/syncthing/config.xml"
printf 'TEST_CERT_MUST_NOT_LEAK\n' >"$root/data/syncthing/cert.pem"
printf 'TEST_PRIVATE_KEY_MUST_NOT_LEAK\n' >"$root/data/syncthing/key.pem"
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/work" -v "$root/data:/data" \
  -e GTM_RECOVERY_AGE_RECIPIENT="$recipient_a,$recipient_b" \
  -e GTM_GIT_URL=/work/history.git \
  --entrypoint sh gtm/sync:ci -ec '/usr/local/bin/identity-backup-once.sh'
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/work" --entrypoint sh gtm/sync:ci -ec '
  git --git-dir=/work/history.git show gtm-recovery:identity.tar.gz.age > /work/identity.tar.gz.age
  age -d -i /work/identity-a.key -o /work/identity.tar.gz /work/identity.tar.gz.age
  age -d -i /work/identity-b.key -o /work/identity-b.tar.gz /work/identity.tar.gz.age
  mkdir /work/restored
  tar -xzf /work/identity.tar.gz -C /work/restored
'
cmp "$root/identity.tar.gz" "$root/identity-b.tar.gz"
cmp "$root/data/syncthing/config.xml" "$root/restored/config.xml"
cmp "$root/data/syncthing/cert.pem" "$root/restored/cert.pem"
cmp "$root/data/syncthing/key.pem" "$root/restored/key.pem"
if grep -aq 'TEST_PRIVATE_KEY_MUST_NOT_LEAK' "$root/identity.tar.gz.age"; then
  echo 'SECURITY: plaintext private key in remote Git blob!' >&2
  exit 1
fi
echo 'PASS: Sync pushed age-encrypted identity to B and recovered same config/certificate/key.'
