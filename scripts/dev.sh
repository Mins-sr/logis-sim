#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CONFIG="$ROOT/config"
LOCAL="$ROOT/.local"
FRONTEND="$LOCAL/frontend"
VENV="$LOCAL/venv"
PYTHON_BIN="${PYTHON_BIN:-$VENV/bin/python}"
FRONTEND_MARKER="$FRONTEND/.config-state"

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh <command> [options]

Commands:
  setup                         Create .local workspaces and install dependencies.
  frontend dev|build|preview   Run the frontend in its .local workspace.
  api                          Run the FastAPI service.
  worker                       Run the worker service.
  optimizer                    Run the optimizer service.
  migrate check|apply          Check or apply local Alembic migrations.
  compose [--dev] ...          Run Docker Compose with project name tof-logis.
  usage [args...]               Show or update the local usage ledger.
EOF
}

ensure_local_dirs() {
  mkdir -p "$LOCAL/frontend" "$LOCAL/data" "$LOCAL/logs" "$LOCAL/cache" "$LOCAL/ledger" "$LOCAL/backups"
}

frontend_dependency_fingerprint() {
  # The marker tracks only dependency inputs. Vite and TypeScript settings are
  # copied before every command and therefore do not require reinstalling npm
  # packages.
  shasum -a 256 \
    "$CONFIG/frontend/package.json" \
    "$CONFIG/frontend/package-lock.json" | shasum -a 256 | awk '{print $1}'
}

copy_frontend_config() {
  ensure_local_dirs
  mkdir -p "$FRONTEND"
  cp "$CONFIG/frontend/package.json" "$FRONTEND/package.json"
  cp "$CONFIG/frontend/package-lock.json" "$FRONTEND/package-lock.json"
  cp "$CONFIG/frontend/vite.config.ts" "$FRONTEND/vite.config.ts"
  cp "$CONFIG/frontend/tsconfig.json" "$FRONTEND/tsconfig.json"
  if [[ -e "$FRONTEND/source" && ! -L "$FRONTEND/source" ]]; then
    rm -rf "$FRONTEND/source"
  fi
  ln -sfn ../../src/frontend "$FRONTEND/source"
}

mark_frontend_ready() {
  local fingerprint
  fingerprint="$(frontend_dependency_fingerprint)"
  printf 'config=%s\n' "$fingerprint" >"$FRONTEND_MARKER"
}

refresh_frontend_workspace() {
  # Commands may be invoked after config edits.  Refresh copies, but never
  # install dependencies implicitly; require_frontend_setup enforces the
  # explicit setup marker below.
  copy_frontend_config
}

require_frontend_setup() {
  if [[ ! -f "$FRONTEND/package.json" || ! -L "$FRONTEND/source" ]]; then
    printf 'Frontend workspace is absent. Run %q setup first.\n' "$ROOT/scripts/dev.sh" >&2
    exit 2
  fi
  local expected actual
  if [[ ! -f "$FRONTEND_MARKER" ]]; then
    printf 'Frontend setup marker is absent. Run %q setup first.\n' "$ROOT/scripts/dev.sh" >&2
    exit 2
  fi
  expected="config=$(frontend_dependency_fingerprint)"
  actual="$(cat "$FRONTEND_MARKER")"
  if [[ "$actual" != "$expected" ]]; then
    printf 'Frontend configuration or lockfile is stale. Run %q setup first.\n' "$ROOT/scripts/dev.sh" >&2
    exit 2
  fi
  if [[ ! -x "$FRONTEND/node_modules/.bin/vite" ]]; then
    printf 'Frontend dependencies are absent. Run %q setup first.\n' "$ROOT/scripts/dev.sh" >&2
    exit 2
  fi
}

require_python() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    printf 'Python environment is absent. Run %q setup first or set PYTHON_BIN.\n' "$ROOT/scripts/dev.sh" >&2
    exit 2
  fi
}

run_python_module() {
  require_python
  (cd "$ROOT" && PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    DATABASE_URL="${DATABASE_URL:-sqlite:///$LOCAL/data/slapstack.db}" \
    OPTIMIZER_URL="${OPTIMIZER_URL:-http://127.0.0.1:8001}" \
    "$PYTHON_BIN" -m "$@")
}

setup() {
  ensure_local_dirs
  copy_frontend_config
  if [[ ! -x "$PYTHON_BIN" ]]; then
    local bootstrap_python="${PYTHON_BOOTSTRAP:-python3.12}"
    if ! command -v "$bootstrap_python" >/dev/null 2>&1; then
      bootstrap_python="python3"
    fi
    "$bootstrap_python" -m venv "$VENV"
  fi
  "$PYTHON_BIN" -m pip install -r "$CONFIG/backend/requirements-dev.txt"
  (cd "$FRONTEND" && npm ci)
  mark_frontend_ready
  prepare_compose_frontend_dependencies
}

frontend() {
  local action="${1:-dev}"
  shift || true
  refresh_frontend_workspace
  require_frontend_setup
  case "$action" in
    dev|build|preview)
      (cd "$FRONTEND" && TOF_LOGIS_VITE_OUT_DIR="$FRONTEND/dist" npm run "$action" -- "$@")
      ;;
    *)
      printf 'Unknown frontend action: %s\n' "$action" >&2
      usage >&2
      exit 2
      ;;
  esac
}

migrate() {
  local action="${1:-check}"
  shift || true
  case "$action" in
    check|apply) ;;
    *) printf 'Migration action must be check or apply.\n' >&2; exit 2 ;;
  esac
  require_python
  local database_url="${DATABASE_URL:-}"
  if [[ -z "$database_url" && -f "$CONFIG/env/.env" ]]; then
    # Compose's env file is also the host migration input when it defines a
    # DATABASE_URL. Parse only that key; do not execute arbitrary shell text
    # from a local secrets file. An exported DATABASE_URL always wins.
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line#${line%%[![:space:]]*}}"
      if [[ "$line" == DATABASE_URL=* ]]; then
        database_url="${line#DATABASE_URL=}"
        if [[ "$database_url" == \"*\" && "$database_url" == *\" ]]; then
          database_url="${database_url:1:${#database_url}-2}"
        elif [[ "$database_url" == \'*\' && "$database_url" == *\' ]]; then
          database_url="${database_url:1:${#database_url}-2}"
        fi
        break
      fi
    done < "$CONFIG/env/.env"
  fi
  (cd "$ROOT" && PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    DATABASE_URL="${database_url:-sqlite:///$LOCAL/data/slapstack.db}" \
    "$PYTHON_BIN" "$ROOT/scripts/migrate_db.py" "$action" "$@")
}

prepare_compose_frontend_dependencies() {
  # The development overlay deliberately uses a named node_modules volume so
  # the host's dependencies never leak into the container.  Populate it only
  # during explicit setup; compose --dev never runs npm install.
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    return 0
  fi
  local -a compose_args=(docker compose -p tof-logis)
  if [[ -f "$CONFIG/env/.env" ]]; then
    compose_args+=(--env-file "$CONFIG/env/.env")
  fi
  compose_args+=(-f "$CONFIG/docker/compose.yaml" -f "$CONFIG/docker/compose.dev.yaml"
    run --rm --no-deps frontend npm ci)
  (cd "$ROOT" && "${compose_args[@]}")
}

compose() {
  local dev=0
  if [[ "${1:-}" == "--dev" ]]; then
    dev=1
    shift
  fi
  if (( dev )); then
    refresh_frontend_workspace
    require_frontend_setup
  fi
  local -a compose_args=(docker compose -p tof-logis)
  if [[ -f "$CONFIG/env/.env" ]]; then
    compose_args+=(--env-file "$CONFIG/env/.env")
  fi
  compose_args+=(-f "$CONFIG/docker/compose.yaml")
  if (( dev )); then
    compose_args+=(-f "$CONFIG/docker/compose.dev.yaml")
  fi
  compose_args+=("$@")
  (cd "$ROOT" && "${compose_args[@]}")
}

usage_command() {
  local usage_python="${HARNESS_PYTHON:-}"
  if [[ -z "$usage_python" ]]; then
    if [[ -x "$VENV/bin/python" ]]; then
      usage_python="$VENV/bin/python"
    elif [[ -x "$ROOT/.venv312/bin/python" ]]; then
      usage_python="$ROOT/.venv312/bin/python"
    elif [[ -x "$ROOT/.venv/bin/python" ]]; then
      usage_python="$ROOT/.venv/bin/python"
    else
      usage_python="python3"
    fi
  fi
  (cd "$ROOT" && "$usage_python" "$ROOT/scripts/harness.py" usage "$@")
}

main() {
  local command="${1:-help}"
  shift || true
  case "$command" in
    setup) setup "$@" ;;
    frontend) frontend "$@" ;;
    api) run_python_module uvicorn app.main:app --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8000}" "$@" ;;
    worker) run_python_module worker.main "$@" ;;
    optimizer) run_python_module uvicorn optimizer.main:app --host "${OPTIMIZER_HOST:-127.0.0.1}" --port "${OPTIMIZER_PORT:-8001}" "$@" ;;
    migrate) migrate "$@" ;;
    compose) compose "$@" ;;
    usage) usage_command "$@" ;;
    help|-h|--help) usage ;;
    *) printf 'Unknown command: %s\n' "$command" >&2; usage >&2; exit 2 ;;
  esac
}

main "$@"
