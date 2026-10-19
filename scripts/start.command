#!/usr/bin/env bash
# Finder double-click entrypoint. Keep all startup logic in start.sh.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if "$ROOT/scripts/start.sh" "$@"; then
  exit 0
else
  status=$?
  printf '\n起動に失敗しました。上のメッセージと .local/logs/start/ を確認してください。Enterで閉じます。\n'
  read -r _ || true
  exit "$status"
fi
