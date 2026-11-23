#!/bin/sh
# Operator-invoked ONLY once the old Gitea writer is independently fenced.
# Restores B from complete local Git on A after new Gitea+SQLite bootstrap.
set -eu
unset GIT_DIR GIT_WORK_TREE
: "${GTM_GIT_URL:?}"
[ "${GTM_FENCE_ACKNOWLEDGED:-false}" = true ] || {
  echo 'Refusing Gitea reseed without fencing acknowledgement' >&2; exit 1;
}
export GIT_ASKPASS=/usr/local/bin/askpass.sh GIT_TERMINAL_PROMPT=0
[ ! -f /tls/tls.crt ] || export GIT_SSL_CAINFO=/tls/tls.crt
git --git-dir=/data/git rev-parse --verify refs/heads/main >/dev/null
for branch in settings infra; do
  git -C "/data/recovery/$branch" rev-parse --verify HEAD >/dev/null ||
    { echo "Missing encrypted $branch recovery Git" >&2; exit 1; }
done
GIT_DIR=/data/git GIT_WORK_TREE=/data/files git push origin refs/heads/main:refs/heads/main
git -C /data/recovery/settings push origin HEAD:refs/heads/gtm-recovery
git -C /data/recovery/infra push origin HEAD:refs/heads/gtm-infra
echo 'Re-seeded full Git and encrypted identity/secrets to new Gitea. Validate ref OIDs on both ends.'
