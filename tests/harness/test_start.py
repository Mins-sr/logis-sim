"""Regression tests for the one-button local start entrypoint."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/start.py"


def load_start():
    spec = importlib.util.spec_from_file_location("tof_start", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


start = load_start()


def test_root_is_derived_from_script_location():
    assert start.ROOT == ROOT
    assert "tof-logis" in str(start.ROOT)


def test_compose_args_use_project_and_checked_in_config(tmp_path):
    args = start.compose_args(tmp_path, ["up", "-d", "db"])
    assert args[:4] == ["docker", "compose", "-p", "tof-logis"]
    assert "-f" in args
    assert str(tmp_path / "config/docker/compose.yaml") in args

    env = tmp_path / "config/env"
    env.mkdir(parents=True)
    (env / ".env").write_text("POSTGRES_PORT=55432\n", encoding="utf-8")
    args = start.compose_args(tmp_path, ["config", "--services"])
    assert args[4:6] == ["--env-file", str(env / ".env")]


def test_migration_database_url_rejects_sqlite_and_defaults_to_postgres(tmp_path):
    assert start.migration_database_url(tmp_path, {}) == (
        "postgresql+psycopg://slapstack:slapstack-local-only@127.0.0.1:5432/slapstack"
    )
    with pytest.raises(start.StartError, match="SQLite"):
        start.migration_database_url(tmp_path, {"DATABASE_URL": "sqlite:///unsafe.db"})


def test_migration_database_url_uses_compose_env_and_rewrites_service_host(tmp_path):
    env = tmp_path / "config/env"
    env.mkdir(parents=True)
    (env / ".env").write_text(
        "POSTGRES_DB=demo\nPOSTGRES_USER=alice\nPOSTGRES_PASSWORD=secret\nPOSTGRES_PORT=55432\n",
        encoding="utf-8",
    )
    value = start.migration_database_url(tmp_path, {})
    assert value == "postgresql+psycopg://alice:secret@127.0.0.1:55432/demo"
    assert start.migration_database_url(tmp_path, {"DATABASE_URL": "postgresql+psycopg://alice:secret@db:5432/demo"}) == (
        "postgresql+psycopg://alice:secret@127.0.0.1:55432/demo"
    )
    with pytest.raises(start.StartError, match="Compose Postgres settings"):
        start.migration_database_url(tmp_path, {"DATABASE_URL": "postgresql+psycopg://alice:secret@127.0.0.1:5432/demo"})


def test_parse_proxy_port_is_local_and_validates_boundary():
    assert start.proxy_url({}) == "http://127.0.0.1:8080"
    assert start.proxy_url({"PROXY_PORT": "8123"}) == "http://127.0.0.1:8123"
    for value in ("0", "65536", "not-a-port"):
        with pytest.raises(start.StartError):
            start.proxy_url({"PROXY_PORT": value})


def test_start_sequence_stops_after_failed_step(tmp_path, monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    def fake(command, *, name, **kwargs):
        calls.append((name, list(command)))
        code = 2 if name == "migrate-check" else 0
        return subprocess.CompletedProcess(command, code, "", "")

    monkeypatch.setattr(start, "run_command", fake)
    with pytest.raises(start.StartError, match="migrate-check"):
        start.run_start(tmp_path, env={"PROXY_PORT": "8080"}, no_open=True, timeout=1)
    assert [name for name, _ in calls] == ["containers-list", "containers-verify", "compose-db", "migrate-check"]


def test_shell_wrappers_call_common_entrypoint():
    shell = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")
    command = (ROOT / "scripts/start.command").read_text(encoding="utf-8")
    assert "start.py" in shell and "exec" in shell
    assert "start.sh" in command


def test_read_env_file_ignores_comments_and_does_not_execute(tmp_path):
    env = tmp_path / "config/env"
    env.mkdir(parents=True)
    (env / ".env").write_text("# comment\nA=one\nB='two'\nBAD LINE\n", encoding="utf-8")
    assert start.read_env_file(tmp_path) == {"A": "one", "B": "two"}


@pytest.mark.parametrize("value", ["0", "65536", "abc"])
def test_proxy_port_validation(value):
    with pytest.raises(start.StartError):
        start.proxy_url({"PROXY_PORT": value})


def test_run_command_keeps_logs_for_success_missing_and_timeout(tmp_path):
    ok = start.run_command([sys.executable, "-c", "print('ok')"], name="ok", root=tmp_path)
    assert ok.code == 0 and ok.log and ok.log.read_text(encoding="utf-8") == "ok\n"
    missing = start.run_command([str(tmp_path / "missing")], name="missing", root=tmp_path)
    assert missing.code == 127 and "dependency" in missing.log.read_text(encoding="utf-8")
    timeout = start.run_command([sys.executable, "-c", "import time; time.sleep(1)"], name="timeout", root=tmp_path, timeout=.01)
    assert timeout.code == 124 and "timeout" in timeout.log.read_text(encoding="utf-8")


def test_maybe_setup_runs_explicit_setup_only_when_workspace_is_incomplete(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dev = scripts / "dev.sh"
    dev.write_text("#!/bin/sh\n", encoding="utf-8")
    dev.chmod(0o755)
    calls = []

    def fake(command, *, name, **kwargs):
        calls.append((name, list(command)))
        return start.CommandOutput(tuple(command), 0, "", "")

    monkeypatch.setattr(start, "run_command", fake)
    start.maybe_setup(tmp_path, {}, 1)
    assert calls and calls[0][0] == "setup"


def test_setup_needed_tracks_dependency_fingerprint(tmp_path):
    config = tmp_path / "config/frontend"
    config.mkdir(parents=True)
    (config / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    (config / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    backend = tmp_path / "config/backend"
    backend.mkdir(parents=True)
    (backend / "requirements.txt").write_text("fastapi==1\n", encoding="utf-8")
    (backend / "requirements-dev.txt").write_text("-r requirements.txt\n", encoding="utf-8")
    python_bin = tmp_path / ".local/venv/bin/python"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    python_bin.chmod(0o755)
    vite = tmp_path / ".local/frontend/node_modules/.bin/vite"
    vite.parent.mkdir(parents=True)
    vite.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = tmp_path / ".local/frontend/.config-state"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"config={start.frontend_dependency_fingerprint(tmp_path)}\n", encoding="utf-8")
    backend_marker = tmp_path / ".local/start-dependencies"
    backend_marker.write_text(f"{start.backend_dependency_fingerprint(tmp_path)}\n", encoding="utf-8")
    assert start._setup_needed(tmp_path, str(python_bin)) is False
    (config / "package.json").write_text('{"name":"changed"}\n', encoding="utf-8")
    assert start._setup_needed(tmp_path, str(python_bin)) is True


def test_command_logs_redact_database_password(tmp_path):
    env = {"DATABASE_URL": "postgresql+psycopg://alice:secret@127.0.0.1:5432/demo", "POSTGRES_PASSWORD": "secret"}
    command = [sys.executable, "-c", "print('secret postgresql+psycopg://alice:secret@127.0.0.1:5432/demo')"]
    result = start.run_command(command, name="redact", root=tmp_path, env=env)
    log = result.log.read_text(encoding="utf-8")
    assert "secret" not in log and "[REDACTED]" in log


def test_frontend_has_role_entry_and_actual_membership_guardrails():
    source = (ROOT / "src/frontend/main.tsx").read_text(encoding="utf-8")
    assert all(label in source for label in ("管理者として開く", "荷主として参加", "ドライバーとして参加"))
    assert "state.membership.role" in source
    assert "BroadcastChannel(\"tof-logis-role\")" in source
    assert "authInFlight" in source and "authAttempt" in source
    assert "type: \"CREATE_JOB\"" in source and "type: \"ACCEPT_JOB\"" in source
    assert "state.membership.is_host" in source and "パスワードが変更" in source


def test_wait_http_and_open_url(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(start, "urlopen", lambda *args, **kwargs: Response())
    start.wait_http("http://127.0.0.1:8080/", timeout=.1, interval=.01)
    opened = []
    monkeypatch.setattr(start.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(start.subprocess, "Popen", lambda command, **kwargs: opened.append(command))
    start.open_url("http://127.0.0.1:8080/", {})
    opener = "open" if start.sys.platform == "darwin" else "xdg-open"
    assert opened == [(opener, "http://127.0.0.1:8080/")]
    start.open_url("http://127.0.0.1:8080/", {"NO_OPEN": "1"})
    assert len(opened) == 1
    start.open_url("http://127.0.0.1:8080/", {"SSH_CONNECTION": "1"})
    assert len(opened) == 1


def test_start_lock_rejects_nested_start(tmp_path):
    with start.start_lock(tmp_path):
        with pytest.raises(start.StartError, match="already running"):
            with start.start_lock(tmp_path):
                pass


def test_run_start_migrates_when_check_reports_pending_and_waits_for_proxy(tmp_path, monkeypatch):
    calls = []
    checks = 0

    def fake(command, *, name, **kwargs):
        nonlocal checks
        calls.append(name)
        if name == "migrate-check":
            checks += 1
            code = 1 if checks == 1 else 0
        else:
            code = 0
        return start.CommandOutput(tuple(command), code, "", "")

    monkeypatch.setattr(start, "run_command", fake)
    urls = []
    monkeypatch.setattr(start, "wait_http", lambda url, **kwargs: urls.append(url))
    monkeypatch.setattr(start, "open_url", lambda *args, **kwargs: None)
    assert start.run_start(tmp_path, env={"PROXY_PORT": "8123"}, no_open=True, timeout=1) == "http://127.0.0.1:8123"
    assert calls == ["containers-list", "containers-verify", "compose-db", "migrate-check", "compose-stop", "migrate-apply", "migrate-check-after-apply", "compose-services"]
    assert urls == ["http://127.0.0.1:8123/health/ready", "http://127.0.0.1:8123/"]


def test_run_start_stops_before_services_when_post_migration_check_fails(tmp_path, monkeypatch):
    calls = []

    def fake(command, *, name, **kwargs):
        calls.append(name)
        code = 1 if name in {"migrate-check", "migrate-check-after-apply"} else 0
        return start.CommandOutput(tuple(command), code, "", "")

    monkeypatch.setattr(start, "run_command", fake)
    with pytest.raises(start.StartError, match="migration-verify"):
        start.run_start(tmp_path, env={"PROXY_PORT": "8123"}, no_open=True, timeout=1)
    assert calls == ["containers-list", "containers-verify", "compose-db", "migrate-check", "compose-stop", "migrate-apply", "migrate-check-after-apply"]


def container_row(key="a", name="traefik", project="cvat"):
    return json.dumps({"id": key * 64, "name": name, "project": project})


def container_runner(monkeypatch, tmp_path, before="", after="", failure=None):
    calls = []
    monkeypatch.setattr(start, "wait_http", lambda *args, **kwargs: None)
    monkeypatch.setattr(start, "open_url", lambda *args, **kwargs: None)

    def fake(command, *, name, **kwargs):
        calls.append((name, tuple(command), kwargs))
        stdout = before if name == "containers-list" else after if name == "containers-verify" else ""
        code = failure[1] if failure and failure[0] == name else 0
        return start.CommandOutput(tuple(command), code, stdout, "", tmp_path / f"{name}.log")

    monkeypatch.setattr(start, "run_command", fake)
    return calls


@pytest.mark.parametrize("before", ["", container_row(project="tof-logis")])
def test_stop_others_empty_or_own_only_never_stops(tmp_path, monkeypatch, before):
    calls = container_runner(monkeypatch, tmp_path, before, before)
    assert start.stop_other_containers(tmp_path, {}, 7) == ()
    assert [name for name, _, _ in calls] == ["containers-list", "containers-verify"]
    for _, command, kwargs in calls:
        # docker ps without --all excludes stopped containers.
        assert command[:3] == ("docker", "ps", "--no-trunc")
        assert "--all" not in command and "-a" not in command
        assert kwargs["timeout"] == 7


def test_stop_others_uses_exact_project_and_snapshot_ids(tmp_path, monkeypatch, capsys):
    own = container_row("a", "unrelated-name", "tof-logis")
    outsiders = [container_row("b"), container_row("c", "tof-logis-fake", ""),
                 container_row("d", "similar", "tof-logis-other")]
    calls = container_runner(monkeypatch, tmp_path, "\n".join([own, *outsiders]), own)
    env = {"DOCKER_HOST": "unix:///test.sock"}
    stopped = start.stop_other_containers(tmp_path, env, 7)
    assert tuple(item.id for item in stopped) == tuple(key * 64 for key in "bcd")
    assert [name for name, _, _ in calls] == ["containers-list", "containers-stop", "containers-verify"]
    assert calls[1][1] == ("docker", "stop", "--timeout", "10", *(key * 64 for key in "bcd"))
    assert all(kwargs["env"] == env and kwargs["timeout"] == 7 for _, _, kwargs in calls)
    output = capsys.readouterr().out
    assert all(word in output for word in ("traefik", "cvat", "tof-logis-fake", "所属なし", "自動再開しません"))


@pytest.mark.parametrize("phase,code", [(phase, code) for phase in
                         ("containers-list", "containers-stop", "containers-verify") for code in (1, 124)])
def test_stop_others_command_failure_aborts_before_db(tmp_path, monkeypatch, phase, code):
    calls = container_runner(monkeypatch, tmp_path, container_row(), failure=(phase, code))
    with pytest.raises(start.StartError, match=phase) as error:
        start.run_start(tmp_path, no_open=True, timeout=7)
    assert str(tmp_path / f"{phase}.log") in str(error.value)
    assert calls[-1][0] == phase
    assert not any(name.startswith("compose") for name, _, _ in calls)


@pytest.mark.parametrize("bad", ["not-json", "null", "[]", "{}",
    json.dumps({"id": "--all", "name": "x", "project": "cvat"}),
    json.dumps({"id": "a" * 64, "name": "", "project": "cvat"}),
    json.dumps({"id": "a" * 64, "name": "x", "project": None}),
    container_row() + "\n" + container_row(),
])
def test_invalid_inventory_never_stops_or_starts(tmp_path, monkeypatch, bad):
    calls = container_runner(monkeypatch, tmp_path, bad)
    with pytest.raises(start.StartError, match="containers-list"):
        start.run_start(tmp_path, no_open=True, timeout=7)
    assert [name for name, _, _ in calls] == ["containers-list"]


@pytest.mark.parametrize("before,after", [
    (container_row(), container_row()),
    (container_row(), container_row("b", "new-service", "other")),
    ("", container_row()),
    (container_row(), "invalid-json"),
])
def test_remaining_new_or_invalid_inventory_aborts_before_db(tmp_path, monkeypatch, before, after):
    calls = container_runner(monkeypatch, tmp_path, before, after)
    with pytest.raises(start.StartError, match="containers-verify") as error:
        start.run_start(tmp_path, no_open=True, timeout=7)
    assert "containers-verify.log" in str(error.value)
    assert calls[-1][0] == "containers-verify"
    assert not any(name.startswith("compose") for name, _, _ in calls)


def test_dependencies_setup_stop_verify_then_db_without_restore(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/dev.sh").touch()
    calls = container_runner(monkeypatch, tmp_path, container_row(), failure=("compose-db", 1))
    monkeypatch.setattr(start, "check_dependencies", lambda *args: calls.append(("deps", (), {})))
    monkeypatch.setattr(start, "maybe_setup", lambda *args: calls.append(("setup", (), {})))
    with pytest.raises(start.StartError, match="database"):
        start.run_start(tmp_path, no_open=True, timeout=7)
    assert [name for name, _, _ in calls] == ["deps", "setup", "containers-list", "containers-stop", "containers-verify", "compose-db"]
    assert all(token not in command for _, command, _ in calls for token in ("rm", "prune", "down", "kill", "start", "restart"))


@pytest.mark.parametrize("password", ["aa", "tof-logis"])
def test_inventory_uses_raw_metadata_but_redacts_logs(tmp_path, monkeypatch, password):
    own = container_row("b", "own", "tof-logis")
    other = container_row("a", "other", "cvat")
    commands = []
    lists = 0

    def fake_subprocess(command, **kwargs):
        nonlocal lists
        commands.append(tuple(command))
        if command[1] == "ps":
            lists += 1
            stdout = own + "\n" + other if lists == 1 else own
        else:
            stdout = "a" * 64
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(start.subprocess, "run", fake_subprocess)
    stopped = start.stop_other_containers(tmp_path, {"POSTGRES_PASSWORD": password}, 7)
    assert tuple(item.id for item in stopped) == ("a" * 64,)
    assert commands[1] == ("docker", "stop", "--timeout", "10", "a" * 64)
    log = (tmp_path / ".local/logs/start/containers-list.log").read_text()
    assert password not in log and "[REDACTED]" in log


def test_frontend_exposes_role_switch_safety_and_command_result_flow():
    source = (ROOT / "src/frontend/main.tsx").read_text(encoding="utf-8")
    assert "postMessage({ type: \"role-changed\"" in source
    assert "source: tabId.current" in source
    assert "setUserId" in source and "setSelectedRole(actualRole" in source
    assert "rejection_reason" in source and "結果を確認できませんでした" in source
    assert "job.version" in source and "assigned_user_id" in source
    assert "案件登録" in source and "案件参加" in source
    assert "計画を生成" in source and "計画を取得" in source and "START_TRIP" in source
    assert "trips" in source and "進捗" in source
    assert "停止" in source and "60倍" in source
