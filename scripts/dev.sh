#!/usr/bin/env bash
# Atajos de desarrollo local de AegisVolt.
#   ./scripts/dev.sh up        levanta Floci (persistente) y espera a que esté sano
#   ./scripts/dev.sh up-apps [env]  levanta además los 4 servicios en contenedores (gateway :8000)
#   ./scripts/dev.sh down-apps      detiene los contenedores de los servicios (Floci sigue)
#   ./scripts/dev.sh up-observability  Prefect (UI :4200) + flujos de salud y auditoría
#   ./scripts/dev.sh down      detiene el stack (los datos se conservan)
#   ./scripts/dev.sh reset     detiene el stack y BORRA los datos de Floci (pide confirmación)
#   ./scripts/dev.sh status    estado de los contenedores y de Floci
#   ./scripts/dev.sh smoke     pruebas de humo de Floci (F0-1 a F0-8)
#   ./scripts/dev.sh lint      ruff + shellcheck
#   ./scripts/dev.sh test [unit|integration|e2e|all]
# Usa .venv (Python 3.12) si existe; si no, corre las pruebas en un contenedor python:3.12-slim.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

py() { # Ejecuta un comando con Python 3.12: .venv local o contenedor efímero
  if [[ -x .venv/bin/python ]]; then
    PATH="$ROOT/.venv/bin:$PATH" "$@"
  else
    docker run --rm --network host -v "$ROOT":/src -w /src \
      -u "$(id -u):$(id -g)" -e HOME=/tmp \
      -e AWS_ENDPOINT_URL -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
      python:3.12-slim sh -c \
      'pip install -q --user -r requirements-dev.txt >/dev/null 2>&1 && PATH=$PATH:/tmp/.local/bin "$0" "$@"' "$@"
  fi
}

case "${1:-help}" in
  up)
    docker compose up -d --wait floci
    curl -fs "$AWS_ENDPOINT_URL/_floci/health" >/dev/null && echo "Floci listo en $AWS_ENDPOINT_URL"
    ;;
  up-apps)
    # Token de administración (flags): se genera una vez y vive fuera del repo.
    mkdir -p .run
    [[ -s .run/admin-token ]] || python3 -c 'import secrets; print(secrets.token_urlsafe(24))' >.run/admin-token
    chmod 600 .run/admin-token
    AEGIS_ENV="${2:-dev}" FLAGS_ADMIN_TOKEN="$(cat .run/admin-token)" \
      IMAGE_TAG="$(git rev-parse --short HEAD)" \
      docker compose -f compose.yaml -f compose.apps.yaml up -d --build --wait
    echo "Stack ${2:-dev} listo: http://127.0.0.1:8000/graphql (X-Admin-Token en .run/admin-token)"
    ;;
  down-apps)
    docker compose -f compose.yaml -f compose.apps.yaml stop \
      graphql-gateway station-reservation-service billing-wallet-service energy-dispatch-service
    docker compose -f compose.yaml -f compose.apps.yaml rm -f \
      graphql-gateway station-reservation-service billing-wallet-service energy-dispatch-service
    ;;
  up-observability)
    # Tokens admin de cada ambiente (Secret de Kubernetes) para la remediación opcional.
    for e in dev test prod; do
      tok="$(kubectl get secret aegisvolt-admin -n "aegisvolt-$e" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
      export "ADMIN_TOKEN_${e^^}=$tok"
    done
    docker compose -f compose.yaml -f compose.observability.yaml up -d --build --wait
    echo "Prefect listo: http://127.0.0.1:4200"
    ;;
  down)
    docker compose -f compose.yaml -f compose.apps.yaml -f compose.observability.yaml down
    ;;
  reset)
    read -r -p "Esto borra el estado de Floci (incluido el tfstate en S3). ¿Continuar? [s/N] " ok
    [[ "$ok" == "s" ]] && docker compose down -v
    ;;
  status)
    docker compose ps
    curl -fs "$AWS_ENDPOINT_URL/_floci/health" >/dev/null && echo "Floci: OK" || echo "Floci: caído"
    ;;
  smoke)
    ./scripts/floci_smoke.sh
    ;;
  lint)
    py ruff check .
    py ruff format --check .
    if command -v shellcheck >/dev/null 2>&1; then
      shellcheck scripts/*.sh
    else
      docker run --rm -v "$ROOT":/mnt -w /mnt koalaman/shellcheck:stable scripts/*.sh
    fi
    echo "Lint OK"
    ;;
  test)
    case "${2:-all}" in
      unit) py pytest tests/unit ;;
      integration) py pytest tests/integration ;;
      e2e) py pytest tests/e2e ;;
      all) py pytest ;;
      *) echo "Suite desconocida: $2" >&2; exit 1 ;;
    esac
    ;;
  *)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
