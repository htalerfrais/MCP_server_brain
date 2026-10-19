#!/usr/bin/env bash
# Déploie une branche de ce dépôt dans /srv/brain/app puis redémarre le service.
# À lancer en tant que hector :  scripts/deploy.sh [branche] [--no-restart]
set -euo pipefail

REF="main"
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    *) REF="$arg" ;;
  esac
done

APP=/srv/brain/app
UV="${UV:-$HOME/.local/bin/uv}"
export UV_LINK_MODE=copy UV_PYTHON=/usr/bin/python3 UV_PROJECT_ENVIRONMENT="$APP/.venv"
export PYTHONDONTWRITEBYTECODE=1
umask 027

cd "$APP"
git fetch --quiet --prune origin
git checkout --quiet --force --detach "origin/$REF"
git clean -fdq -e .venv
"$UV" sync --frozen --no-dev --quiet

# Le code reste en lecture seule pour brain (groupe), invisible pour les autres.
# Aucun cache Python dans les sources : brain ne doit rien pouvoir y écrire.
find "$APP/src" -name __pycache__ -type d -prune -exec rm -rf {} +
chmod -R g-w,o-rwx "$APP"

"$APP/.venv/bin/python" - <<PY
from pathlib import Path
from brain_mcp.policy import Policy
policy = Policy.load(Path("$APP/policy.yaml"))
print("politique valide :", ", ".join(sorted(policy.clients)))
PY

echo "déployé : $REF ($(git rev-parse --short HEAD))"
if [ "$RESTART" = 1 ]; then
  sudo -n /usr/bin/systemctl restart brain-mcp.service
  sleep 3
  systemctl is-active brain-mcp.service
fi
