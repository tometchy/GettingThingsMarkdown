#!/usr/bin/env bash
set -euo pipefail
# Billing/account vertical slice only. Kubernetes + Syncthing orchestration is not yet implemented.
cd "$(dirname "$0")"
case "${1:-help}" in
  api)
    : "${GTM_JWT_SECRET:?Set GTM_JWT_SECRET, e.g. export GTM_JWT_SECRET=\$(openssl rand -hex 32)}"
    export ASPNETCORE_ENVIRONMENT="${ASPNETCORE_ENVIRONMENT:-Development}"
    exec dotnet run --project src/ControlPlane --urls http://localhost:5080
    ;;
  desktop)
    exec dotnet run --project src/Client/Desktop
    ;;
  browser)
    exec dotnet run --project src/Client/Browser
    ;;
  test)
    exec dotnet test tests/ControlPlane.Tests/ControlPlane.Tests.csproj
    ;;
  *)
    echo "Available: ./dev.sh api | desktop | browser | test"
    echo "Required Kubernetes/Syncthing setup, up, down, and E2EE tests are NOT implemented."
    exit 1
    ;;
esac
