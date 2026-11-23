#!/usr/bin/env bash
# Revisión EN VIVO de la rúbrica (tabla 5.2): verifica cada criterio contra el sistema corriendo.
#   ./scripts/rubrica.sh            (requiere ./scripts/demo.sh up)
#
# Efectos (todos se revierten): activa y apaga la flag de falla en test, ejecuta una saga de
# prueba en test (queda compensada, saldo intacto), cambia y restaura una flag en dev, e intenta
# push directo y force push a test con un commit temporal (rechazados; el directorio de trabajo
# no se toca).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
A() { aws --endpoint-url http://localhost:4566 "$@"; }
ENVS=(dev test prod)
declare -A PASS FAIL
LAYER=""
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

layer() { LAYER="$1"; PASS[$1]=0; FAIL[$1]=0; printf '\n\033[1m%s\033[0m\n' "$1"; }
check() { # check "<criterio>" <comando...>
  local label="$1"
  shift
  if "$@" >"$TMP/out" 2>&1; then
    printf '  \033[32m✔\033[0m %s\n' "$label"
    PASS[$LAYER]=$((PASS[$LAYER] + 1))
  else
    printf '  \033[31m✘\033[0m %s\n' "$label"
    sed 's/^/      /' "$TMP/out" | tail -3
    FAIL[$LAYER]=$((FAIL[$LAYER] + 1))
  fi
}
gql() { ./scripts/api.sh "$@"; } # GraphQL entrando por API Gateway
json() { "$PY" -c "import json,sys; d=json.load(sys.stdin); $1"; }

# ---------------------------------------------------------------------------------------------
layer "1. Infraestructura como Código (30 %)"
check "Workspaces dev, test y prod" bash -c \
  "cd infra/envs && terraform workspace list | tr -d ' *' | grep -qx dev && terraform workspace list | tr -d ' *' | grep -qx test && terraform workspace list | tr -d ' *' | grep -qx prod"
check "Bucket del tfstate con versionamiento habilitado" bash -c \
  "[[ \$(aws --endpoint-url http://localhost:4566 s3api get-bucket-versioning --bucket aegisvolt-tfstate --query Status --output text) == Enabled ]]"
for env in "${ENVS[@]}"; do
  check "Historial de versiones de env:/$env/terraform.tfstate" bash -c \
    "[[ \$(aws --endpoint-url http://localhost:4566 s3api list-object-versions --bucket aegisvolt-tfstate --prefix env:/$env/terraform.tfstate --query \"length(Versions[?Key=='env:/$env/terraform.tfstate'])\" --output text) -ge 2 ]]"
  # La salida completa va a un archivo ANTES de buscar el texto: un "| grep -q" cortaría a
  # Terraform (SIGPIPE) antes de liberar el lock del estado en S3.
  check "Estado de $env = realidad (terraform plan sin cambios)" bash -c \
    "./scripts/tf.sh $env plan -no-color >$TMP/plan-$env.txt 2>&1; grep -q 'No changes' $TMP/plan-$env.txt"
  check "5 tablas DynamoDB aegisvolt-*-$env (Database-per-Service)" bash -c \
    "[[ \$(aws --endpoint-url http://localhost:4566 dynamodb list-tables --query 'TableNames' --output text | tr '\t' '\n' | grep -c -- '-$env\$') -eq 5 ]]"
  check "VPC + VPC Endpoints (S3, DynamoDB, logs, monitoring, execute-api) en $env" bash -c \
    "./scripts/tf.sh $env output -json vpc_endpoints 2>/dev/null | $PY -c 'import json,sys; e=json.load(sys.stdin); sys.exit(0 if {\"s3\",\"dynamodb\",\"logs\",\"monitoring\",\"execute-api\"} <= set(e) else 1)'"
  check "API Gateway → VPC Link → Ingress → pod responde como '$env'" bash -c \
    "./scripts/api.sh $env '{ environment { name } }' | grep -q '\"name\": \"$env\"'"
done

# ---------------------------------------------------------------------------------------------
layer "2. CI/CD, gobernanza de Git, ambientes y canary (25 %)"
RULESET_ID="$(gh api 'repos/{owner}/{repo}/rulesets' --jq '.[] | select(.name=="aegisvolt-protected-environments") | .id')"
gh api "repos/{owner}/{repo}/rulesets/$RULESET_ID" >"$TMP/ruleset.json"
check "Ruleset activo en dev, test y prod, sin bypass" bash -c \
  "$PY -c 'import json; r=json.load(open(\"$TMP/ruleset.json\")); assert r[\"enforcement\"]==\"active\" and not r[\"bypass_actors\"]; assert {\"refs/heads/dev\",\"refs/heads/test\",\"refs/heads/prod\"} <= set(r[\"conditions\"][\"ref_name\"][\"include\"])'"
check "Reglas: sin force push, sin borrado, PR obligatorio + 6 checks requeridos" bash -c \
  "$PY -c 'import json; r=json.load(open(\"$TMP/ruleset.json\")); t={x[\"type\"]:x for x in r[\"rules\"]}; assert {\"non_fast_forward\",\"deletion\",\"pull_request\",\"required_status_checks\"} <= set(t); c={x[\"context\"] for x in t[\"required_status_checks\"][\"parameters\"][\"required_status_checks\"]}; assert c=={\"lint-build\",\"unit-tests\",\"integration-tests\",\"e2e-tests\",\"terraform-plan\",\"promote-guard\"}, c'"
git fetch -q origin test
PROBE="$(git commit-tree "origin/test^{tree}" -p origin/test -m 'rubrica: intento de push directo')"
check "Push directo a test RECHAZADO (en vivo)" bash -c \
  "git push origin $PROBE:refs/heads/test 2>&1 | grep -q 'rule violations'"
check "Force push a test RECHAZADO (en vivo)" bash -c \
  "git push --force origin $PROBE:refs/heads/test 2>&1 | grep -q 'rule violations'"
gh pr list --state merged --limit 200 --json headRefName,baseRefName >"$TMP/prs.json"
check "Promoción lineal: feature/* → dev, dev → test, test → prod (y ningún salto fusionado)" bash -c \
  "$PY -c 'import json; p=json.load(open(\"$TMP/prs.json\")); ok=lambda h,b: (b==\"dev\" and h.startswith(\"feature/\")) or (h,b) in {(\"dev\",\"test\"),(\"test\",\"prod\")}; bad=[x for x in p if not ok(x[\"headRefName\"],x[\"baseRefName\"])]; kinds={(\"feature\" if x[\"headRefName\"].startswith(\"feature/\") else x[\"headRefName\"], x[\"baseRefName\"]) for x in p}; assert not bad, bad; assert kinds=={(\"feature\",\"dev\"),(\"dev\",\"test\"),(\"test\",\"prod\")}'"
check "Workflow de PR con unit, integration, e2e y terraform-plan" bash -c \
  "for j in unit-tests integration-tests e2e-tests terraform-plan; do grep -q \"name: \$j\" .github/workflows/pr-checks.yml || exit 1; done"
check "Apply dinámico: TARGET_ENV = rama del merge → workspace" bash -c \
  "grep -q 'TARGET_ENV: \${{ github.ref_name }}' .github/workflows/deploy.yml && grep -q 'tf.sh \"\$TARGET_ENV\"' .github/workflows/deploy.yml"
for env in "${ENVS[@]}"; do
  check "Último despliegue de $env exitoso (terraform-apply + deploy-k8s)" bash -c \
    "[[ \$(gh run list --workflow deploy.yml --branch $env --limit 1 --json conclusion --jq '.[0].conclusion') == success ]]"
done
check "Canary del pipeline: 20 % → análisis en CloudWatch → PROMOTE" bash -c \
  "for id in \$(gh run list --workflow deploy.yml --branch dev --limit 10 --json databaseId --jq '.[].databaseId'); do gh run view \$id --log 2>/dev/null | grep -q 'Decisión: PROMOTE' && exit 0; done; exit 1"
check "Canary: sin pistas canary colgadas en ningún namespace" bash -c \
  "[[ -z \$(kubectl get deploy -A -l track=canary --no-headers 2>/dev/null) ]]"

# ---------------------------------------------------------------------------------------------
layer "3. Backend, microservicios, Kubernetes, GraphQL, SAGA y flags (25 %)"
check "Esquema GraphQL (queries, mutations, subscriptions) al día con el código" bash -c \
  "$PY scripts/export_schema.py --check && grep -q '^type Subscription' services/graphql-gateway/schema.graphql"
for env in "${ENVS[@]}"; do
  check "4 microservicios desacoplados listos en aegisvolt-$env (Docker + K8s)" bash -c \
    "kubectl get deploy -n aegisvolt-$env -l track=stable -o json | $PY -c 'import json,sys; d=json.load(sys.stdin)[\"items\"]; n={x[\"metadata\"][\"name\"] for x in d if x[\"status\"].get(\"readyReplicas\",0)==x[\"spec\"][\"replicas\"]}; assert {\"graphql-gateway\",\"station-reservation-service\",\"billing-wallet-service\",\"energy-dispatch-service\"} <= n, n'"
done

balance() { gql test '{ wallet(fleetId: "fleet-rapidoexpress") { balanceCents } }' | json 'print(d["data"]["wallet"]["balanceCents"])'; }
B0="$(balance)"
gql test 'mutation { setFeatureFlag(name: "SIMULATE_CHARGER_HARDWARE_FAULT", enabled: true) { enabled } }' --admin >/dev/null
sleep 6 # caché de flags de los servicios (5 s)
CONNECTOR="$(gql test '{ stations { stationId connectors { connectorId type status } } }' | json '
for s in d["data"]["stations"]:
    for c in s["connectors"]:
        if c["type"] == "CCS2" and c["status"] == "AVAILABLE":
            print(s["stationId"], c["connectorId"]); raise SystemExit')"
read -r STATION CONN <<<"$CONNECTOR"
gql test "mutation { reserveAndStartCharging(input: {stationId: \"$STATION\", connectorId: \"$CONN\", fleetId: \"fleet-rapidoexpress\", kwh: 20}) { saga { sagaId status correlationId } } }" >"$TMP/saga.json"
gql test 'mutation { setFeatureFlag(name: "SIMULATE_CHARGER_HARDWARE_FAULT", enabled: false) { enabled } }' --admin >/dev/null
SID="$(json 'print(d["data"]["reserveAndStartCharging"]["saga"]["sagaId"])' <"$TMP/saga.json")"
CID="$(json 'print(d["data"]["reserveAndStartCharging"]["saga"]["correlationId"])' <"$TMP/saga.json")"
check "SAGA con fallo inducido en test → COMPENSATED" bash -c "grep -q '\"status\": \"COMPENSATED\"' $TMP/saga.json"
check "Compensación en billing (tabla): retención RELEASED" bash -c \
  "[[ \$(aws --endpoint-url http://localhost:4566 dynamodb get-item --table-name aegisvolt-billing-test --key '{\"pk\":{\"S\":\"WALLET#fleet-rapidoexpress\"},\"sk\":{\"S\":\"TXN#$SID\"}}' --query 'Item.status.S' --output text) == RELEASED ]]"
check "Compensación en reservations (tabla): reserva RELEASED y conector AVAILABLE" bash -c \
  "[[ \$(aws --endpoint-url http://localhost:4566 dynamodb get-item --table-name aegisvolt-reservations-test --key '{\"pk\":{\"S\":\"RESERVATION#$SID\"},\"sk\":{\"S\":\"META\"}}' --query 'Item.status.S' --output text) == RELEASED && \$(aws --endpoint-url http://localhost:4566 dynamodb get-item --table-name aegisvolt-reservations-test --key '{\"pk\":{\"S\":\"STATION#$STATION\"},\"sk\":{\"S\":\"CONNECTOR#$CONN\"}}' --query 'Item.status.S' --output text) == AVAILABLE ]]"
B1="$(balance)"
check "Saldo de la flota intacto tras el rollback ($B0 → $B1)" test "$B1" = "$B0"
gql test "mutation { retryCompensation(sagaId: \"$SID\") { saga { status events { detail } } } }" --admin >"$TMP/retry.json"
B2="$(balance)"
check "Idempotencia: re-ejecutar las compensaciones no devuelve dinero dos veces ($B2)" bash -c \
  "grep -q 'idempotente' $TMP/retry.json && [[ $B2 == $B0 ]]"
check "Flags aisladas: prod conserva sus valores (falla y tarifa apagadas)" bash -c \
  "./scripts/api.sh prod '{ featureFlags { name enabled } }' | $PY -c 'import json,sys; f={x[\"name\"]:x[\"enabled\"] for x in json.load(sys.stdin)[\"data\"][\"featureFlags\"]}; assert not f[\"SIMULATE_CHARGER_HARDWARE_FAULT\"] and not f[\"ENABLE_DYNAMIC_SURGE_PRICING\"], f'"

# ---------------------------------------------------------------------------------------------
layer "4. Observabilidad: CloudWatch + Prefect (12 %)"
sleep 4 # los servicios envían logs en lotes cada 2 s
for g in graphql-gateway billing-wallet-service energy-dispatch-service station-reservation-service; do
  check "correlation_id de la saga fallida en /aegisvolt/test/$g" bash -c \
    "[[ \$(aws --endpoint-url http://localhost:4566 logs filter-log-events --log-group-name /aegisvolt/test/$g --filter-pattern '{ \$.correlation_id = \"$CID\" }' --query 'length(events)') -ge 1 ]]"
done
check "Log groups /aegisvolt/<env>/<servicio> en los 3 ambientes" bash -c \
  "for e in dev test prod; do [[ \$(aws --endpoint-url http://localhost:4566 logs describe-log-groups --log-group-name-prefix /aegisvolt/\$e/ --query 'length(logGroups)') -eq 4 ]] || exit 1; done"
check "Prefect: flujo de salud de los 3 ambientes ejecutado (Completed)" bash -c \
  "docker exec aegisvolt-prefect-flows prefect deployment run 'aegisvolt-salud-ambientes/cada-5-min' --watch 2>&1 | grep -q \"finished successfully\""
check "Prefect: auditoría de consistencia de la SAGA ejecutada y consistente en los 3" bash -c \
  "docker exec aegisvolt-prefect-flows prefect deployment run 'aegisvolt-auditoria-saga/cada-10-min' --watch 2>&1 | grep -q \"finished successfully\" && curl -s http://127.0.0.1:4200/api/artifacts/filter -H 'content-type: application/json' -d '{\"artifacts\":{\"key\":{\"any_\":[\"auditoria-saga-resumen\"]}},\"limit\":1,\"sort\":\"CREATED_DESC\"}' | $PY -c 'import json,sys; rows=[l for l in json.load(sys.stdin)[0][\"data\"].splitlines() if l.startswith((\"| dev\",\"| test\",\"| prod\"))]; assert len(rows)==3 and all(\"✅\" in r for r in rows), rows'"

# ---------------------------------------------------------------------------------------------
layer "5. Capa de presentación: frontend web (8 %)"
for pair in dev:3001 test:3002 prod:3003; do
  env="${pair%%:*}"
  port="${pair##*:}"
  check "Consola :$port conectada al GraphQL de $env" bash -c \
    "curl -s http://127.0.0.1:$port/graphql -H 'content-type: application/json' -d '{\"query\":\"{ environment { name } }\"}' | grep -q '\"name\":\"$env\"'"
done
check "Tiempo real: cambio de flag en dev llega por WSS a la consola de dev; prod intacto" \
  env TOKEN="$(./scripts/ui.sh token dev)" "$PY" - <<'EOF'
import asyncio, json, os, urllib.request, websockets

async def sub(port):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/graphql", subprotocols=["graphql-transport-ws"],
                                  origin=f"http://127.0.0.1:{port}")
    await ws.send(json.dumps({"type": "connection_init"})); await ws.recv()
    await ws.send(json.dumps({"id": "1", "type": "subscribe",
                              "payload": {"query": "subscription { featureFlags { name enabled } }"}}))
    await ws.recv()
    return ws

def set_flag(value):
    body = json.dumps({"query": f'mutation {{ setFeatureFlag(name: "ENABLE_FAST_CHARGE_BOOST", enabled: {value}) {{ enabled }} }}'})
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:3001/graphql", body.encode(),
        {"content-type": "application/json", "x-admin-token": os.environ["TOKEN"]})).read()

async def main():
    dev, prod = await sub(3001), await sub(3003)
    try:
        await asyncio.to_thread(set_flag, "false")
        msg = json.loads(await asyncio.wait_for(dev.recv(), 10))
        flags = {f["name"]: f["enabled"] for f in msg["payload"]["data"]["featureFlags"]}
        assert flags["ENABLE_FAST_CHARGE_BOOST"] is False
        try:
            await asyncio.wait_for(prod.recv(), 4)
            raise SystemExit("prod recibió un cambio")
        except asyncio.TimeoutError:
            pass
    finally:
        await asyncio.to_thread(set_flag, "true")
        await dev.close(); await prod.close()

asyncio.run(main())
EOF

# ---------------------------------------------------------------------------------------------
printf '\n\033[1mResumen\033[0m\n'
total_ok=0
total_fail=0
for l in "1. Infraestructura como Código (30 %)" "2. CI/CD, gobernanza de Git, ambientes y canary (25 %)" \
  "3. Backend, microservicios, Kubernetes, GraphQL, SAGA y flags (25 %)" \
  "4. Observabilidad: CloudWatch + Prefect (12 %)" "5. Capa de presentación: frontend web (8 %)"; do
  printf '  %-70s %2d/%-2d\n' "$l" "${PASS[$l]}" "$((PASS[$l] + FAIL[$l]))"
  total_ok=$((total_ok + PASS[$l]))
  total_fail=$((total_fail + FAIL[$l]))
done
if [[ $total_fail -eq 0 ]]; then
  printf '\033[32mRúbrica completa: %d/%d criterios verificados en vivo\033[0m\n' "$total_ok" "$total_ok"
else
  printf '\033[31m%d criterios por revisar\033[0m (%d OK)\n' "$total_fail" "$total_ok"
  exit 1
fi
