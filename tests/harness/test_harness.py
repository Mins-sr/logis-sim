"""Harness contracts: no paid model calls or application database access."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from scripts import harness as h
from scripts import migrate_db, migrate_layout as ml


@pytest.mark.parametrize("paths,expected", [
    (["src/app/main.py"], ("backend",)),
    (["src/shared/vrpb.py", "src/worker/main.py", "src/optimizer/main.py", "src/db/migrations/env.py"], ("backend",)),
    (["src/frontend/main.tsx"], ("frontend",)),
    (["package.json", "package-lock.json", "vite.config.ts", "tsconfig.json"], ("frontend",)),
    (["scripts/harness.py", "tests/harness/test_harness.py"], ("harness",)),
    (["tests/test_api.py"], ("backend",)),
    (["docs/TESTS.md"], ("layout",)),
    (["config/layout.json"], h.SCOPES),
    (["scripts/dev.sh"], h.SCOPES),
    (["AGENTS.md"], h.SCOPES),
    (["pyproject.toml"], h.SCOPES),
    (["requirements-dev.txt"], h.SCOPES),
    (["unknown.file"], h.SCOPES),
    (["src/frontend/a.ts", "src/app/main.py"], ("backend", "frontend")),
    ([], ()),
])
def test_scope_mapping(paths, expected):
    assert h.select_scopes(paths) == expected


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_changes_include_staged_unstaged_untracked_and_rename(tmp_path):
    git(tmp_path, "init")
    assert h.changed_files(tmp_path) is None
    for name in ("staged.py", "unstaged.py", "old name.py"):
        (tmp_path / name).write_text("original\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "test: fixture")
    (tmp_path / "staged.py").write_text("staged\n")
    git(tmp_path, "add", "staged.py")
    # Even cancelling a staged edit in the working tree must remain selected.
    (tmp_path / "staged.py").write_text("original\n")
    (tmp_path / "unstaged.py").write_text("unstaged\n")
    (tmp_path / "new file.py").write_text("new\n")
    git(tmp_path, "mv", "old name.py", "renamed.py")
    assert set(h.changed_files(tmp_path)) == {
        "staged.py", "unstaged.py", "new file.py", "old name.py", "renamed.py",
    }


def test_non_git_is_error(tmp_path):
    with pytest.raises(ValueError, match="Git"):
        h.changed_files(tmp_path)


def test_run_success_failure_missing_timeout_and_log_bound(tmp_path):
    success = h.run_step("ok", [sys.executable, "-c", "print('ok')"], tmp_path, tmp_path)
    assert success.code == 0
    failed = h.run_step("test", [sys.executable, "-c",
        "print('noise\\n' * 3000); print('FAILED tests/test_x.py::test_bad'); "
        "print('tests/test_x.py:23: AssertionError'); raise SystemExit(7)"], tmp_path, tmp_path)
    assert failed.code == 7
    assert failed.log.stat().st_size > 15000
    missing = h.run_step("missing", [str(tmp_path / "not-installed")], tmp_path, tmp_path)
    assert missing.code == 127
    timeout = h.run_step("timeout", [sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, tmp_path, timeout=0.05)
    assert timeout.code == 124
    summary = h.summarize([success, failed, missing, timeout], ["WARN example"], tmp_path)
    assert len(summary.splitlines()) <= 40
    assert "test_bad" in summary and ":23:" in summary and "exit=7" in summary
    assert "noise" not in summary
    assert "FAIL" in summary and "124" in summary and "127" in summary
    assert "PASS" in h.summarize([success], [], tmp_path)


def test_working_directory_isolates_database(tmp_path):
    root = tmp_path / "project"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    database = root / "test-api.db"
    database.write_bytes(b"keep this database")
    result = h.run_step("db", [sys.executable, "-c",
        "from pathlib import Path; Path('test-api.db').write_text('temporary')"], scratch, tmp_path)
    assert result.code == 0
    assert database.read_bytes() == b"keep this database"
    assert (scratch / "test-api.db").read_text() == "temporary"


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "oops", "1e9999"])
def test_invalid_credits(value):
    with pytest.raises(ValueError):
        h.credit_value(value)


def record(path, task="task-1", credits=None, at="2026-10-01T00:00:00+09:00", **kwargs):
    h.record_usage(path, task, "gpt-5.6-luna", "success", credits,
                   "利用画面・当該タスクのみ" if credits is not None else "未計測", at, **kwargs)


def test_usage_unknown_duplicate_and_settlement(tmp_path):
    path = tmp_path / "usage.jsonl"
    record(path)
    with pytest.raises(ValueError, match="重複"):
        record(path)
    report = h.usage_summary(path, "2026-10")
    assert "未計測=1" in report and "確定" not in report
    h.settle_usage(path, "task-1", "12.5", "利用画面・個別実測")
    report = h.usage_summary(path, "2026-10")
    assert "12.5" in report and "2987.5" in report and "未計測=0" in report
    with pytest.raises(ValueError):
        h.settle_usage(path, "task-1", "1", "source")
    with pytest.raises(ValueError):
        h.settle_usage(path, "absent", "1", "source")


@pytest.mark.parametrize("amount,label", [("0", "OK"), ("2399.9", "OK"), ("2400", "WARN"), ("3000", "STOP"), ("3001", "STOP")])
def test_budget_thresholds(tmp_path, amount, label):
    path = tmp_path / "usage.jsonl"
    record(path, credits=amount)
    assert h.usage_summary(path, "2026-10").startswith(label)


def test_month_uses_japan_timezone(tmp_path):
    path = tmp_path / "usage.jsonl"
    record(path, "september", "1", "2026-09-30T14:59:59+00:00")
    record(path, "october", "2", "2026-09-30T15:00:00+00:00")
    assert "実測合計=1 " in h.usage_summary(path, "2026-09")
    assert "実測合計=2 " in h.usage_summary(path, "2026-10")
    assert "実測合計=0 " in h.usage_summary(path, "2026-11")


@pytest.mark.parametrize("field,value", [
    ("task", "bad\nname"), ("task", ""), ("model", "unknown"), ("status", "maybe"),
    ("at", "2026-10-01"), ("at", "invalid"), ("source", ""), ("source", "bad\nsource"),
])
def test_invalid_record_inputs(tmp_path, field, value):
    args = dict(path=tmp_path / "usage.jsonl", task="task", model="gpt-5.6-sol", status="failed",
                credits="1", source="usage screen", at="2026-10-01T00:00:00+09:00")
    with pytest.raises(ValueError):
        h.record_usage(**{**args, field: value})
    assert not args["path"].exists()


def test_corrupt_ledger_and_invalid_month_fail(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text("not json\n")
    with pytest.raises(ValueError):
        h.usage_summary(path, "2026-10")
    with pytest.raises(ValueError):
        h.usage_summary(path, "2026-13")
    path.write_text('{}\n')
    with pytest.raises(ValueError):
        h.usage_summary(path, "2026-10")


def test_verification_routes_and_isolation(tmp_path, monkeypatch, capsys):
    calls = []
    def fake_step(name, command, cwd, log_dir, **kwargs):
        calls.append((name, command, cwd, kwargs))
        log = log_dir / (name + ".log")
        log.write_text("")
        if name == "backend-coverage":
            (log_dir / "backend-coverage.json").write_text(json.dumps({"totals": {"percent_covered": 50}}))
        if name == "harness-coverage":
            (log_dir / "harness-coverage.json").write_text(json.dumps({"totals": {"percent_covered": 90}}))
        return h.Result(name, 0, log)
    monkeypatch.setattr(h, "run_step", fake_step)
    (tmp_path / ".local/frontend").mkdir(parents=True)
    assert h.verify(tmp_path, h.SCOPES) == 0
    output = capsys.readouterr().out
    assert "PASS" in output and "50.0%" in output and "未整備" in output
    backend = next(call for call in calls if call[0] == "backend-tests")
    assert backend[2] != tmp_path and not backend[2].exists()
    assert "--source=" + ",".join(str(tmp_path / part) for part in h.BACKEND) in backend[1]
    assert str(tmp_path / "tests/harness") in backend[1]
    assert backend[3]["env"]["PYTHONPATH"] == os.pathsep.join((str(tmp_path / "src"), str(tmp_path)))
    frontend = next(call for call in calls if call[0] == "frontend-build")
    assert frontend[1] == [str(tmp_path / "scripts/dev.sh"), "frontend", "build"]
    assert frontend[2] == tmp_path
    assert "--fail-under=80" in next(call for call in calls if call[0] == "harness-coverage")[1]
    assert len(output.splitlines()) <= 40


def test_verification_failure_and_no_checks(tmp_path, monkeypatch, capsys):
    def fail(name, command, cwd, log_dir, **kwargs):
        log = log_dir / (name + ".log")
        log.write_text("ModuleNotFoundError: missing dependency\n")
        return h.Result(name, 1, log)
    monkeypatch.setattr(h, "run_step", fail)
    assert h.verify(tmp_path, ("backend",)) == 1
    assert "FAIL" in capsys.readouterr().out
    assert h.verify(tmp_path, ()) == 0
    assert "SKIP" in capsys.readouterr().out


def test_interruption_between_steps_is_not_success(tmp_path, monkeypatch, capsys):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(h, "run_step", interrupt)
    assert h.verify(tmp_path, ("backend",)) == 1
    output = capsys.readouterr().out
    assert output.startswith("FAIL") and "130" in output
    results = next((tmp_path / ".local/harness/logs").glob("*/results.json"))
    assert json.loads(results.read_text())["results"][0]["exit_code"] == 130


def test_incomplete_coverage_report_is_recorded_as_failure(tmp_path, monkeypatch, capsys):
    def success_without_report(name, command, cwd, log_dir, **kwargs):
        log = log_dir / (name + ".log")
        log.write_text("")
        return h.Result(name, 0, log)
    monkeypatch.setattr(h, "run_step", success_without_report)
    assert h.verify(tmp_path, ("backend",)) == 1
    output = capsys.readouterr().out
    assert "FAIL" in output and "coverage" in output
    assert next((tmp_path / ".local/harness/logs").glob("*/results.json")).exists()


def test_cli_usage_and_errors(tmp_path, capsys):
    assert h.main(["usage", "record", "--id", "run-1", "--model", "gpt-6-astra", "--status", "aborted"], root=tmp_path) == 0
    assert (tmp_path / ".local/harness/usage.jsonl").exists()
    assert h.main(["usage", "summary"], root=tmp_path) == 0
    assert "未計測=1" in capsys.readouterr().out
    assert h.main(["usage", "settle", "--id", "run-1", "--credits", "2", "--source", "usage screen"], root=tmp_path) == 0
    assert h.main(["usage", "settle", "--id", "run-1", "--credits", "2", "--source", "usage screen"], root=tmp_path) == 2
    assert "ERROR" in capsys.readouterr().err


def test_cli_changed_initial_and_scope(tmp_path, monkeypatch):
    scopes = []
    monkeypatch.setattr(h, "verify", lambda root, selected: scopes.append(selected) or 0)
    monkeypatch.setattr(h, "changed_files", lambda root: None)
    assert h.main(["verify", "--changed"], root=tmp_path) == 0
    assert scopes[-1] == h.SCOPES
    assert h.main(["verify", "--scope", "frontend"], root=tmp_path) == 0
    assert scopes[-1] == ("frontend",)
    monkeypatch.setattr(h, "changed_files", lambda root: ["docs/TESTS.md"])
    assert h.main(["verify", "--changed"], root=tmp_path) == 0
    assert scopes[-1] == ("layout",)
    with pytest.raises(SystemExit):
        h.main(["verify", "--changed", "--scope", "backend"], root=tmp_path)


def test_layout_allows_declared_hidden_entries_and_rejects_unknown(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "layout.json").write_text((h.ROOT / "config/layout.json").read_text())
    (tmp_path / ".local").mkdir()
    (tmp_path / ".coverage.123").write_text("")
    assert h.validate_layout(tmp_path, require_all=False) == []
    (tmp_path / ".unknown-management").mkdir()
    assert any("unknown root entry" in error for error in h.validate_layout(tmp_path, require_all=False))


def test_layout_reports_kind_mismatch(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "layout.json").write_text((h.ROOT / "config/layout.json").read_text())
    (tmp_path / "AGENTS.md").mkdir()
    errors = h.validate_layout(tmp_path, require_all=False)
    assert any("kind mismatch: AGENTS.md" in error for error in errors)


def test_layout_generated_paths_are_ignored():
    assert h.validate_ignored_paths(h.ROOT) == []


def test_layout_checks_absent_generated_paths_and_tracked_secrets(tmp_path):
    git(tmp_path, "init")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/layout.json").write_text((h.ROOT / "config/layout.json").read_text())
    (tmp_path / ".gitignore").write_text(".local/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "layout")
    assert h.validate_ignored_paths(tmp_path) == []
    secret = tmp_path / ".local/secret.env"
    secret.parent.mkdir()
    secret.write_text("TOKEN=tracked\n")
    git(tmp_path, "add", "-f", str(secret.relative_to(tmp_path)))
    errors = h.validate_layout(tmp_path, require_all=False)
    assert any("generated path is tracked" in error for error in errors)


def test_layout_rejects_tracked_unknown_root_entry(tmp_path):
    git(tmp_path, "init")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/layout.json").write_text((h.ROOT / "config/layout.json").read_text())
    rogue = tmp_path / "rogue.txt"
    rogue.write_text("unexpected\n")
    git(tmp_path, "add", ".")
    errors = h.validate_layout(tmp_path, require_all=False)
    assert any("tracked root entry is outside contract" in error for error in errors)


def test_layout_migration_collision_is_failure_preserving(tmp_path):
    source = tmp_path / "package.json"
    destination = tmp_path / "config/frontend/package.json"
    destination.parent.mkdir(parents=True)
    source.write_text("source")
    destination.write_text("different")
    with pytest.raises(FileExistsError):
        ml.apply(tmp_path)
    assert source.read_text() == "source"
    assert destination.read_text() == "different"


def test_layout_migration_is_idempotent(tmp_path):
    (tmp_path / "package.json").write_text("same")
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker/python.Dockerfile").write_text("docker")
    first = ml.apply(tmp_path)
    second = ml.apply(tmp_path)
    assert not first["collisions"] and not second["collisions"]
    assert (tmp_path / "config/frontend/package.json").read_text() == "same"
    assert not (tmp_path / "package.json").exists()
    assert not (tmp_path / "docker").exists()


def test_layout_migration_rolls_back_when_source_unlink_fails(tmp_path, monkeypatch):
    first = tmp_path / "package.json"
    second = tmp_path / "requirements.txt"
    first.write_text("first")
    second.write_text("second")
    original_unlink = ml.Path.unlink

    def fail_on_second(path, *args, **kwargs):
        if path == second:
            raise OSError("injected unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(ml.Path, "unlink", fail_on_second)
    with pytest.raises(OSError, match="injected"):
        ml.apply(tmp_path)
    assert first.read_text() == "first"
    assert second.read_text() == "second"
    assert not (tmp_path / "config/frontend/package.json").exists()
    assert not (tmp_path / "config/backend/requirements.txt").exists()


def test_layout_check_fails_for_incomplete_migration_and_passes_when_complete(tmp_path, capsys):
    (tmp_path / "config/frontend").mkdir(parents=True)
    assert ml.main(["check", "--root", str(tmp_path)]) == 2
    capsys.readouterr()
    for source, destination in ml.move_paths(tmp_path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("migrated")
    assert ml.main(["check", "--root", str(tmp_path)]) == 0


def test_dev_entrypoint_works_from_path_with_spaces():
    with tempfile.TemporaryDirectory(prefix="tof logis ") as directory:
        result = subprocess.run([str(h.ROOT / "scripts/dev.sh"), "help"], cwd=directory,
                                capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "frontend dev|build|preview" in result.stdout


def test_migration_command_is_local_and_does_not_exec_container(monkeypatch):
    assert migrate_db.migration_command("check")[-1] == "check"
    assert migrate_db.migration_command("apply")[-2:] == ["upgrade", "head"]
    calls = []
    monkeypatch.setattr(migrate_db.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or type("R", (), {"returncode": 0})())
    assert migrate_db.main(["check"]) == 0
    assert calls and "compose" not in calls[0][0] and "exec" not in calls[0][0]


def test_frontend_verification_does_not_install_when_setup_is_absent(tmp_path):
    assert h.verify(tmp_path, ("frontend",)) == 1
    result = next((tmp_path / ".local/harness/logs").glob("*/frontend-build.log"))
    assert "setup required" in result.read_text()
    assert not (tmp_path / ".local/frontend").exists()


def test_frontend_build_output_contract_is_local():
    vite = (h.ROOT / "config/frontend/vite.config.ts").read_text()
    assert 'root: "source"' in vite
    assert '"include": ["source"]' in (h.ROOT / "config/frontend/tsconfig.json").read_text()
    if (h.ROOT / ".local/frontend/source").is_symlink():
        assert os.readlink(h.ROOT / ".local/frontend/source") == "../../src/frontend"
    assert "outDir: process.env.TOF_LOGIS_VITE_OUT_DIR ?? \"../dist\"" in vite
    assert (h.ROOT / ".local/frontend/dist/index.html").exists() or not (h.ROOT / ".local/frontend").exists()
