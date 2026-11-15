#!/usr/bin/env bash
# Instala y registra el runner self-hosted de GitHub Actions en ESTA máquina (T1, T3).
#   - Se ejecuta con tu usuario, sin sudo, como servicio de systemd --user.
#   - Etiqueta "aegisvolt": solo los jobs con runs-on [self-hosted, aegisvolt] llegan aquí
#     (terraform-plan de PR del propio repo y deploy post-merge). Los PR de forks nunca.
#   - Necesita Floci, terraform, aws, docker, python3 y gh en el PATH.
#
# Uso:   ./scripts/setup_runner.sh            instala/registra y arranca el servicio
#        ./scripts/setup_runner.sh status     estado del servicio
#        ./scripts/setup_runner.sh remove     detiene y des-registra el runner
set -euo pipefail

RUNNER_VERSION="2.338.2"
RUNNER_DIR="$HOME/.local/share/aegisvolt-runner"
SERVICE="aegisvolt-runner.service"
UNIT_FILE="$HOME/.config/systemd/user/$SERVICE"
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

case "${1:-install}" in
  status)
    systemctl --user status "$SERVICE" --no-pager || true
    gh api "repos/$REPO/actions/runners" --jq '.runners[] | "\(.name): \(.status) [\(.labels | map(.name) | join(", "))]"'
    exit 0
    ;;
  remove)
    systemctl --user disable --now "$SERVICE" 2>/dev/null || true
    TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/remove-token" --jq .token)"
    (cd "$RUNNER_DIR" && ./config.sh remove --token "$TOKEN")
    rm -f "$UNIT_FILE"
    systemctl --user daemon-reload
    echo "Runner eliminado."
    exit 0
    ;;
  install) ;;
  *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

if [[ ! -x ./config.sh ]]; then
  echo "→ Descargando actions/runner v$RUNNER_VERSION"
  TARBALL="actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"
  curl -fsSL -o "$TARBALL" \
    "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/$TARBALL"
  # Verificación de integridad: el SHA-256 publicado en las notas de la release.
  EXPECTED="$(gh api "repos/actions/runner/releases/tags/v$RUNNER_VERSION" --jq .body |
    grep -oP "(?<=<!-- BEGIN SHA linux-x64 -->)[0-9a-f]{64}(?=<!-- END SHA linux-x64 -->)")"
  echo "$EXPECTED  $TARBALL" | sha256sum -c -
  tar xzf "$TARBALL"
  rm -f "$TARBALL"
fi

if [[ ! -f .runner ]]; then
  echo "→ Registrando el runner en $REPO"
  TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)"
  ./config.sh --unattended --replace \
    --url "https://github.com/$REPO" --token "$TOKEN" \
    --name "aegisvolt-$(hostname -s)" --labels aegisvolt --work _work
fi

echo "→ Servicio systemd --user"
mkdir -p "$(dirname "$UNIT_FILE")"
cat >"$UNIT_FILE" <<EOF
[Unit]
Description=GitHub Actions runner (AegisVolt)
After=network-online.target

[Service]
WorkingDirectory=$RUNNER_DIR
ExecStart=$RUNNER_DIR/run.sh
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=10
# Endurecimiento básico: sin escalar privilegios.
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE"
sleep 3
systemctl --user is-active "$SERVICE" && echo "Runner activo."
gh api "repos/$REPO/actions/runners" --jq '.runners[] | "\(.name): \(.status) [\(.labels | map(.name) | join(", "))]"'
