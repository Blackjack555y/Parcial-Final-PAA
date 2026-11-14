#!/usr/bin/env bash
# Configura la gobernanza del repositorio en GitHub (idempotente: se puede re-ejecutar).
#   1. Métodos de merge: solo "merge commit" (dev, test y prod comparten historia al promover).
#   2. Actions: token de solo lectura por defecto y aprobación obligatoria para PR de forks (T3).
#   3. Ruleset de dev/test/prod: sin force push, sin borrado, sin push directo, checks obligatorios.
# Requisitos: gh autenticado con permisos de admin sobre el repo; el repo debe ser público (T2).
# Uso: ./scripts/setup_rulesets.sh [owner/repo]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULESET_FILE="$ROOT/.github/rulesets/protected-branches.json"
REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"

echo "Repositorio: $REPO"

VISIBILITY="$(gh repo view "$REPO" --json visibility --jq .visibility)"
if [[ "$VISIBILITY" != "PUBLIC" ]]; then
  echo "ERROR: el repo es $VISIBILITY. Los rulesets solo se aplican gratis en repos públicos." >&2
  exit 1
fi

for branch in dev test prod; do
  gh api "repos/$REPO/branches/$branch" --silent 2>/dev/null ||
    { echo "ERROR: falta la rama '$branch' en el remoto. Créala antes." >&2; exit 1; }
done

echo "→ Métodos de merge (solo merge commit; no borrar ramas tras el merge)"
gh api -X PATCH "repos/$REPO" --silent \
  -F allow_merge_commit=true -F allow_squash_merge=false -F allow_rebase_merge=false \
  -F delete_branch_on_merge=false

echo "→ Actions: GITHUB_TOKEN de solo lectura por defecto"
gh api -X PUT "repos/$REPO/actions/permissions/workflow" --silent \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false

echo "→ Actions: PR de cualquier colaborador externo requieren aprobación"
gh api -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --silent \
  -f approval_policy=all_external_contributors

NAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$RULESET_FILE")"
EXISTING_ID="$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$NAME\") | .id")"
if [[ -n "$EXISTING_ID" ]]; then
  echo "→ Actualizando ruleset '$NAME' (id $EXISTING_ID)"
  gh api -X PUT "repos/$REPO/rulesets/$EXISTING_ID" --input "$RULESET_FILE" --silent
else
  echo "→ Creando ruleset '$NAME'"
  gh api -X POST "repos/$REPO/rulesets" --input "$RULESET_FILE" --silent
fi

echo
echo "Listo. Reglas activas en dev, test y prod:"
gh api "repos/$REPO/rules/branches/test" --jq '.[].type' | sort -u | sed 's/^/  - /'
