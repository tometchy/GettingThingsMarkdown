#!/usr/bin/env bash
set -euo pipefail
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
chmod 0777 "$root"
mkdir -p "$root"/{data,scratch,config/account}
chmod -R 0777 "$root"
python3 - "$root" <<'PY'
import pathlib, sqlite3, sys
p=pathlib.Path(sys.argv[1])
for name in ("accounts.sqlite","tenants.sqlite"):
    with sqlite3.connect(p/"data"/name) as db:
        db.execute("CREATE TABLE preserved (value TEXT)")
        db.execute("INSERT INTO preserved VALUES (?)", ("sensitive-test-state-"+name,))
(p/"config/account/jwt").write_text("sensitive-test-secret")
PY
run() { docker run --rm --user "$(id -u):$(id -g)" -v "$root:/test" --entrypoint sh gtm/controlplane-backup:ci -ec "$1"; }
run '
  age-keygen -o /test/a.key
  age-keygen -o /test/b.key
  a="$(age-keygen -y /test/a.key)"
  b="$(age-keygen -y /test/b.key)"
  python3 /usr/local/bin/gtm-backup.py create --data /test/data --config /test/config --output /test/backup.age --scratch /test/scratch --recipient "$a" --recipient "$b"
  if grep -aq sensitive-test /test/backup.age; then exit 1; fi
  for who in a b; do
    python3 /usr/local/bin/gtm-backup.py restore --bundle /test/backup.age --identity "/test/$who.key" --target "/test/restored-$who" --scratch /test/scratch
  done
  if python3 /usr/local/bin/gtm-backup.py restore --bundle /test/backup.age --identity /test/a.key --target /test/data --scratch /test/scratch; then exit 1; fi
'
python3 - "$root" <<'PY'
import pathlib, sqlite3, sys
p=pathlib.Path(sys.argv[1])
for who in ("a","b"):
    restored=p/("restored-"+who)
    for name in ("accounts.sqlite","tenants.sqlite"):
        with sqlite3.connect(restored/name) as db:
            assert db.execute("SELECT value FROM preserved").fetchone()[0]=="sensitive-test-state-"+name
    assert (restored/"config--account--jwt").read_text()=="sensitive-test-secret"
PY
echo "PASS: encrypted shared state can be restored with either independent age key, refuses live overwrite"
