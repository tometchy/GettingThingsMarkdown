#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .gtm
cluster=gtm
export KIND_EXPERIMENTAL_PROVIDER=podman

need() { command -v "$1" >/dev/null || { echo "Missing tool: $1" >&2; exit 1; }; }

cluster_up() {
  if ! kind get clusters 2>/dev/null | grep -qx "$cluster"; then
    kind create cluster --config infrastructure/kind/config.yaml --name "$cluster"
  fi
}

forward_api() {
  if [ -f .gtm/port-forward.pid ] && kill -0 "$(cat .gtm/port-forward.pid)" 2>/dev/null; then
    echo "API port-forward already running"; return
  fi
  nohup kubectl -n gtm-system port-forward --address 127.0.0.1 service/controlplane 5080:8080 \
    >.gtm/port-forward.log 2>&1 </dev/null &
  echo "$!" > .gtm/port-forward.pid
  echo "API port forward started at http://127.0.0.1:5080"
}

case "${1:-help}" in
  setup)
    need podman; need kind; need kubectl; need openssl; need jq; need curl
    if [ "$(podman info --format '{{.Host.Security.Rootless}}')" != "true" ]; then
      echo "Podman must run rootless (without sudo)." >&2; exit 1
    fi
    if [ "$(podman info --format '{{.Host.CgroupVersion}}')" != "v2" ]; then
      echo "kind rootless requires cgroups v2." >&2; exit 1
    fi
    echo "Rootless Podman + cgroups v2 OK. Run ./dev.sh up"
    ;;
  up)
    "$0" setup
    cluster_up
    echo "Building the control-plane and tenant images..."
    podman build -f src/ControlPlane/Dockerfile -t gtm/controlplane:dev .
    podman build -f server/sync/Dockerfile -t gtm/sync:dev .
    podman build -f server/history/Dockerfile -t gtm/history:dev .
    kind load docker-image --name "$cluster" gtm/controlplane:dev gtm/sync:dev gtm/history:dev
    kubectl apply -f infrastructure/kubernetes/control-plane.yaml
    if ! kubectl -n gtm-system get secret controlplane-secrets >/dev/null 2>&1; then
      kubectl -n gtm-system create secret generic controlplane-secrets \
        --from-literal=jwt="$(openssl rand -hex 32)" \
        --from-literal=admin="$(openssl rand -hex 32)"
    fi
    kubectl -n gtm-system rollout restart deployment/controlplane
    kubectl -n gtm-system rollout status deployment/controlplane --timeout=240s
    forward_api
    ;;
  mail-up)
    need podman; need kind; need kubectl; need openssl
    if ! kind get clusters 2>/dev/null | grep -qx "$cluster"; then
      echo "Run ./dev.sh up first." >&2; exit 1
    fi
    mkdir -p .gtm
    podman build -f src/MailIngress/Dockerfile -t gtm/mail-ingress:dev .
    kind load docker-image --name "$cluster" gtm/mail-ingress:dev
    if ! kubectl -n gtm-system get secret gtm-mail-tls >/dev/null 2>&1; then
      openssl req -new -newkey rsa:3072 -sha256 -days 14 -nodes -x509 \
        -subj "/CN=inbox.test.invalid" -keyout .gtm/mail-tls.key \
        -out .gtm/mail-tls.crt >/dev/null 2>&1
      chmod 600 .gtm/mail-tls.key
      kubectl -n gtm-system create secret tls gtm-mail-tls \
        --cert=.gtm/mail-tls.crt --key=.gtm/mail-tls.key
      rm -f .gtm/mail-tls.key .gtm/mail-tls.crt
    fi
    if ! kubectl -n gtm-system get secret gtm-mail-secret >/dev/null 2>&1; then
      kubectl -n gtm-system create secret generic gtm-mail-secret \
        --from-literal=mail-ingress-token="$(openssl rand -hex 32)"
    fi
    token="$(kubectl -n gtm-system get secret gtm-mail-secret \
      -o go-template='{{index .data "mail-ingress-token"}}' | base64 -d)"
    kubectl -n gtm-system set env deployment/controlplane \
      GTM_INBOUND_MAIL_DOMAIN=inbox.test.invalid GTM_MAIL_INGRESS_TOKEN="$token"
    kubectl -n gtm-system rollout status deployment/controlplane --timeout=240s
    kubectl apply -f infrastructure/kubernetes/mail-ingress-dev.yaml
    kubectl -n gtm-system rollout status deployment/mail-ingress --timeout=240s
    if [ -f .gtm/smtp-port-forward.pid ] &&
       kill -0 "$(cat .gtm/smtp-port-forward.pid)" 2>/dev/null; then
      echo "Local SMTP port-forward already running"
    else
      nohup kubectl -n gtm-system port-forward --address 127.0.0.1 \
        service/mail-ingress 2525:2525 >.gtm/smtp-port-forward.log 2>&1 </dev/null &
      echo "$!" >.gtm/smtp-port-forward.pid
    fi
    echo "Development SMTP on 127.0.0.1:2525 (self-signed STARTTLS; do not expose publicly)."
    echo "Alias domain: inbox.test.invalid. No public mail DNS or MX configured."
    ;;
  mail-down)
    if [ -f .gtm/smtp-port-forward.pid ]; then
      kill "$(cat .gtm/smtp-port-forward.pid)" 2>/dev/null || true
      rm -f .gtm/smtp-port-forward.pid
    fi
    echo "Local SMTP port-forward stopped. Encrypted queue and pods retained."
    ;;
  api)
    : "${GTM_JWT_SECRET:?Set GTM_JWT_SECRET to a random 32+-byte secret}"
    export ASPNETCORE_ENVIRONMENT="${ASPNETCORE_ENVIRONMENT:-Development}"
    exec dotnet run --project src/ControlPlane --urls http://127.0.0.1:5080
    ;;
  desktop)
    exec dotnet run --project src/Client/Desktop
    ;;
  browser)
    exec dotnet run --project src/Client/Browser
    ;;
  web-publish)
    dotnet publish src/Client/Browser/GettingThingsMarkdown.Client.Browser.csproj -c Release
    mkdir -p src/ControlPlane/wwwroot/app
    cp -a src/Client/Browser/bin/Release/net10.0-browser/publish/wwwroot/. src/ControlPlane/wwwroot/app/
    ;;
  create-user)
    need curl; need jq
    email="${2:?Usage: ./dev.sh create-user email@example.com [basic|plus|pro]}"
    plan="${3:-basic}"
    if [ -z "${GTM_NEW_USER_PASSWORD:-}" ]; then
      read -rsp "New password (min. 12 chars): " GTM_NEW_USER_PASSWORD
      echo
    fi
    jq -n --arg email "$email" --arg password "$GTM_NEW_USER_PASSWORD" --arg plan "$plan" \
      '{email:$email,password:$password,plan:$plan}' | \
    curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- \
      http://127.0.0.1:5080/api/auth/register | jq '{account: .account, accessToken: .accessToken}'
    ;;
  status)
    kubectl get pods -A -o wide
    kubectl get pvc -A
    ;;
  logs)
    kubectl -n gtm-system logs deploy/controlplane --tail=200
    ;;
  test)
    exec dotnet test tests/ControlPlane.Tests/ControlPlane.Tests.csproj -c Release
    ;;
  integration)
    exec bash tests/integration/kind-smoke.sh
    ;;
  expose-sync)
    account="${2:?Account UUID is required}"
    port="${3:-22000}"
    if ! [[ "$account" =~ ^[a-fA-F0-9-]{36}$ ]]; then echo "Invalid UUID" >&2; exit 1; fi
    exec kubectl -n "gtm-${account//-/}" port-forward --address 127.0.0.1 svc/sync "$port:22000"
    ;;
  down)
    if [ -f .gtm/port-forward.pid ]; then
      kill "$(cat .gtm/port-forward.pid)" 2>/dev/null || true
      rm -f .gtm/port-forward.pid
    fi
    if [ -f .gtm/smtp-port-forward.pid ]; then
      kill "$(cat .gtm/smtp-port-forward.pid)" 2>/dev/null || true
      rm -f .gtm/smtp-port-forward.pid
    fi
    echo "Port-forwards stopped. Kubernetes cluster, workloads and data preserved."
    ;;
  destroy)
    if [ "${2:-}" != "--yes-delete-everything" ]; then
      echo "Destructive operation. Use: ./dev.sh destroy --yes-delete-everything" >&2; exit 1
    fi
    "$0" down
    kind delete cluster --name "$cluster"
    echo "The local kind cluster and its PVCs were deleted."
    ;;
  *)
    echo "Commands: setup up mail-up mail-down create-user status logs test integration expose-sync down destroy"
    echo "Developer: api desktop browser web-publish"
    exit 1
    ;;
esac
