#!/usr/bin/env bash
# Pruebas de humo de Floci (Fase 0, F0-1 a F0-8).
# Cada prueba decide qué alternativa del plan se usa (ver FASES-DESARROLLO.md, Fase 0).
# Uso: ./scripts/floci_smoke.sh            (requiere Floci en localhost:4566)
# Crea recursos con el prefijo "smoke-" y los borra al terminar.
set -uo pipefail

ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_PAGER=""

RUN_ID="$(date +%s)"
BUCKET="smoke-${RUN_ID}"
LOG_GROUP="/aegisvolt/smoke/${RUN_ID}"
TMP="$(mktemp -d)"
declare -a RESULTS=()

awsl() { aws --endpoint-url "$ENDPOINT" "$@"; }

record() { # record <id> <PASS|FAIL> <mensaje>
  RESULTS+=("$(printf '%-6s %-5s %s' "$1" "$2" "$3")")
  printf '[%s] %s: %s\n' "$2" "$1" "$3"
}

cleanup() {
  awsl s3 rb "s3://${BUCKET}" --force >/dev/null 2>&1
  awsl logs delete-log-group --log-group-name "$LOG_GROUP" >/dev/null 2>&1
  [[ -n "${VPCE_GW:-}" ]] && awsl ec2 delete-vpc-endpoints --vpc-endpoint-ids "$VPCE_GW" >/dev/null 2>&1
  [[ -n "${VPCE_IF:-}" ]] && awsl ec2 delete-vpc-endpoints --vpc-endpoint-ids "$VPCE_IF" >/dev/null 2>&1
  [[ -n "${NLB_ARN:-}" ]] && awsl elbv2 delete-load-balancer --load-balancer-arn "$NLB_ARN" >/dev/null 2>&1
  [[ -n "${SUBNET_ID:-}" ]] && awsl ec2 delete-subnet --subnet-id "$SUBNET_ID" >/dev/null 2>&1
  [[ -n "${VPC_ID:-}" ]] && awsl ec2 delete-vpc --vpc-id "$VPC_ID" >/dev/null 2>&1
  awsl eks delete-cluster --name "smoke-${RUN_ID}" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

curl -fs "${ENDPOINT}/_floci/health" >/dev/null || { echo "Floci no responde en ${ENDPOINT}"; exit 2; }
echo "Floci OK en ${ENDPOINT} — ejecutando pruebas (run ${RUN_ID})"
echo

# --- F0-1: escritura condicional en S3 (necesaria para use_lockfile) -------------
awsl s3api create-bucket --bucket "$BUCKET" >/dev/null
echo "lock" >"$TMP/lock"
if awsl s3api put-object --bucket "$BUCKET" --key test.tflock --body "$TMP/lock" --if-none-match '*' >/dev/null 2>&1; then
  if awsl s3api put-object --bucket "$BUCKET" --key test.tflock --body "$TMP/lock" --if-none-match '*' >/dev/null 2>&1; then
    record F0-1 FAIL "La 2.ª escritura con If-None-Match no fue rechazada → T5: usar lock con DynamoDB"
  else
    record F0-1 PASS "S3 rechaza la 2.ª escritura condicional → T5: use_lockfile = true"
  fi
else
  record F0-1 FAIL "put-object con --if-none-match no soportado → T5: usar lock con DynamoDB"
fi

# --- F0-2: versionamiento del bucket ---------------------------------------------
awsl s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
for i in 1 2 3; do
  echo "{\"serial\": $i}" >"$TMP/state"
  awsl s3api put-object --bucket "$BUCKET" --key "env:/dev/terraform.tfstate" --body "$TMP/state" >/dev/null
done
VERSIONS=$(awsl s3api list-object-versions --bucket "$BUCKET" --prefix "env:/dev/terraform.tfstate" \
  --query 'length(Versions)' --output text 2>/dev/null || echo 0)
OLDEST=$(awsl s3api list-object-versions --bucket "$BUCKET" --prefix "env:/dev/terraform.tfstate" \
  --query 'Versions[-1].VersionId' --output text 2>/dev/null || echo "")
if [[ "$VERSIONS" == "3" ]] && awsl s3api get-object --bucket "$BUCKET" --key "env:/dev/terraform.tfstate" \
  --version-id "$OLDEST" "$TMP/old" >/dev/null 2>&1 && grep -q '"serial": 1' "$TMP/old"; then
  record F0-2 PASS "3 versiones listadas y la más antigua se recupera por VersionId"
else
  record F0-2 FAIL "Versionamiento incompleto (versiones=${VERSIONS}) → BLOQUEANTE, investigar"
fi

# --- F0-3: VPC, subred y VPC Endpoints -------------------------------------------
VPC_ID=$(awsl ec2 create-vpc --cidr-block 10.99.0.0/16 --query 'Vpc.VpcId' --output text 2>/dev/null || true)
if [[ -n "$VPC_ID" && "$VPC_ID" != "None" ]]; then
  SUBNET_ID=$(awsl ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.99.1.0/24 \
    --availability-zone us-east-1a --query 'Subnet.SubnetId' --output text 2>/dev/null || true)
  RT_ID=$(awsl ec2 describe-route-tables --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || true)
  VPCE_GW=$(awsl ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Gateway \
    --service-name com.amazonaws.us-east-1.dynamodb --route-table-ids "$RT_ID" \
    --query 'VpcEndpoint.VpcEndpointId' --output text 2>/dev/null || true)
  VPCE_IF=$(awsl ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Interface \
    --service-name com.amazonaws.us-east-1.execute-api --subnet-ids "$SUBNET_ID" \
    --query 'VpcEndpoint.VpcEndpointId' --output text 2>/dev/null || true)
  if [[ "$VPCE_GW" == vpce-* && "$VPCE_IF" == vpce-* ]]; then
    record F0-3 PASS "VPC ${VPC_ID}, VPCE Gateway (DynamoDB) e Interface (execute-api) creados"
  else
    record F0-3 FAIL "VPC creada pero VPCE falló (gw='${VPCE_GW}', if='${VPCE_IF}') → módulo con flag"
  fi
else
  record F0-3 FAIL "No se pudo crear la VPC → módulo network con flag"
fi

# --- F0-4: NLB interno + VPC Link de API Gateway ---------------------------------
if [[ -n "${SUBNET_ID:-}" ]]; then
  NLB_ARN=$(awsl elbv2 create-load-balancer --name "smoke-nlb-${RUN_ID: -6}" --type network \
    --scheme internal --subnets "$SUBNET_ID" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)
  if [[ "$NLB_ARN" == arn:* ]]; then
    LINK_ID=$(awsl apigateway create-vpc-link --name "smoke-${RUN_ID}" --target-arns "$NLB_ARN" \
      --query 'id' --output text 2>/dev/null || true)
    if [[ -n "$LINK_ID" && "$LINK_ID" != "None" ]]; then
      record F0-4 PASS "NLB interno + VPC Link ${LINK_ID} creados"
      awsl apigateway delete-vpc-link --vpc-link-id "$LINK_ID" >/dev/null 2>&1
    else
      record F0-4 FAIL "NLB creado pero VPC Link falló → API Gateway con flag"
    fi
  else
    record F0-4 FAIL "No se pudo crear el NLB → API Gateway con flag"
  fi
else
  record F0-4 FAIL "Sin subred (depende de F0-3)"
fi

# --- F0-5: EKS (plano de control) ------------------------------------------------
if awsl eks create-cluster --name "smoke-${RUN_ID}" \
  --role-arn arn:aws:iam::000000000000:role/smoke \
  --resources-vpc-config "subnetIds=${SUBNET_ID:-subnet-00000000}" >/dev/null 2>&1 &&
  awsl eks describe-cluster --name "smoke-${RUN_ID}" --query 'cluster.name' --output text >/dev/null 2>&1; then
  record F0-5 PASS "create-cluster y describe-cluster responden (plano de control)"
else
  record F0-5 FAIL "EKS no disponible → módulo eks con flag; ejecución en minikube"
fi

# --- F0-6 y F0-7: CloudWatch Logs, metric filter y filtro por correlation_id -----
awsl logs create-log-group --log-group-name "$LOG_GROUP" >/dev/null
awsl logs create-log-stream --log-group-name "$LOG_GROUP" --log-stream-name smoke >/dev/null
awsl logs put-metric-filter --log-group-name "$LOG_GROUP" --filter-name errors \
  --filter-pattern '{ $.level = "ERROR" }' \
  --metric-transformations "metricName=SmokeErrors${RUN_ID},metricNamespace=AegisVolt/Smoke,metricValue=1,unit=Count" \
  >/dev/null 2>&1
NOW_MS=$(($(date +%s) * 1000))
python3 - "$NOW_MS" "$RUN_ID" >"$TMP/events.json" <<'PY'
import json, sys
now, run = int(sys.argv[1]), sys.argv[2]
print(json.dumps([
    {"timestamp": now, "message": json.dumps({"level": "ERROR", "correlation_id": f"corr-{run}", "version": "v2"})},
    {"timestamp": now + 1, "message": json.dumps({"level": "INFO", "correlation_id": "otro", "version": "v1"})},
]))
PY
awsl logs put-log-events --log-group-name "$LOG_GROUP" --log-stream-name smoke \
  --log-events "file://$TMP/events.json" >/dev/null
sleep 3

DATAPOINTS=$(awsl cloudwatch get-metric-statistics --namespace AegisVolt/Smoke \
  --metric-name "SmokeErrors${RUN_ID}" --statistics Sum --period 60 \
  --start-time "$(date -u -d '-5 min' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u -d '+5 min' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'length(Datapoints)' --output text 2>/dev/null || echo 0)
if [[ "$DATAPOINTS" =~ ^[1-9] ]]; then
  record F0-6 PASS "El metric filter genera métricas → T4: canary usa el metric filter"
else
  record F0-6 FAIL "El metric filter no genera métricas → T4: PutMetricData directo desde los servicios"
fi

MATCHES=$(awsl logs filter-log-events --log-group-name "$LOG_GROUP" \
  --filter-pattern "{ \$.correlation_id = \"corr-${RUN_ID}\" }" \
  --query 'length(events)' --output text 2>/dev/null || echo 0)
if [[ "$MATCHES" == "1" ]]; then
  record F0-7 PASS "filter-log-events con patrón JSON devuelve exactamente el evento buscado"
else
  record F0-7 FAIL "Patrón JSON devolvió ${MATCHES} eventos → usar búsqueda por texto \"corr-…\""
fi

# --- F0-8: conectividad minikube → Floci -----------------------------------------
if command -v minikube >/dev/null 2>&1 && minikube status >/dev/null 2>&1; then
  if minikube ssh -- "curl -fs http://host.minikube.internal:4566/_floci/health" >/dev/null 2>&1; then
    record F0-8 PASS "minikube llega a Floci por host.minikube.internal:4566"
  else
    record F0-8 FAIL "minikube no llega por host.minikube.internal → conectar minikube a la red 'aegisvolt'"
  fi
else
  record F0-8 SKIP "minikube no está instalado o no está corriendo (se repite en la Fase 4)"
fi

echo
echo "================ Resumen ================"
printf '%s\n' "${RESULTS[@]}"
