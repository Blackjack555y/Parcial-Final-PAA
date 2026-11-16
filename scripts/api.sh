#!/usr/bin/env bash
# Llama al GraphQL de un ambiente ENTRANDO POR API GATEWAY (recorrido completo de la pregunta 6.1):
#   cliente → API Gateway (stage <env>) → Ingress de Kubernetes (api.<env>.aegisvolt.local)
#           → Service graphql-gateway → pod → microservicios → DynamoDB
#
#   ./scripts/api.sh dev '{ environment { name version } }'
#   ./scripts/api.sh test 'mutation { setFeatureFlag(name: "SIMULATE_CHARGER_HARDWARE_FAULT", enabled: true) { enabled } }' --admin
#   ./scripts/api.sh dev '{ stations { stationId } }' -v      (muestra la URL y las cabeceras)
# En Floci, la URL de invocación es http://localhost:4566/restapis/<api-id>/<stage>/_user_request_/
set -euo pipefail

ENV="${1:?Uso: api.sh <dev|test|prod> '<query GraphQL>' [--admin] [-v]}"
QUERY="${2:?Falta la query GraphQL}"
shift 2
ADMIN=false
VERBOSE=false
for arg in "$@"; do
  case "$arg" in
    --admin) ADMIN=true ;;
    -v) VERBOSE=true ;;
  esac
done
case "$ENV" in dev | test | prod) ;; *) echo "Ambiente inválido: $ENV" >&2; exit 1 ;; esac

ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
API_ID="$(aws --endpoint-url "$ENDPOINT" --region us-east-1 apigateway get-rest-apis \
  --query "items[?name=='aegisvolt-$ENV-public'].id | [0]" --output text)"
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "No existe la API aegisvolt-$ENV-public" >&2; exit 1; }
URL="$ENDPOINT/restapis/$API_ID/$ENV/_user_request_/graphql"

HEADERS=(-H "content-type: application/json")
if $ADMIN; then
  TOKEN="$(kubectl get secret aegisvolt-admin -n "aegisvolt-$ENV" -o jsonpath='{.data.token}' | base64 -d)"
  HEADERS+=(-H "x-admin-token: $TOKEN")
fi
BODY="$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$QUERY")"

if $VERBOSE; then
  echo "▶ API Gateway: aegisvolt-$ENV-public ($API_ID), stage $ENV" >&2
  echo "▶ POST $URL" >&2
  curl -sS -D /dev/stderr "${HEADERS[@]}" -d "$BODY" "$URL" | python3 -m json.tool
else
  curl -sS "${HEADERS[@]}" -d "$BODY" "$URL" | python3 -m json.tool
fi
