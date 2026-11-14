#!/usr/bin/env bash
# Atajos de desarrollo local de AegisVolt.
#   ./scripts/dev.sh up        levanta Floci (persistente) y espera a que esté sano
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
  down)
    docker compose down
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
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
