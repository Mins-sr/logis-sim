#!/usr/bin/env python3
"""Start the local SLAPStack stack and open its role entrypoint.

This module intentionally keeps orchestration in Python so the macOS and Linux
launchers share exactly the same checks and failure messages.  It never installs
OS packages and it always uses the checked-in Compose file and project name.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Iterator, Mapping, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import quote, unquote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "tof-logis"
COMPOSE_FILE = Path("config/docker/compose.yaml")
DEFAULT_PORT = 8080
DEFAULT_TIMEOUT = 120.0
LOCK_FILE = ".local/start.lock"


class StartError(RuntimeError):
    """An actionable start failure, including its orchestration stage."""

    def __init__(self, stage: str, message: str, log: Path | None = None):
        self.stage = stage
        self.log = log
        suffix = f" Log: {log}" if log else ""
        super().__init__(f"{stage}: {message}.{suffix} Re-run: scripts/start.sh --no-open")


@dataclass(frozen=True)
class CommandOutput:
    command: tuple[str, ...]
    code: int
    stdout: str
    stderr: str
    log: Path | None = None


def _root(value: Path | str | None = None) -> Path:
    return (Path(value) if value is not None else ROOT).expanduser().resolve()


def _env_file(root: Path) -> Path:
    return root / "config/env/.env"


def read_env_file(root: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without executing a secrets file."""
    path = _env_file(root)
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            values[key] = value
    return values


def compose_args(root: Path | str, command: Sequence[str]) -> list[str]:
    """Build a deterministic Compose invocation for the local project."""
    root = _root(root)
    args = ["docker", "compose", "-p", PROJECT]
    env_file = _env_file(root)
    if env_file.is_file():
        args.extend(("--env-file", str(env_file)))
    args.extend(("-f", str(root / COMPOSE_FILE), *command))
    return args


def _port(value: str | None) -> int:
    try:
        port = int(value or DEFAULT_PORT)
    except (TypeError, ValueError) as error:
        raise StartError("configuration", f"PROXY_PORT is not a number: {value!r}") from error
    if not 1 <= port <= 65535:
        raise StartError("configuration", f"PROXY_PORT must be between 1 and 65535: {port}")
    return port


def proxy_url(env: Mapping[str, str]) -> str:
    """Return the loopback-only URL used for readiness and the browser."""
    return f"http://127.0.0.1:{_port(env.get('PROXY_PORT'))}"


def _postgres_url_from_env(values: Mapping[str, str]) -> str:
    database = values.get("POSTGRES_DB", "slapstack")
    user = values.get("POSTGRES_USER", "slapstack")
    password = values.get("POSTGRES_PASSWORD", "slapstack-local-only")
    port = _port(values.get("POSTGRES_PORT", "5432"))
    # Compose's default port is 5432; _port also prevents an invalid host URL.
    return f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}/{quote(database, safe='')}"


def _database_settings(values: Mapping[str, str]) -> tuple[str, str, str, int]:
    return (
        values.get("POSTGRES_USER", "slapstack"),
        values.get("POSTGRES_PASSWORD", "slapstack-local-only"),
        values.get("POSTGRES_DB", "slapstack"),
        _port(values.get("POSTGRES_PORT", "5432")),
    )


def _validate_database_url(parsed, values: Mapping[str, str], expected_port: int, *, check_port: bool = True) -> None:
    expected_user, expected_password, expected_db, _ = _database_settings(values)
    actual_user = unquote(parsed.username or "")
    actual_password = unquote(parsed.password or "")
    actual_db = unquote(parsed.path.lstrip("/"))
    actual_port = parsed.port or 5432
    expected = (expected_user, expected_password, expected_db, expected_port)
    actual = (actual_user, actual_password, actual_db, actual_port)
    mismatch = actual != expected if check_port else actual[:3] != expected[:3]
    if mismatch:
        raise StartError("migration", "DATABASE_URL does not match the Compose Postgres settings")


def migration_database_url(root: Path | str, env: Mapping[str, str]) -> str:
    """Resolve a host-side Postgres URL and reject accidental SQLite use."""
    root = _root(root)
    file_values = read_env_file(root)
    values = {**file_values, **dict(env)}
    raw = values.get("DATABASE_URL", "").strip()
    if not raw:
        return _postgres_url_from_env(values)
    if raw.lower().startswith("sqlite"):
        raise StartError("migration", "SQLite DATABASE_URL is unsafe for Compose startup")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg", "postgres"} or not parsed.hostname:
        raise StartError("migration", "DATABASE_URL must be a PostgreSQL URL for the Compose database")
    hostname = parsed.hostname
    expected_port = _database_settings(values)[3]
    # A service-name host works inside Compose but not for host-side Alembic.
    if hostname in {"db", "postgres", "postgresql"}:
        _validate_database_url(parsed, values, expected_port, check_port=False)
        host = "127.0.0.1"
        credentials = ""
        if parsed.username is not None:
            credentials = quote(unquote(parsed.username), safe="")
            if parsed.password is not None:
                credentials += ":" + quote(unquote(parsed.password), safe="")
            credentials += "@"
        netloc = credentials + host + f":{expected_port}"
        parsed = parsed._replace(netloc=netloc)
    elif hostname in {"localhost", "127.0.0.1", "::1"}:
        _validate_database_url(parsed, values, expected_port)
    else:
        raise StartError("migration", f"DATABASE_URL host is not local or the Compose db service: {hostname}")
    return urlunsplit(parsed)


def _log_dir(root: Path) -> Path:
    path = root / ".local/logs/start"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _redacted_output(value: str, env: Mapping[str, str] | None) -> str:
    if not value or env is None:
        return value
    secrets = {env.get("POSTGRES_PASSWORD", "")}
    raw_url = env.get("DATABASE_URL", "")
    if raw_url:
        secrets.add(raw_url)
        password = urlsplit(raw_url).password
        if password:
            secrets.add(unquote(password))
    for secret in sorted((item for item in secrets if len(item) >= 2), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def run_command(command: Sequence[str], *, name: str, root: Path = ROOT,
                env: Mapping[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT,
                raw_stdout: bool = False) -> CommandOutput:
    """Run one stage, retaining bounded evidence in .local/logs/start."""
    root = _root(root)
    log = _log_dir(root) / f"{name}.log"
    try:
        completed = subprocess.run(
            list(command), cwd=root, env=dict(env) if env is not None else None,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        log.write_text(f"ERROR dependency/launch: {error}\n", encoding="utf-8")
        return CommandOutput(tuple(command), 127, "", str(error), log)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        stdout = str(stdout)
        stderr = str(stderr)
        safe_output = _redacted_output(f"ERROR timeout after {timeout}s\n{stdout}{stderr}", env)
        log.write_text(safe_output, encoding="utf-8")
        return CommandOutput(tuple(command), 124, _redacted_output(stdout, env), _redacted_output(stderr, env), log)
    output = (completed.stdout or "") + (completed.stderr or "")
    safe_output = _redacted_output(output, env)
    log.write_text(safe_output, encoding="utf-8")
    # Structured inventory must retain exact IDs and project labels in memory.
    # Logs and stderr remain redacted, including when raw stdout is requested.
    stdout = completed.stdout or ""
    return CommandOutput(tuple(command), completed.returncode, stdout if raw_stdout else _redacted_output(stdout, env), _redacted_output(completed.stderr or "", env), log)


def _require_tool(label: str, command: Sequence[str]) -> None:
    if shutil.which(command[0]) is None:
        hint = {
            "docker": "Install Docker Desktop (macOS) or Docker Engine + Compose (Linux), then retry.",
            "python": "Install Python 3.12 or newer, then retry.",
            "node": "Install Node.js 20 or newer, then retry.",
            "npm": "Install npm (bundled with Node.js), then retry.",
        }.get(label, "Install the prerequisite, then retry.")
        raise StartError("dependencies", f"{label} is missing. {hint}")


def check_dependencies(root: Path, env: Mapping[str, str], timeout: float) -> None:
    _require_tool("docker", ("docker",))
    python_command = env.get("PYTHON_BIN") or "python3"
    if os.path.sep in python_command and shutil.which(python_command) is None:
        # A missing project venv is created by the explicit setup stage; the
        # bootstrap interpreter still needs to be present before that stage.
        python_command = "python3"
    _require_tool("python", (python_command,))
    _require_tool("node", ("node",))
    _require_tool("npm", ("npm",))
    docker_info = run_command(("docker", "info"), name="docker-info", root=root, env=env, timeout=min(timeout, 30))
    if docker_info.code and sys.platform == "darwin":
        # Docker Desktop is the supported macOS runtime; opening it is safe and
        # leaves any existing containers and named volumes untouched.
        subprocess.run(("open", "-a", "Docker"), check=False)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            docker_info = run_command(("docker", "info"), name="docker-info", root=root, env=env, timeout=min(10, timeout))
            if docker_info.code == 0:
                break
            time.sleep(1)
    if docker_info.code:
        raise StartError("dependencies", "Docker daemon is unavailable", docker_info.log)
    compose_version = run_command(("docker", "compose", "version"), name="compose-version", root=root, env=env, timeout=30)
    if compose_version.code:
        raise StartError("dependencies", "Docker Compose plugin is unavailable", compose_version.log)


def frontend_dependency_fingerprint(root: Path) -> str | None:
    files = (root / "config/frontend/package.json", root / "config/frontend/package-lock.json")
    if not all(path.is_file() for path in files):
        return None
    payload = b"".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n".encode()
        for path in files
    )
    return hashlib.sha256(payload).hexdigest()


def backend_dependency_fingerprint(root: Path) -> str | None:
    files = (root / "config/backend/requirements.txt", root / "config/backend/requirements-dev.txt")
    if not all(path.is_file() for path in files):
        return None
    payload = b"".join(path.read_bytes() for path in files)
    return hashlib.sha256(payload).hexdigest()


def _setup_needed(root: Path, python_bin: str) -> bool:
    fingerprint = frontend_dependency_fingerprint(root)
    backend_fingerprint = backend_dependency_fingerprint(root)
    marker = root / ".local/frontend/.config-state"
    backend_marker = root / ".local/start-dependencies"
    return not (
        Path(python_bin).is_file() and os.access(python_bin, os.X_OK)
        and fingerprint is not None
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == f"config={fingerprint}"
        and (root / ".local/frontend/node_modules/.bin/vite").is_file()
        and backend_fingerprint is not None
        and backend_marker.is_file()
        and backend_marker.read_text(encoding="utf-8").strip() == backend_fingerprint
    )


def maybe_setup(root: Path, env: Mapping[str, str], timeout: float) -> None:
    python_bin = env.get("PYTHON_BIN") or str(root / ".local/venv/bin/python")
    # A fixture root used by unit tests may only exercise sequencing; real
    # projects always have dev.sh and therefore use the explicit setup path.
    dev = root / "scripts/dev.sh"
    if not dev.is_file() or not _setup_needed(root, python_bin):
        return
    result = run_command((str(dev), "setup"), name="setup", root=root, env=env, timeout=timeout)
    if result.code:
        raise StartError("setup", "initial setup failed", result.log)
    fingerprint = backend_dependency_fingerprint(root)
    if fingerprint:
        marker = root / ".local/start-dependencies"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint + "\n", encoding="utf-8")


def wait_http(url: str, *, timeout: float = DEFAULT_TIMEOUT, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers={"Accept": "application/json,text/html"})
            with urlopen(request, timeout=min(10, max(1, deadline - time.monotonic()))) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, URLError, TimeoutError) as error:
            last_error = str(error)
        time.sleep(interval)
    raise StartError("readiness", f"{url} did not respond successfully: {last_error}")


def open_url(url: str, env: Mapping[str, str]) -> None:
    if env.get("NO_OPEN") == "1":
        return
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) and (env.get("SSH_CONNECTION") or sys.platform != "darwin"):
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener) is None:
        print(f"ブラウザを開けませんでした。URLを開いてください: {url}")
        return
    subprocess.Popen((opener, url), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def start_lock(root: Path) -> Iterator[None]:
    path = root / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StartError("lock", "another start process is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _result_code(result: CommandOutput | subprocess.CompletedProcess) -> int:
    """Accept the real result type and simple subprocess test doubles."""
    return int(getattr(result, "code", getattr(result, "returncode", 1)))


def _result_log(result: CommandOutput | subprocess.CompletedProcess) -> Path | None:
    return getattr(result, "log", None)


def _check(result: CommandOutput | subprocess.CompletedProcess, stage: str) -> None:
    code = _result_code(result)
    if code:
        raise StartError(stage, f"command failed with exit={code}", _result_log(result))


@dataclass(frozen=True)
class RunningContainer:
    id: str
    name: str
    project: str


def running_containers(root: Path, env: Mapping[str, str], timeout: float,
                       stage: str) -> tuple[RunningContainer, ...]:
    # Read only the metadata we need; never capture environment variables or
    # other labels. Omitting --all excludes stopped containers.
    template = ('{"id":{{json .ID}},"name":{{json .Names}},'
                '"project":{{json (.Label "com.docker.compose.project")}}}')
    result = run_command(("docker", "ps", "--no-trunc", "--format", template),
                         name=stage, root=root, env=env, timeout=timeout, raw_stdout=True)
    _check(result, stage)
    try:
        rows = tuple(json.loads(line) for line in result.stdout.splitlines() if line.strip())
        for row in rows:
            if (not isinstance(row, dict)
                    or not all(isinstance(row.get(key), str) for key in ("id", "name", "project"))
                    or not re.fullmatch(r"[0-9a-f]{64}", row["id"])
                    or not row["name"]):
                raise ValueError("invalid container metadata")
        containers = tuple(RunningContainer(row["id"], row["name"], row["project"]) for row in rows)
        if len({item.id for item in containers}) != len(containers):
            raise ValueError("duplicate container IDs")
    except (ValueError, TypeError) as error:
        raise StartError(stage, "invalid Docker container inventory; startup aborted", _result_log(result)) from error
    return containers


def stop_other_containers(root: Path, env: Mapping[str, str],
                          timeout: float) -> tuple[RunningContainer, ...]:
    """Stop a snapshot of foreign workloads, then fail closed on any remainder."""
    containers = running_containers(root, env, timeout, "containers-list")
    targets = tuple(item for item in containers if item.project != PROJECT)
    if targets:
        print("他プロジェクトの起動中コンテナを停止します（データは削除しません）。", flush=True)
        for item in targets:
            # Quote untrusted labels/names so terminal control characters cannot
            # disguise the affected workload in progress output.
            print(f"  {json.dumps(item.name)} / project={json.dumps(item.project) if item.project else '所属なし'}", flush=True)
        print("停止対象は、この起動の失敗時・終了時にも自動再開しません。必要なサービスは利用者が再開してください。", flush=True)
        stopped = run_command(("docker", "stop", "--timeout", "10", *(item.id for item in targets)),
                              name="containers-stop", root=root, env=env, timeout=timeout)
        _check(stopped, "containers-stop")
    remaining = tuple(item for item in running_containers(root, env, timeout, "containers-verify")
                      if item.project != PROJECT)
    if remaining:
        names = ", ".join(json.dumps(item.name) for item in remaining)
        raise StartError("containers-verify", f"other containers still running or newly started: {names}; startup aborted",
                         _log_dir(root) / "containers-verify.log")
    if targets:
        print(f"他プロジェクトの停止確認完了: {len(targets)}コンテナ（自動再開しません）。", flush=True)
    return targets


def run_start(root: Path | str = ROOT, *, env: Mapping[str, str] | None = None,
              no_open: bool = False, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Execute startup in DB -> migration -> services -> HTTP readiness order."""
    root = _root(root)
    # An exported value wins over the checked-in local env file, matching
    # scripts/dev.sh and preventing an old file from overriding a caller's
    # explicit connection settings.
    merged = {**read_env_file(root), **os.environ, **dict(env or {})}
    merged["DATABASE_URL"] = migration_database_url(root, merged)
    merged["NO_OPEN"] = "1" if no_open else merged.get("NO_OPEN", "0")
    if root == ROOT or (root / "scripts/dev.sh").is_file():
        check_dependencies(root, merged, timeout)
        maybe_setup(root, merged, timeout)
    stop_other_containers(root, merged, timeout)
    def compose(action: Sequence[str]) -> list[str]:
        return compose_args(root, action)
    db = run_command(compose(("up", "-d", "--wait", "db")), name="compose-db", root=root, env=merged, timeout=timeout)
    _check(db, "database")
    check = run_command((str(root / "scripts/dev.sh"), "migrate", "check"), name="migrate-check", root=root, env=merged, timeout=timeout)
    check_code = _result_code(check)
    if check_code == 1:
        stopped = run_command(compose(("stop", "api", "worker", "optimizer", "proxy")), name="compose-stop", root=root, env=merged, timeout=timeout)
        _check(stopped, "migration-preparation")
        applied = run_command((str(root / "scripts/dev.sh"), "migrate", "apply"), name="migrate-apply", root=root, env=merged, timeout=timeout)
        _check(applied, "migration")
        verified = run_command((str(root / "scripts/dev.sh"), "migrate", "check"), name="migrate-check-after-apply", root=root, env=merged, timeout=timeout)
        _check(verified, "migration-verify")
    else:
        _check(check, "migrate-check")
    services = run_command(compose(("up", "-d", "--build", "--wait")), name="compose-services", root=root, env=merged, timeout=timeout)
    _check(services, "services")
    url = proxy_url(merged)
    wait_http(f"{url}/health/ready", timeout=timeout)
    wait_http(f"{url}/", timeout=timeout)
    print(f"管理画面: {url}/")
    port = _port(merged.get("PROXY_PORT"))
    if merged.get("SSH_CONNECTION") or not (merged.get("DISPLAY") or merged.get("WAYLAND_DISPLAY")):
        print(f"SSH接続: ssh -N -L {port}:127.0.0.1:{port} <user>@<server> の後、http://127.0.0.1:{port}/ を開いてください")
    print("停止: scripts/dev.sh compose down（名前付きDBボリュームは保持されます）")
    print("worker: 起動済み（API・プロキシの接続確認済み。業務機能全体の検証は別途必要です）")
    open_url(url + "/", merged)
    return url


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout per startup stage in seconds")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.timeout > 3600:
        parser.error("--timeout must be between 0 and 3600 seconds")
    root = ROOT
    try:
        with start_lock(root):
            run_start(root, no_open=args.no_open, timeout=args.timeout)
    except StartError as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
