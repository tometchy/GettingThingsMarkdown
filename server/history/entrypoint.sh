#!/bin/sh
set -eu
: "${GTM_GITEA_ADMIN_PASSWORD:?Missing admin secret}"
: "${GTM_GITEA_SYNC_PASSWORD:?Missing sync writer secret}"
: "${GTM_GITEA_READER_PASSWORD:?Missing read-only secret}"
: "${GITEA__server__DOMAIN:?Missing service DNS}"
/usr/local/bin/docker-setup.sh
GITEA="/usr/local/bin/gitea -c ${GITEA_APP_INI}"
$GITEA migrate

if ! $GITEA admin user list | grep -Eq '[[:space:]]archive[[:space:]]'; then
  $GITEA admin user create --username archive --email archive@localhost.invalid \
    --password "$GTM_GITEA_ADMIN_PASSWORD" --admin --must-change-password=false
fi
if ! $GITEA admin user list | grep -Eq '[[:space:]]reader[[:space:]]'; then
  $GITEA admin user create --username reader --email reader@localhost.invalid \
    --password "$GTM_GITEA_READER_PASSWORD" --must-change-password=false
fi
if ! $GITEA admin user list | grep -Eq '[[:space:]]sync[[:space:]]'; then
  $GITEA admin user create --username sync --email sync@localhost.invalid \
    --password "$GTM_GITEA_SYNC_PASSWORD" --must-change-password=false
fi

$GITEA web &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' INT TERM EXIT

# Use the exact Kubernetes Service hostname for correct TLS verification.
HOST="$GITEA__server__DOMAIN"
ROOT="https://${HOST}:3000"
CURL="curl --silent --show-error --fail --cacert /tls/tls.crt --resolve ${HOST}:3000:127.0.0.1 --noproxy '*'"
count=0
until $CURL --user "archive:$GTM_GITEA_ADMIN_PASSWORD" "$ROOT/api/v1/version" >/dev/null 2>&1; do
  count=$((count+1))
  if [ "$count" -gt 90 ]; then echo "Gitea did not become healthy" >&2; exit 1; fi
  sleep 2
done

if ! $CURL --user "archive:$GTM_GITEA_ADMIN_PASSWORD" \
   "$ROOT/api/v1/repos/archive/history" >/dev/null 2>&1; then
  $CURL --user "archive:$GTM_GITEA_ADMIN_PASSWORD" \
    -H 'Content-Type: application/json' \
    -d '{"name":"history","private":true,"auto_init":false,"default_branch":"main"}' \
    "$ROOT/api/v1/user/repos" >/dev/null
fi
$CURL --user "archive:$GTM_GITEA_ADMIN_PASSWORD" -X PUT \
  -H 'Content-Type: application/json' -d '{"permission":"write"}' \
  "$ROOT/api/v1/repos/archive/history/collaborators/sync" >/dev/null
$CURL --user "archive:$GTM_GITEA_ADMIN_PASSWORD" -X PUT \
  -H 'Content-Type: application/json' -d '{"permission":"read"}' \
  "$ROOT/api/v1/repos/archive/history/collaborators/reader" >/dev/null

# A's Git credential only has write access as a collaborator, not repository
# ownership. Reject force pushes and branch deletion even before protection API.
REPO="/var/lib/gitea/git/repositories/archive/history.git"
if [ -d "$REPO" ]; then
  git --git-dir="$REPO" config receive.denyNonFastForwards true
  git --git-dir="$REPO" config receive.denyDeletes true
  git --git-dir="$REPO" config receive.fsckObjects true
  git --git-dir="$REPO" config transfer.fsckObjects true
fi

if [ "${GTM_WORM_BACKUP_ENABLED:-false}" = "true" ]; then
  /usr/local/bin/backup-loop.sh &
  echo 'WORM backup worker started (same History container, no sidecar).'
fi
python3 /usr/local/bin/git_integrity.py watch "$REPO" /var/lib/gitea/integrity.json --role history &
python3 /usr/local/bin/git_integrity.py watchdog /var/lib/gitea/integrity.json --role history &
echo 'Gitea archive initialized. Waiting for server.'
wait "$server_pid"
