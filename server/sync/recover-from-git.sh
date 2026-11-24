#!/bin/sh
# DESTRUCTIVE IF RUN AGAINST A LIVE PVC. Fail closed unless called on a fresh
# EMPTY Sync PVC as an operator-controlled one-shot recovery step. No extra pod
# is needed after recovery, and only encrypted Syncthing blobs are in Git.
set -eu
: "${GTM_GIT_URL:?Missing remote full Git history}"
: "${GTM_RECOVERY_AGE_IDENTITY_PATH:?Private age key mounted for ONE-SHOT recovery only}"
[ -s "$GTM_RECOVERY_AGE_IDENTITY_PATH" ] || { echo 'Missing recovery key' >&2; exit 1; }
mkdir -p /data/files /data/git /data/syncthing
[ ! -e /data/git/HEAD ] || { echo 'Refusing restore over a live Git repository' >&2; exit 1; }
[ ! -e /data/syncthing/config.xml ] || { echo 'Refusing restore over a live Syncthing identity' >&2; exit 1; }
[ -z "$(ls -A /data/files)" ] || { echo 'Refusing restore into nonempty encrypted folder' >&2; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM HUP
chmod 0700 "$tmp"
export GIT_DIR=/data/git GIT_WORK_TREE=/data/files
export GIT_ASKPASS=/usr/local/bin/askpass.sh
export GIT_TERMINAL_PROMPT=0
[ ! -f /tls/tls.crt ] || export GIT_SSL_CAINFO=/tls/tls.crt
git init -q -b main
git remote add origin "$GTM_GIT_URL"
git fetch -q origin \
  'refs/heads/main:refs/remotes/origin/main' \
  'refs/heads/gtm-recovery:refs/remotes/origin/gtm-recovery' \
  'refs/heads/gtm-infra:refs/remotes/origin/gtm-infra'
git show refs/remotes/origin/gtm-recovery:identity.tar.gz.age > "$tmp/identity.tar.gz.age"
age -d -i "$GTM_RECOVERY_AGE_IDENTITY_PATH" \
  -o "$tmp/identity.tar.gz" "$tmp/identity.tar.gz.age"
# Only extract expected filenames; never run arbitrary scripts from the bundle.
tar -tzf "$tmp/identity.tar.gz" | sort > "$tmp/list.txt"
printf '%s\n' cert.pem config.xml key.pem | sort > "$tmp/expected.txt"
cmp "$tmp/list.txt" "$tmp/expected.txt" >/dev/null || {
  echo 'Unexpected files in recovery identity bundle' >&2; exit 1;
}
tar -xzf "$tmp/identity.tar.gz" -C /data/syncthing config.xml cert.pem key.pem
chmod 0600 /data/syncthing/key.pem
git reset -q --hard refs/remotes/origin/main
# Restore the optional Git-only mail branch without checking it out.
# A network/authorization failure must not be mistaken for absent mail.
set +e
git ls-remote --exit-code origin refs/heads/gtm-mail >/dev/null
mail_status=$?
set -e
case "$mail_status" in
  0) git fetch -q origin 'refs/heads/gtm-mail:refs/heads/gtm-mail' ;;
  2) : ;; # No matching ref on a legacy tenant
  *) echo 'Cannot verify mailbox ref during recovery' >&2; exit 1 ;;
esac
# Git global GIT_DIR settings would force recovery branch commands to operate
# on the main Git directory. Remove them before cloning separate branches.
unset GIT_DIR GIT_WORK_TREE
# Keep BOTH encrypted configuration branches on the new Sync PVC as well.
# They contain only age ciphertext (never the operator's private age key).
mkdir -p /data/recovery
for pair in 'settings:gtm-recovery' 'infra:gtm-infra'; do
  directory="${pair%%:*}"
  branch="${pair#*:}"
  git clone -q --single-branch --branch "$branch" "$GTM_GIT_URL" "/data/recovery/$directory"
done
[ -s /data/syncthing/key.pem ] && [ -s /data/syncthing/cert.pem ] && [ -s /data/syncthing/config.xml ] || {
  echo 'Incomplete recovered Syncthing identity' >&2; exit 1;
}
echo 'Recovered EXACT full Git ciphertext and Syncthing server identity. Verify peer Device ID before enabling writes.'
