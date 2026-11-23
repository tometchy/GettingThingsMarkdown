#!/bin/sh
set -eu
export GIT_DIR=/data/git GIT_WORK_TREE=/data/files
export GIT_ASKPASS=/usr/local/bin/askpass.sh
export GIT_TERMINAL_PROMPT=0
export GIT_SSL_CAINFO=/tls/tls.crt
while :; do
  # No plaintext is generated here. Only the encrypted filesystem snapshot
  # received by the untrusted Syncthing server is tracked by Git.
  if git add -A -- .; then
    if ! git diff --cached --quiet; then
      if ! git commit -q -m "Encrypted snapshot $(date -u '+%Y-%m-%dT%H:%M:%SZ')"; then
        echo "Git encrypted commit failed" >&2
      fi
    fi
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
      # Use Git over HTTPS with pinned CA; credentials supplied by askpass.
      if ! git push origin HEAD:refs/heads/main; then
        echo "Git HTTPS push pending; retry on next cycle" >&2
      else
        : # Intentionally retain every local encrypted Git LFS object.
        # DO NOT prune the local Git LFS objects. The second cloud's
        # Gitea stores pointers while opaque LFS bytes live in S3. Local
        # retained objects form the independent encrypted historical copy.
        # A durable 7-day purge/undo/refcount workflow must safely GC later.
      fi
    fi
  else
    echo "Failed to enumerate encrypted filesystem; retrying" >&2
  fi
  sleep 12
done
