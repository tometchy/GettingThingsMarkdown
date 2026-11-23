#!/bin/sh
set -eu
: "${GTM_SYNCTHING_API_KEY:?Missing Syncthing API key}"
: "${GTM_GIT_URL:?Missing Git HTTPS destination}"
: "${GTM_GIT_PASSWORD:?Missing Git writer password}"
mkdir -p /data/files /data/git /data/syncthing

# This config, including cert.pem and key.pem, lives on a persistent PVC.
# Syncthing must never generate a new Device ID on container restart.
if [ ! -f /data/syncthing/config.xml ]; then
  syncthing generate --home=/data/syncthing
fi

export GIT_DIR=/data/git
export GIT_WORK_TREE=/data/files
if [ ! -f /data/git/HEAD ]; then
  git init -q -b main
  git config user.name 'GettingThingsMarkdown encrypted archive'
  git config user.email 'archive@localhost.invalid'
fi
git config --local core.bare false
git config --local core.worktree /data/files
git config --local user.name 'GettingThingsMarkdown encrypted archive'
if [ "${GTM_GIT_LFS_ENABLED:-false}" = "true" ]; then
  # Git attributes remain outside the Syncthing directory so no clear-text
  # infrastructure metadata enters the folder sent to the encrypted device.
  # Git LFS stores encrypted file contents as S3 objects in History (Gitea).
  git lfs install --local
  printf '%s\n' '* filter=lfs diff=lfs merge=lfs -text' > /data/git/info/attributes
fi
git config --local user.email 'archive@localhost.invalid'
# Git's exclusion metadata lives outside the Syncthing folder.
printf '%s\n' '.stfolder' '.stversions/' '.stignore' '.syncthing.*' > /data/git/info/exclude
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$GTM_GIT_URL"
else
  git remote add origin "$GTM_GIT_URL"
fi
/usr/local/bin/commit-loop.sh &
# The administrative REST API is reachable only over a private cluster Service
# restricted by NetworkPolicy. It is not exposed through public ingress.
exec syncthing serve --home=/data/syncthing --no-browser \
  --gui-address=0.0.0.0:8384 --gui-apikey="$GTM_SYNCTHING_API_KEY"
