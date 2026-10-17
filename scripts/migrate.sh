#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
MIGRATE_PYTHON="${PYTHON_BIN:-}"
if [[ -z "$MIGRATE_PYTHON" ]]; then
  if [[ -x "$ROOT/.local/venv/bin/python" ]]; then
    MIGRATE_PYTHON="$ROOT/.local/venv/bin/python"
  elif [[ -x "$ROOT/.venv312/bin/python" ]]; then
    MIGRATE_PYTHON="$ROOT/.venv312/bin/python"
  elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    MIGRATE_PYTHON="$ROOT/.venv/bin/python"
  else
    MIGRATE_PYTHON="python3"
  fi
fi
action="${1:-check}"
shift || true

case "$action" in
  layout)
    exec "$MIGRATE_PYTHON" "$ROOT/scripts/migrate_layout.py" "${1:-check}" "${@:2}"
    ;;
  db)
    exec "$MIGRATE_PYTHON" "$ROOT/scripts/migrate_db.py" "${1:-check}" "${@:2}"
    ;;
  check|apply)
    exec "$MIGRATE_PYTHON" "$ROOT/scripts/migrate_db.py" "$action" "$@"
    ;;
  *)
    printf 'Usage: %s [check|apply|layout check|layout apply|db check|db apply]\n' "$0" >&2
    exit 2
    ;;
esac
