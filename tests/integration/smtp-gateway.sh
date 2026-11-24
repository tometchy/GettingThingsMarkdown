#!/usr/bin/env bash
# End-to-end mail over real STARTTLS to the shared SMTP pod, through the
# Control Plane's public-key queue and authenticated client fetch.
set -euo pipefail
: "${GTM_API_URL:=http://127.0.0.1:5081}"
: "${GTM_SMTP_HOST:=127.0.0.1}"
: "${GTM_SMTP_PORT:=2525}"

tmp="$(mktemp -d)"
native_container="gtmd-mail-ci-$RANDOM"
cleanup() {
  docker rm -f "$native_container" >/dev/null 2>&1 || true
  if [ -n "${mail_pf:-}" ]; then kill "$mail_pf" 2>/dev/null || true; fi
  rm -rf "$tmp" 2>/dev/null || true
}
trap cleanup EXIT
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072   -out "$tmp/private.pem" >/dev/null 2>&1
openssl pkey -in "$tmp/private.pem" -pubout -outform DER   -out "$tmp/public.der" >/dev/null 2>&1
public="$(base64 -w0 < "$tmp/public.der")"
email="smtp-ci-$(openssl rand -hex 8)@example.test"
registration="$(curl --fail-with-body -fsS "$GTM_API_URL/api/auth/register"   -H 'Content-Type: application/json'   -d "$(jq -nc --arg email "$email" '{
    email:$email,password:"long test password for mail",plan:"plus"
  }')")"
bearer="$(jq -r '.accessToken' <<< "$registration")"
account="$(jq -r '.account.id' <<< "$registration")"
for i in $(seq 1 60); do
  status="$(curl -fsS -H "Authorization: Bearer $bearer" "$GTM_API_URL/api/tenant" | jq -r '.status')"
  [ "$status" = Ready ] && break
  sleep 5
done
[ "$status" = Ready ]
[ "$bearer" != null ] && [ -n "$bearer" ]
inbox="$(curl --fail-with-body -fsS "$GTM_API_URL/api/mail/inbox"   -H "Authorization: Bearer $bearer" -H 'Content-Type: application/json'   -d "$(jq -nc --arg publicKeyBase64 "$public" '{publicKeyBase64:$publicKeyBase64}')")"
recipient="$(jq -r '.address' <<< "$inbox")"
[[ "$recipient" == r*@inbox.test.invalid ]]

GTM_TEST_MAIL_RECIPIENT="$recipient" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
msg = EmailMessage()
msg['From'] = 'sender@example.test'
msg['To'] = os.environ['GTM_TEST_MAIL_RECIPIENT']
msg['Subject'] = 'Forwarded Gmail confirmation test'
msg.set_content('Confirmation URL: https://mail.google.com/confirm-gtm-ci')
msg.add_attachment(b"%PDF-1.4 test binary", maintype="application", subtype="pdf", filename="test.pdf")
with smtplib.SMTP(os.environ.get('GTM_SMTP_HOST', '127.0.0.1'),
                  int(os.environ.get('GTM_SMTP_PORT', '2525')), timeout=20) as smtp:
    smtp.ehlo()
    if not smtp.has_extn('STARTTLS'):
        raise AssertionError('No STARTTLS offered')
    smtp.starttls(context=ssl._create_unverified_context())
    smtp.ehlo()
    smtp.send_message(msg)
    smtp.mail('sender@example.test')
    code, _ = smtp.rcpt('ordinary@evil.test')
    assert code == 550, (code, 'Open relay risk')
    code, _ = smtp.rcpt('r' + '0' * 48 + '@inbox.test.invalid')
    assert code == 550, (code, 'Unknown random alias accepted')
PY

messages="$(curl --fail-with-body -fsS "$GTM_API_URL/api/mail/messages"   -H "Authorization: Bearer $bearer")"
jq -e 'length == 1 and .[0].ciphertext != null and .[0].wrappedKey != null'   <<< "$messages" >/dev/null
if grep -q 'mail.google.com/confirm-gtm-ci' <<< "$messages"; then
  echo "Unencrypted mail leaked into API response" >&2
  exit 1
fi
id="$(jq -r '.[0].id' <<< "$messages")"
ns="gtm-${account//-/}"
kubectl -n "$ns" exec sync-0 -- git --git-dir=/data/git ls-tree -r --name-only gtm-mail | grep -q '^.gtmd/inbox/'
kubectl -n "$ns" exec history-0 -- git --git-dir=/var/lib/gitea/git/repositories/archive/history.git ls-tree -r --name-only gtm-mail | grep -q '^.gtmd/inbox/'
# Independently exercise real Syncthing protocol transport, not the API download.
mkdir -p "$tmp/client-state" "$tmp/client-files"
chmod 777 "$tmp/client-state" "$tmp/client-files"
native_key="$(openssl rand -hex 24)"
kubectl -n "$ns" port-forward svc/sync 22015:22001 >"$tmp/mail-forward.log" 2>&1 &
mail_pf=$!
docker run -d --name "$native_container" --network host -v "$tmp/client-state:/state" -v "$tmp/client-files:/files" \
  --entrypoint /bin/syncthing ghcr.io/syncthing/syncthing:2.1.7 \
  serve --home=/state --no-browser --gui-address=127.0.0.1:8399 --gui-apikey="$native_key" >/dev/null
st() { curl -fsS --connect-timeout 3 -H "X-API-Key: $native_key" "$@"; }
for i in $(seq 1 50); do
  if st http://127.0.0.1:8399/rest/system/status >"$tmp/native.json" 2>/dev/null; then break; fi
  sleep 1
done
client="$(jq -er .myID "$tmp/native.json")"
pair="$(curl --fail-with-body -fsS -X POST "$GTM_API_URL/api/devices" -H "Authorization: Bearer $bearer" -H 'Content-Type: application/json' -d "$(jq -nc --arg id "$client" '{deviceId:$id,name:"mail-ci"}')")"
peer="$(jq -er .mailDeviceId <<< "$pair")"
primary="$(jq -er .serverDeviceId <<< "$pair")"
[ "$peer" != "$primary" ]
st http://127.0.0.1:8399/rest/config/defaults/device | jq -c --arg id "$peer" '.deviceID=$id | .untrusted=false | .addresses=["tcp://127.0.0.1:22015"]' | st -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8399/rest/config/devices >/dev/null
st http://127.0.0.1:8399/rest/config/defaults/folder | jq -c --arg own "$client" --arg peer "$peer" '.id="gtmd-mail" | .label="Mail CI" | .path="/files" | .type="receiveonly" | .devices=[{"deviceID":$own},{"deviceID":$peer}]' | st -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8399/rest/config/folders >/dev/null
received=""
for i in $(seq 1 60); do
  received="$(find "$tmp/client-files" -maxdepth 1 -name '*.json' -type f -print -quit)"
  [ -n "$received" ] && jq -e --arg id "$id" '.id == $id' "$received" >/dev/null 2>&1 && break
  sleep 2
done
[ -n "$received" ]
jq -e --arg id "$id" '.id == $id' "$received" >/dev/null
jq -e '.ciphertext != null' "$received" >/dev/null
echo "Native Syncthing ciphertext-only mailbox received"
curl --fail-with-body -fsS -X DELETE "$GTM_API_URL/api/mail/messages/$id"   -H "Authorization: Bearer $bearer" >/dev/null
curl --fail-with-body -fsS "$GTM_API_URL/api/mail/messages"   -H "Authorization: Bearer $bearer" | jq -e 'length == 0' >/dev/null
echo "SMTP STARTTLS -> encrypted queue -> authenticated ACK PASS"
