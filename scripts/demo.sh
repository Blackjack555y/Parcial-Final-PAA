#!/usr/bin/env bash
# Kit del día de la sustentación.
#   ./scripts/demo.sh up            levanta TODO en orden (Floci, runner, minikube, 3 ambientes,
#                                   consolas, Prefect) y termina con el semáforo
#   ./scripts/demo.sh check         semáforo: ¿está listo cada punto que se va a demostrar?
#   ./scripts/demo.sh reset-flags   flags de los 3 ambientes a su valor inicial (antes de empezar)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
A() { aws --endpoint-url http://localhost:4566 "$@"; }
ENVS=(dev test prod)
OK=0
FAIL=0

check() { # check "<descripción>" <comando...>
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[32m✔\033[0m %s\n' "$label"
    OK=$((OK + 1))
  else
    printf '  \033[31m✘\033[0m %s\n' "$label"
    FAIL=$((FAIL + 1))
  fi
}

stable_tag() {
  kubectl get deploy graphql-gateway -n "aegisvolt-$1" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | sed 's/.*://'
}

api_env_is() { ./scripts/api.sh "$1" '{ environment { name } }' | grep -q "\"name\": \"$1\""; }
flag_is() { # flag_is <env> <flag> <true|false>
  ./scripts/api.sh "$1" '{ featureFlags { name enabled } }' | python3 -c '
import json, sys
flags = {f["name"]: f["enabled"] for f in json.load(sys.stdin)["data"]["featureFlags"]}
sys.exit(0 if str(flags.get(sys.argv[1])).lower() == sys.argv[2] else 1)' "$2" "$3"
}
pods_ready() {
  kubectl get deploy -n "aegisvolt-$1" -o json | python3 -c '
import json, sys
items = json.load(sys.stdin)["items"]
sys.exit(0 if items and all(d["status"].get("readyReplicas", 0) == d["spec"]["replicas"] for d in items) else 1)'
}
state_versions() {
  [[ "$(A s3api list-object-versions --bucket aegisvolt-tfstate --prefix "env:/$1/terraform.tfstate" \
    --query "length(Versions[?Key=='env:/$1/terraform.tfstate'])" --output text)" -ge 2 ]]
}
ruleset_active() {
  gh api "repos/{owner}/{repo}/rules/branches/test" --jq '.[].type' | grep -q non_fast_forward
}

cmd_check() {
  echo "Plataforma"
  check "Floci responde (localhost:4566)" curl -fs http://localhost:4566/_floci/health
  check "Floci conectado a la red de minikube (192.168.49.250)" bash -c \
    "docker inspect aegisvolt-floci --format '{{(index .NetworkSettings.Networks \"minikube\").IPAddress}}' | grep -q 192.168.49.250"
  check "minikube corriendo" minikube status
  check "Runner self-hosted activo" systemctl --user is-active aegisvolt-runner
  check "Runner en línea en GitHub" bash -c \
    "gh api 'repos/{owner}/{repo}/actions/runners' --jq '.runners[].status' | grep -q online"
  check "Ruleset activo en dev/test/prod (sin force push)" ruleset_active
  check "Prefect UI (127.0.0.1:4200)" curl -fs http://127.0.0.1:4200/api/health
  for env in "${ENVS[@]}"; do
    echo "Ambiente $env ($(stable_tag "$env"))"
    check "pods listos en aegisvolt-$env" pods_ready "$env"
    check "API Gateway → Ingress → pod responde como '$env'" api_env_is "$env"
    check "consola web :$((3000 + $(printf '%s\n' "${ENVS[@]}" | grep -nx "$env" | cut -d: -f1)))" \
      curl -fs "http://127.0.0.1:$((3000 + $(printf '%s\n' "${ENVS[@]}" | grep -nx "$env" | cut -d: -f1)))/healthz"
    check "flag de falla APAGADA" flag_is "$env" SIMULATE_CHARGER_HARDWARE_FAULT false
    check "tfstate con historial de versiones en S3" state_versions "$env"
    check "log groups /aegisvolt/$env/*" bash -c \
      "[[ \$(aws --endpoint-url http://localhost:4566 logs describe-log-groups --log-group-name-prefix /aegisvolt/$env/ --query 'length(logGroups)') -eq 4 ]]"
  done
  echo
  if [[ $FAIL -eq 0 ]]; then
    printf '\033[32mTodo listo (%d/%d)\033[0m\n' "$OK" "$OK"
  else
    printf '\033[31m%d puntos por revisar\033[0m (%d OK)\n' "$FAIL" "$OK"
    return 1
  fi
}

cmd_up() {
  echo "▶ Floci";            ./scripts/dev.sh up
  echo "▶ Runner";           systemctl --user start aegisvolt-runner
  echo "▶ minikube";         minikube status >/dev/null 2>&1 || {
    # Si Floci ocupó la IP del nodo (arrancó primero), se libera antes de iniciar minikube.
    docker network disconnect minikube aegisvolt-floci 2>/dev/null || true
    minikube start
  }
  for env in "${ENVS[@]}"; do
    tag="$(stable_tag "$env")"
    if [[ -n "$tag" ]]; then
      echo "▶ $env ($tag): reconectar con Floci"
      ./scripts/deploy.sh "$env" "$tag" | tail -1
    fi
  done
  echo "▶ Consolas";         ./scripts/ui.sh stop >/dev/null; ./scripts/ui.sh start
  echo "▶ Prefect";          ./scripts/dev.sh up-observability | tail -1
  echo
  cmd_check
}

cmd_reset_flags() {
  for env in "${ENVS[@]}"; do
    local on=true
    [[ "$env" == prod ]] && on=false
    for pair in "ENABLE_DYNAMIC_SURGE_PRICING:$on" "ENABLE_FAST_CHARGE_BOOST:$on" \
      "SIMULATE_CHARGER_HARDWARE_FAULT:false"; do
      ./scripts/api.sh "$env" \
        "mutation { setFeatureFlag(name: \"${pair%%:*}\", enabled: ${pair##*:}) { name enabled } }" \
        --admin >/dev/null && echo "  $env ${pair%%:*}=${pair##*:}"
    done
  done
}

case "${1:-check}" in
  up) cmd_up ;;
  check) cmd_check ;;
  reset-flags) cmd_reset_flags ;;
  *) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
