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
curl --fail-with-body -fsS -X DELETE "$GTM_API_URL/api/mail/messages/$id"   -H "Authorization: Bearer $bearer" >/dev/null
curl --fail-with-body -fsS "$GTM_API_URL/api/mail/messages"   -H "Authorization: Bearer $bearer" | jq -e 'length == 0' >/dev/null
echo "SMTP STARTTLS -> encrypted queue -> authenticated ACK PASS"
