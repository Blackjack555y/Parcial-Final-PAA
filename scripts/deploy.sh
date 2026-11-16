#!/usr/bin/env bash
# Despliegue en Kubernetes (minikube, un namespace por ambiente) con estrategia CANARY.
#
#   ./scripts/deploy.sh image-tag                 etiqueta de imagen = hash del código (services+libs)
#   ./scripts/deploy.sh build <tag>               construye las 4 imágenes con esa etiqueta
#   ./scripts/deploy.sh <env> <tag> [opciones]    despliega <tag> en aegisvolt-<env>
#   ./scripts/deploy.sh status <env>              pistas, imágenes y pesos del ambiente
#
# Opciones: --analysis-seconds N (60) · --canary-weight P (20) · --chaos TASA (0, solo demo)
#
# Flujo (pregunta 6.2):
#   1. Primera vez en el ambiente → se despliega directo como estable (no hay con qué comparar).
#   2. Misma etiqueta que la estable → solo se reaplica configuración (no hay código nuevo).
#   3. Etiqueta nueva → CANARY:
#        a. pista canary (1 réplica por servicio) con la versión nueva, peso 0
#        b. Ingress canary al 20 % del tráfico (los 4 servicios de una petición son de la MISMA
#           versión: el gateway canary solo llama a los servicios canary)
#        c. ventana de análisis con tráfico sintético + pods canary Ready sin reinicios
#        d. scripts/canary.py evaluate: tasa de error 5xx de la versión nueva en CloudWatch
#           ≤ 5 % con ≥ 20 peticiones
#        e. OK  → la estable pasa a la versión nueva (rolling update), canary a 0 % y se elimina
#           NO  → canary a 0 % y se elimina (ROLLBACK automático); la estable nunca se tocó
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Servicios de la pista (stable/canary) y todas las imágenes (incluye la consola web, que solo
# vive en la pista estable y se actualiza al promover).
SERVICES=(station-reservation-service billing-wallet-service energy-dispatch-service graphql-gateway)
IMAGES=("${SERVICES[@]}" frontend)
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
export AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

log() { printf '\033[1;36m▶ %s\033[0m\n' "$*" >&2; }
summary() { [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && echo "$*" >>"$GITHUB_STEP_SUMMARY"; return 0; }
usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

image_tag() {
  # Mismo código ⇒ misma etiqueta: al promover dev→test→prod se despliega la imagen idéntica
  # (build once). Un cambio solo de infraestructura no dispara un canary innecesario.
  git -C "$ROOT" rev-parse HEAD:services HEAD:libs HEAD:frontend | sha256sum | cut -c1-12
}

build() {
  IMAGE_TAG="$1" docker compose -f "$ROOT/compose.yaml" -f "$ROOT/compose.apps.yaml" build \
    "${IMAGES[@]}"
}

# --- Clúster y conexión con Floci ------------------------------------------------------------
ensure_cluster() {
  minikube status >/dev/null 2>&1 || { echo "minikube no está corriendo (minikube start)" >&2; exit 1; }
  kubectl config use-context minikube >/dev/null
  # Floci se conecta TAMBIÉN a la red Docker de minikube con una IP FIJA, lejos de la .2 que
  # minikube usa siempre: si Floci arranca primero no le "roba" la IP al nodo (error
  # "Address already in use"). Al revés —minikube en dos redes— rompe la detección de IP.
  docker network connect --ip "${FLOCI_MINIKUBE_IP:-192.168.49.250}" minikube aegisvolt-floci \
    2>/dev/null || true
  FLOCI_IP="$(docker inspect aegisvolt-floci \
    --format '{{(index .NetworkSettings.Networks "minikube").IPAddress}}')"
  [[ -n "$FLOCI_IP" ]] || { echo "No se encontró la IP de Floci en la red minikube" >&2; exit 1; }
}

load_images() {
  local tag="$1" loaded
  loaded="$(minikube image ls 2>/dev/null)"
  for svc in "${IMAGES[@]}"; do
    if ! grep -q "aegisvolt/$svc:$tag" <<<"$loaded"; then
      log "Cargando imagen aegisvolt/$svc:$tag en minikube"
      minikube image load "aegisvolt/$svc:$tag"
    fi
  done
}

# Renderiza un overlay con la etiqueta de imagen indicada (sin modificar archivos versionados).
render() {
  local overlay="$1" tag="$2" dir
  dir="$(mktemp -d "$ROOT/.run/kustomize-XXXXXX")"
  {
    echo "apiVersion: kustomize.config.k8s.io/v1beta1"
    echo "kind: Kustomization"
    echo "resources: [../../k8s/overlays/$overlay]"
    echo "images:"
    for svc in "${IMAGES[@]}"; do echo "  - { name: aegisvolt/$svc, newTag: \"$tag\" }"; done
  } >"$dir/kustomization.yaml"
  kubectl kustomize "$dir"
  rm -rf "$dir"
}

wire_platform() {
  local ns="$1"
  # EndpointSlice del Service "floci" + salida permitida SOLO hacia Floci:4566.
  kubectl apply -n "$ns" -f - >/dev/null <<EOF
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: floci
  labels:
    kubernetes.io/service-name: floci
addressType: IPv4
ports:
  - { name: http, port: 4566, protocol: TCP }
endpoints:
  - addresses: ["$FLOCI_IP"]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-aws-egress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/part-of: aegisvolt
  policyTypes: [Egress]
  egress:
    - to:
        - ipBlock: { cidr: "$FLOCI_IP/32" }
      ports:
        - { protocol: TCP, port: 4566 }
EOF
  # Token de administración de flags: se crea una vez por ambiente y nunca se versiona.
  if ! kubectl get secret aegisvolt-admin -n "$ns" >/dev/null 2>&1; then
    kubectl create secret generic aegisvolt-admin -n "$ns" \
      --from-literal=token="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(24))')" >/dev/null
    log "Secret aegisvolt-admin creado en $ns"
  fi
}

current_stable_tag() {
  # Vacío si el ambiente aún no tiene pista estable (primer despliegue).
  { kubectl get deploy graphql-gateway -n "$1" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true; } | sed 's/.*://'
}

wait_track() {
  local ns="$1" suffix="$2" list=("${SERVICES[@]}")
  [[ -z "$suffix" ]] && list=("${IMAGES[@]}") # la pista estable incluye la consola
  for svc in "${list[@]}"; do
    kubectl rollout status "deploy/$svc$suffix" -n "$ns" --timeout=180s >/dev/null
  done
}

set_weight() {
  kubectl annotate ingress graphql-gateway-canary -n "$1" --overwrite \
    "nginx.ingress.kubernetes.io/canary-weight=$2" >/dev/null
  log "Ingress canary → $2 % del tráfico"
}

remove_canary() {
  local env="$1" tag="$2"
  render "$env/canary" "$tag" | kubectl delete -f - --ignore-not-found --wait=false >/dev/null
}

canary_pods_healthy() {
  local ns="$1" bad
  bad="$(kubectl get pods -n "$ns" -l track=canary -o json | "$PYTHON" -c '
import json, sys
pods = json.load(sys.stdin)["items"]
bad = [p["metadata"]["name"] for p in pods
       if any(not c.get("ready") or c.get("restartCount", 0) > 0
              for c in p.get("status", {}).get("containerStatuses", [])) or not pods]
print(" ".join(bad) if pods else "sin-pods")')"
  [[ -z "$bad" ]] || { echo "Pods canary no sanos: $bad" >&2; return 1; }
}

port_forward() {
  # Acceso local al Ingress Controller (Docker Desktop no expone la IP de minikube al host).
  PF_PORT="$("$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller "$PF_PORT:80" >/dev/null 2>&1 &
  PF_PID=$!
  trap 'kill $PF_PID 2>/dev/null || true' EXIT
  for _ in $(seq 20); do curl -fs -o /dev/null "http://127.0.0.1:$PF_PORT/healthz" 2>/dev/null && break; sleep 0.5; done
}

deploy() {
  local env="$1" tag="$2" analysis=60 weight=20 chaos=0
  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --analysis-seconds) analysis="$2"; shift 2 ;;
      --canary-weight) weight="$2"; shift 2 ;;
      --chaos) chaos="$2"; shift 2 ;;
      *) usage ;;
    esac
  done
  case "$env" in dev | test | prod) ;; *) echo "Ambiente inválido: $env" >&2; exit 1 ;; esac
  local ns="aegisvolt-$env" host="api.$env.aegisvolt.local"
  mkdir -p "$ROOT/.run"

  ensure_cluster
  load_images "$tag"
  local stable
  stable="$(current_stable_tag "$ns")"
  log "Ambiente $env · estable=${stable:-ninguna} · nueva=$tag"
  summary "### Despliegue Kubernetes \`$env\`"
  summary "- Estable actual: \`${stable:-ninguna}\` · versión nueva: \`$tag\`"

  # Namespace, plataforma y pista estable (con la versión estable ACTUAL: no cambia código).
  render "$env" "${stable:-$tag}" | kubectl apply -f - >/dev/null
  wire_platform "$ns"

  if [[ -z "$stable" || "$stable" == "$tag" ]]; then
    wait_track "$ns" ""
    local why="misma versión ya estable: solo se reaplicó la configuración"
    [[ -z "$stable" ]] && why="primer despliegue del ambiente: no hay versión estable con qué comparar"
    log "Sin canary ($why)"
    summary "- Sin canary ($why)"
    return 0
  fi

  # --- CANARY --------------------------------------------------------------------------------
  local since
  since="$(date +%s)"
  log "Desplegando pista canary con $tag"
  render "$env/canary" "$tag" | kubectl apply -f - >/dev/null
  if [[ "$chaos" != "0" ]]; then
    log "DEMO: inyectando ${chaos} de errores en el gateway canary"
    kubectl set env deploy/graphql-gateway-canary -n "$ns" CHAOS_ERROR_RATE="$chaos" >/dev/null
  fi
  if ! wait_track "$ns" "-canary"; then
    remove_canary "$env" "$tag"
    log "ROLLBACK: la pista canary no llegó a Ready"
    summary "- ❌ ROLLBACK: la pista canary no llegó a Ready"
    exit 1
  fi
  set_weight "$ns" "$weight"

  port_forward
  log "Ventana de análisis: ${analysis}s de tráfico sintético"
  "$PYTHON" "$ROOT/scripts/canary.py" traffic --url "http://127.0.0.1:$PF_PORT" --host "$host" \
    --seconds "$analysis" --rps 5
  sleep 8 # los servicios envían logs a CloudWatch cada 2 s; margen para los metric filters

  local verdict=0
  canary_pods_healthy "$ns" || verdict=1
  "$PYTHON" "$ROOT/scripts/canary.py" evaluate --env "$env" --version "$tag" --since "$since" \
    || verdict=1

  if [[ $verdict -ne 0 ]]; then
    set_weight "$ns" 0
    remove_canary "$env" "$tag"
    log "ROLLBACK automático: la versión estable $stable sigue sirviendo el 100 %"
    summary "- ❌ **ROLLBACK automático**: \`$stable\` sigue con el 100 % del tráfico"
    exit 1
  fi

  log "PROMOCIÓN: la pista estable pasa a $tag"
  render "$env" "$tag" | kubectl apply -f - >/dev/null
  wait_track "$ns" ""
  set_weight "$ns" 0
  remove_canary "$env" "$tag"
  summary "- ✅ **Promovido al 100 %**: \`$tag\` es la nueva versión estable"
  log "Listo: $env sirve $tag al 100 %"
}

status() {
  local ns="aegisvolt-$1"
  kubectl get deploy -n "$ns" \
    -o custom-columns='DEPLOY:.metadata.name,TRACK:.metadata.labels.track,READY:.status.readyReplicas,WANT:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image'
  kubectl get ingress -n "$ns" \
    -o custom-columns='INGRESS:.metadata.name,HOST:.spec.rules[0].host,CANARY-WEIGHT:.metadata.annotations.nginx\.ingress\.kubernetes\.io/canary-weight'
}

case "${1:-}" in
  image-tag) image_tag ;;
  build) build "${2:?Uso: deploy.sh build <tag>}" ;;
  status) status "${2:?Uso: deploy.sh status <env>}" ;;
  dev | test | prod) deploy "$@" ;;
  *) usage ;;
esac
