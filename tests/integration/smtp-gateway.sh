#!/usr/bin/env bash
# End-to-end mail over real STARTTLS to the shared SMTP pod, through the
# Control Plane's public-key queue and authenticated client fetch.
set -euo pipefail
: "${GTM_API_URL:=http://127.0.0.1:5081}"
: "${GTM_SMTP_HOST:=127.0.0.1}"
: "${GTM_SMTP_PORT:=2525}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
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
                  int(os.environ.get('GTM_SMTP_PORT', '2525')), timeout=120) as smtp:
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

# Discover directly from the read-only Gitea history API, not a queue.
listing="$(curl -fsS "$GTM_API_URL/api/history/mail?page=1" -H "Authorization: Bearer $bearer")"
revision="$(jq -er '.revision' <<< "$listing")"
digest="$(jq -er '.digests[0]' <<< "$listing")"
file="$(curl -fsS "$GTM_API_URL/api/history/mail/$digest?revision=$revision" -H "Authorization: Bearer $bearer")"
jq -e '.ciphertext != null and .wrappedKey != null' <<< "$file" >/dev/null
! grep -q 'mail.google.com/confirm-gtm-ci' <<< "$file"
ns="gtm-${account//-/}"
a="$(kubectl -n "$ns" exec sync-0 -- git --git-dir=/data/git rev-parse gtm-mail)"
b="$(kubectl -n "$ns" exec history-0 -- git --git-dir=/var/lib/gitea/git/repositories/archive/history.git rev-parse gtm-mail)"
[ "$a" = "$b" ] && [ "$a" = "$revision" ]
kubectl -n "$ns" exec sync-0 -- git --git-dir=/data/git show "gtm-mail:.gtmd/inbox/$digest.json" >"$tmp/local.json"
printf '%s' "$file" >"$tmp/api.json"
cmp "$tmp/local.json" "$tmp/api.json"
# No additional Syncthing mailbox peer or plaintext workspace injection.
kubectl -n "$ns" exec sync-0 -- sh -ec 'test ! -e /data/mail-syncthing/config.xml; test ! -e /data/files/.gtmd/inbox'
curl -fsS "$GTM_API_URL/api/mail/messages" -H "Authorization: Bearer $bearer" | jq -e 'length == 0' >/dev/null
# Query the actual read-only account storage API. Fixed-length request bodies
# are also used by the reconciler updating quota before SMTP delivery.
curl -fsS "$GTM_API_URL/api/storage" -H "Authorization: Bearer $bearer" | jq -e '.quotaBytes > 0 and .historyBytes > 0' >/dev/null
echo "PASS: SMTP STARTTLS -> both Git replicas -> authenticated read-only Git file (no mailbox peer or queue)"
