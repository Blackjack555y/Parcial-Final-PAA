#!/usr/bin/env bash
# Envoltorio de Terraform: SIEMPRE selecciona el workspace del ambiente antes de operar.
# Lo usan las personas y el pipeline, así que local y CI ejecutan exactamente lo mismo.
#
#   ./scripts/tf.sh bootstrap plan|apply      bucket S3 versionado del estado (su propio estado
#                                             también vive en el bucket: bootstrap/terraform.tfstate)
#   ./scripts/tf.sh <env> init                init + crea los workspaces dev/test/prod si faltan
#   ./scripts/tf.sh <env> plan [-out=FILE]    plan del ambiente <env>
#   ./scripts/tf.sh <env> apply [FILE]        apply del ambiente <env> (o de un plan guardado)
#   ./scripts/tf.sh <env> output              outputs del ambiente
#   ./scripts/tf.sh <env> versions            versiones del tfstate de <env> en S3
#   ./scripts/tf.sh <env> restore <VersionId> restaura una versión anterior del tfstate
#
# <env> = dev | test | prod.  TF_TARGET=floci (por defecto) | aws
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${TF_TARGET:-floci}"
BUCKET="aegisvolt-tfstate"
ENVS_DIR="$ROOT/infra/envs"
BOOTSTRAP_DIR="$ROOT/infra/bootstrap"

if [[ "$TARGET" == "floci" ]]; then
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  AWS_S3=(aws --endpoint-url "${AWS_ENDPOINT_URL:-http://localhost:4566}" s3api)
else
  AWS_S3=(aws s3api)
fi
export TF_IN_AUTOMATION="${CI:+1}"

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

[[ $# -ge 2 ]] || usage
ENV="$1"
CMD="$2"
shift 2

# --- Bootstrap ------------------------------------------------------------------
if [[ "$ENV" == "bootstrap" ]]; then
  cd "$BOOTSTRAP_DIR"
  [[ "$CMD" == "apply" || "$CMD" == "plan" ]] || usage
  BOOT_BACKEND=(-backend-config="$ENVS_DIR/backends/$TARGET.s3.tfbackend"
    -backend-config="key=bootstrap/terraform.tfstate")

  if "${AWS_S3[@]}" head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    # Caso normal: el bucket existe y el estado del bootstrap vive dentro de él.
    terraform init -input=false -reconfigure "${BOOT_BACKEND[@]}" >/dev/null
    terraform "$CMD" -input=false -var "target=$TARGET" "$@"
  else
    # Primera vez (huevo y gallina): aplicar con estado local temporal y luego migrarlo
    # al bucket recién creado. El override se borra siempre al salir.
    [[ "$CMD" == "apply" ]] || { echo "El bucket no existe aún: ejecuta 'bootstrap apply'." >&2; exit 1; }
    trap 'rm -f "$BOOTSTRAP_DIR/backend_override.tf"' EXIT
    printf 'terraform {\n  backend "local" {}\n}\n' >backend_override.tf
    terraform init -input=false -reconfigure >/dev/null
    terraform apply -input=false -var "target=$TARGET" "$@"
    rm -f backend_override.tf
    terraform init -input=false -migrate-state -force-copy "${BOOT_BACKEND[@]}" >/dev/null
    rm -f terraform.tfstate terraform.tfstate.backup
    echo "Estado del bootstrap migrado a s3://$BUCKET/bootstrap/terraform.tfstate"
  fi
  exit 0
fi

# --- Ambientes ------------------------------------------------------------------
case "$ENV" in
  dev | test | prod) ;;
  *) echo "Ambiente inválido: '$ENV' (usa dev, test o prod)" >&2; exit 1 ;;
esac

STATE_KEY="env:/$ENV/terraform.tfstate"
cd "$ENVS_DIR"

tf_init() {
  terraform init -input=false -reconfigure -backend-config="backends/$TARGET.s3.tfbackend" >/dev/null
}

select_workspace() {
  # Selecciona el workspace del ambiente; lo crea si aún no existe.
  terraform workspace select -or-create=true "$ENV" >/dev/null
  local current
  current="$(terraform workspace show)"
  if [[ "$current" != "$ENV" ]]; then
    echo "ERROR: workspace activo '$current' ≠ '$ENV'" >&2
    exit 1
  fi
  echo "▶ Terraform: workspace=$current target=$TARGET estado=s3://$BUCKET/$STATE_KEY" >&2
}

case "$CMD" in
  init)
    tf_init
    for ws in dev test prod; do terraform workspace select -or-create=true "$ws" >/dev/null; done
    select_workspace
    terraform workspace list
    ;;
  plan)
    tf_init
    select_workspace
    terraform plan -input=false -var-file="$TARGET.tfvars" "$@"
    ;;
  apply)
    tf_init
    select_workspace
    if [[ $# -gt 0 && -f "${1:-}" ]]; then
      terraform apply -input=false "$@"          # plan guardado: ya trae las variables
    else
      terraform apply -input=false -var-file="$TARGET.tfvars" "$@"
    fi
    ;;
  output)
    tf_init
    select_workspace
    terraform output "$@"
    ;;
  versions)
    "${AWS_S3[@]}" list-object-versions --bucket "$BUCKET" --prefix "$STATE_KEY" \
      --query "reverse(sort_by(Versions[?Key=='$STATE_KEY'], &LastModified))[].{Fecha:LastModified,Actual:IsLatest,Bytes:Size,VersionId:VersionId}" \
      --output table
    ;;
  restore)
    VERSION_ID="${1:?Uso: tf.sh <env> restore <VersionId>}"
    TMP="$(mktemp)"
    trap 'rm -f "$TMP"' EXIT
    "${AWS_S3[@]}" get-object --bucket "$BUCKET" --key "$STATE_KEY" --version-id "$VERSION_ID" "$TMP" >/dev/null
    echo "Versión $VERSION_ID descargada (serial $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["serial"])' "$TMP"))."
    read -r -p "¿Restaurarla como estado ACTUAL de '$ENV'? Se crea una versión nueva; nada se borra. [s/N] " ok
    [[ "$ok" == "s" ]] || { echo "Cancelado."; exit 1; }
    # Copiar la versión antigua encima crea una versión nueva con su contenido: el historial
    # queda intacto y la restauración misma es reversible.
    "${AWS_S3[@]}" copy-object --bucket "$BUCKET" --key "$STATE_KEY" \
      --copy-source "$BUCKET/$STATE_KEY?versionId=$VERSION_ID" >/dev/null
    echo "Restaurada. Verifica con: ./scripts/tf.sh $ENV plan"
    ;;
  *) usage ;;
esac
