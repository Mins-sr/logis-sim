#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${HARNESS_PYTHON:-}" ]]; then
  harness_python="$HARNESS_PYTHON"
elif [[ -x "$project_root/.venv312/bin/python" ]]; then
  harness_python="$project_root/.venv312/bin/python"
elif [[ -x "$project_root/.venv/bin/python" ]]; then
  harness_python="$project_root/.venv/bin/python"
else
  harness_python="python3"
fi
exec "$harness_python" "$project_root/scripts/harness.py" verify "$@"
