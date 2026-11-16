#!/usr/bin/env bash
# Abre la consola web de cada ambiente en un puerto local fijo (port-forward al Service frontend).
#   ./scripts/ui.sh start     dev → http://127.0.0.1:3001 · test → :3002 · prod → :3003
#   ./scripts/ui.sh stop
#   ./scripts/ui.sh token <env>   imprime el X-Admin-Token del ambiente (para cambiar flags)
# Docker Desktop no expone la IP de minikube al host; por eso se usa port-forward.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/.run"
declare -A PORTS=([dev]=3001 [test]=3002 [prod]=3003)
mkdir -p "$RUN"

case "${1:-}" in
  start)
    for env in dev test prod; do
      pidfile="$RUN/ui-$env.pid"
      if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then continue; fi
      # port-forward se ata a UN pod: si un despliegue lo reemplaza, se corta. El bucle lo
      # reabre solo (importante durante la demo, cuando el canary cambia pods).
      nohup bash -c "while true; do kubectl port-forward -n aegisvolt-$env svc/frontend ${PORTS[$env]}:80; sleep 1; done" \
        >"$RUN/ui-$env.log" 2>&1 &
      echo $! >"$pidfile"
    done
    sleep 2
    for env in dev test prod; do
      if curl -fs -o /dev/null "http://127.0.0.1:${PORTS[$env]}/healthz"; then
        echo "  $env  → http://127.0.0.1:${PORTS[$env]}"
      else
        echo "  $env  → sin respuesta (¿desplegado? ver .run/ui-$env.log)"
      fi
    done
    ;;
  stop)
    for env in dev test prod; do
      if [[ -f "$RUN/ui-$env.pid" ]]; then
        pid="$(cat "$RUN/ui-$env.pid")"
        pkill -P "$pid" 2>/dev/null || true # el kubectl hijo del bucle
        kill "$pid" 2>/dev/null || true
      fi
      rm -f "$RUN/ui-$env.pid"
    done
    echo "Port-forwards detenidos"
    ;;
  token)
    env="${2:?Uso: ui.sh token <dev|test|prod>}"
    kubectl get secret aegisvolt-admin -n "aegisvolt-$env" -o jsonpath='{.data.token}' | base64 -d
    echo
    ;;
  *) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
