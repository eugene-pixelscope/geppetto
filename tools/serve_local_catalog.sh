#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${GEPPETTO_ENV_FILE:-$REPO_ROOT/local/runtime/catalog.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

: "${GEPPETTO_MODE:=catalog}"
export GEPPETTO_MODE

exec "${GEPPETTO_PYTHON:-$REPO_ROOT/.venv/bin/python}" -m geppetto.app --port "${GEPPETTO_PORT:-8091}"
