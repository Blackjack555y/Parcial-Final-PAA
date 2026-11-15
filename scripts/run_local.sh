#!/usr/bin/env bash
# Ejecuta los 4 servicios como procesos locales (uvicorn) contra las tablas de un ambiente.
# Útil para desarrollo y para la demo antes de Kubernetes. Requiere .venv y Floci.
#
#   ./scripts/run_local.sh start [dev|test|prod]   arranca (por defecto dev)
#   ./scripts/run_local.sh stop                    detiene todo
#   ./scripts/run_local.sh status                  procesos y health checks
#   ./scripts/run_local.sh logs <servicio>         sigue el log de un servicio
#
# Puertos: gateway 8000 · reservations 8001 · billing 8002 · dispatch 8003
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

declare -A PORTS=(
  [graphql-gateway]=8000
  [station-reservation-service]=8001
  [billing-wallet-service]=8002
  [energy-dispatch-service]=8003
)
declare -A MODULES=(
  [graphql-gateway]=graphql_gateway.main
  [station-reservation-service]=station_reservation.main
  [billing-wallet-service]=billing_wallet.main
  [energy-dispatch-service]=energy_dispatch.main
)
ORDER=(station-reservation-service billing-wallet-service energy-dispatch-service graphql-gateway)

start() {
  local env="${1:-dev}"
  [[ -x "$ROOT/.venv/bin/uvicorn" ]] || { echo "Falta .venv (uv venv + uv pip install)" >&2; exit 1; }
  # Token de administración de flags: se genera una vez y se guarda fuera del repo (.run/).
  local token_file="$RUN_DIR/admin-token"
  [[ -s "$token_file" ]] || python3 -c 'import secrets; print(secrets.token_urlsafe(24))' >"$token_file"
  chmod 600 "$token_file"

  for svc in "${ORDER[@]}"; do
    if [[ -f "$RUN_DIR/$svc.pid" ]] && kill -0 "$(cat "$RUN_DIR/$svc.pid")" 2>/dev/null; then
      echo "$svc ya está corriendo"; continue
    fi
    (
      cd "$ROOT/services/$svc"
      AEGIS_ENV="$env" AEGIS_SERVICE="$svc" AEGIS_VERSION="local-$(git -C "$ROOT" rev-parse --short HEAD)" \
      AEGIS_CLOUDWATCH_LOGS=true \
      AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:4566}" \
      AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 \
      PEAK_HOURS="${PEAK_HOURS:-0-24}" FLAGS_ADMIN_TOKEN="$(cat "$token_file")" \
      CORS_ORIGINS="http://localhost:5173" \
      RESERVATION_URL=http://127.0.0.1:8001 BILLING_URL=http://127.0.0.1:8002 \
      DISPATCH_URL=http://127.0.0.1:8003 \
      nohup "$ROOT/.venv/bin/uvicorn" --factory "${MODULES[$svc]}:create_app" \
        --host 127.0.0.1 --port "${PORTS[$svc]}" --no-access-log \
        >"$RUN_DIR/$svc.log" 2>&1 &
      echo $! >"$RUN_DIR/$svc.pid"
    )
    echo "▶ $svc → http://127.0.0.1:${PORTS[$svc]} (ambiente $env)"
  done
  for _ in $(seq 30); do
    curl -fs http://127.0.0.1:8000/readyz/deep 2>/dev/null | grep -q '"ready":true' && break
    sleep 1
  done
  status
  echo "GraphiQL: http://127.0.0.1:8000/graphql   ·   X-Admin-Token en .run/admin-token"
}

stop() {
  for svc in "${ORDER[@]}"; do
    if [[ -f "$RUN_DIR/$svc.pid" ]]; then
      kill "$(cat "$RUN_DIR/$svc.pid")" 2>/dev/null || true
      rm -f "$RUN_DIR/$svc.pid"
      echo "■ $svc detenido"
    fi
  done
}

status() {
  for svc in "${ORDER[@]}"; do
    local state="detenido"
    if [[ -f "$RUN_DIR/$svc.pid" ]] && kill -0 "$(cat "$RUN_DIR/$svc.pid")" 2>/dev/null; then
      if curl -fs "http://127.0.0.1:${PORTS[$svc]}/healthz" >/dev/null; then state="OK"; else state="sin respuesta"; fi
    fi
    printf '  %-30s :%s  %s\n' "$svc" "${PORTS[$svc]}" "$state"
  done
}

case "${1:-}" in
  start) start "${2:-dev}" ;;
  stop) stop ;;
  status) status ;;
  logs) tail -f "$RUN_DIR/${2:?Uso: run_local.sh logs <servicio>}.log" ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
