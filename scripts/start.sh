#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$ROOT/.local/venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.local/venv/bin/python"
  elif [[ -x "$ROOT/.venv312/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv312/bin/python"
  elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  printf 'Python 3 is required. Install it, then run %s again.\n' "$0" >&2
  exit 2
fi

cd "$ROOT"
exec "$PYTHON_BIN" "$ROOT/scripts/start.py" "$@"
