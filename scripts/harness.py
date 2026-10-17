"""Local verification and manually measured credit ledger; never invokes a model."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("backend", "frontend", "harness")
BACKEND = ("src/app", "src/shared", "src/worker", "src/optimizer")
MODELS = ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra")
JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Result:
    name: str
    code: int
    log: Path


def select_scopes(paths: list[str]) -> tuple[str, ...]:
    selected = set()
    for path in paths:
        if path.startswith(("scripts/", "tests/harness/")):
            selected.add("harness")
        elif path.startswith(tuple(part + "/" for part in (*BACKEND, "src/db", "tests"))):
            selected.add("backend")
        elif path.startswith("src/frontend/"):
            selected.add("frontend")
        elif ("/" not in path and path.startswith("package") and path.endswith(".json")) or path in ("vite.config.ts", "tsconfig.json"):
            selected.add("frontend")
        elif path.startswith("docs/"):
            continue
        else:
            return SCOPES
    return tuple(scope for scope in SCOPES if scope in selected)


def changed_files(root: Path) -> list[str] | None:
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    if git("rev-parse", "--show-toplevel").returncode:
        raise ValueError("Git未初期化です。全体検証には引数なしの verify.sh を使用してください")
    if git("rev-parse", "--verify", "HEAD").returncode:
        return None
    commands = (
        ("diff", "--name-only", "--no-renames", "-z", "--"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    )
    paths = set()
    for command in commands:
        result = git(*command)
        if result.returncode:
            raise ValueError("Git差分取得に失敗しました")
        paths.update(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)
    return sorted(paths)


def run_step(name: str, command: list[str], cwd: Path, log_dir: Path,
             *, env: dict[str, str] | None = None, timeout: float = 600) -> Result:
    log = log_dir / f"{name}.log"
    with log.open("w", encoding="utf-8") as output:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            output.write(f"ERROR dependency/launch: {error}\n")
            return Result(name, 127, log)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            # Kill the entire build/test process group, including npm's child processes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            code = 130 if isinstance(error, KeyboardInterrupt) else 124
            output.write(f"ERROR {'interrupted' if code == 130 else 'timeout'}: exit={code}\n")
    return Result(name, code, log)


def summarize(results: list[Result], warnings: list[str], log_dir: Path) -> str:
    lines = ["FAIL" if any(result.code for result in results) else "PASS"]
    for result in results:
        lines.append(f"{result.name}: exit={result.code} log={result.log}")
        if result.code:
            matches = []
            with result.log.open(errors="replace") as source:
                for line in source:
                    if re.search(r"FAILED |ERROR|Error|\.py:\d+|error TS|:[0-9]+:[0-9]+:", line):
                        matches.append(line.strip()[:240])
                        matches = matches[-2:]
            lines.extend(matches or ["詳細は工程ログを参照してください"])
    lines.extend(warnings)
    # Reserve the last line for the full machine-readable result location.
    return "\n".join([*lines[:39], f"results={log_dir / 'results.json'}"])


def verify(root: Path, scopes: tuple[str, ...]) -> int:
    if not scopes:
        print("SKIP: 検証対象のコード変更なし（文書内容は差分レビューで確認）")
        return 0
    base = root / ".harness/logs"
    base.mkdir(parents=True, exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix="verify-", dir=base))
    results: list[Result] = []
    warnings: list[str] = []
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
           "PYTHONDONTWRITEBYTECODE": "1"}
    with tempfile.TemporaryDirectory(prefix="tof-verify-") as directory:
        scratch = Path(directory)

        def step(name, command, cwd=scratch):
            result = run_step(name, command, cwd, logs, env=env)
            results.append(result)
            if result.code == 130:
                raise KeyboardInterrupt
            return result.code == 0

        def coverage(scope, sources, tests, extra=()):
            data = str(logs / f".{scope}-coverage")
            config = f"--rcfile={root / 'pyproject.toml'}"
            step(f"{scope}-tests", [sys.executable, "-m", "coverage", "run", config,
                 f"--data-file={data}", "--source=" + ",".join(str(root / source) for source in sources),
                 "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
                 f"--junitxml={logs / (scope + '-tests.xml')}", str(root / tests), *extra])
            report = logs / f"{scope}-coverage.json"
            ok = step(f"{scope}-coverage", [sys.executable, "-m", "coverage", "json", config,
                      f"--data-file={data}", f"--fail-under={80 if scope == 'harness' else 0}",
                      "-o", str(report)])
            if report.exists():
                percent = float(json.loads(report.read_text())["totals"]["percent_covered"])
                if not 0 <= percent <= 100:
                    raise ValueError(f"カバレッジ値が不正です: {report}")
                warnings.append(f"{'WARN' if percent < 80 else 'INFO'} {scope} coverage={percent:.1f}%"
                                + (" / アプリ80%未達は別作業" if scope == "backend" and percent < 80 else ""))
            elif ok:
                raise ValueError(f"カバレッジ結果がありません: {report}")

        try:
            if "backend" in scopes:
                targets = [str(root / part) for part in (*BACKEND, "src/db")]
                step("backend-syntax", [sys.executable, "-m", "compileall", "-q", *targets])
                step("backend-lint", [sys.executable, "-m", "ruff", "check", "--select", "E9,F63,F7,F82", *targets])
                coverage("backend", BACKEND, "tests", ("--ignore", str(root / "tests/harness")))
            if "frontend" in scopes:
                step("frontend-build", ["npm", "run", "build"], root)
                warnings.append("WARN frontend: 単体テストは未整備（buildは型検査とビルドのみ）")
            if "harness" in scopes:
                step("harness-lint", [sys.executable, "-m", "ruff", "check", str(root / "scripts"), str(root / "tests/harness")])
                step("harness-shell", ["bash", "-n", str(root / "scripts/verify.sh")])
                coverage("harness", ("scripts",), "tests/harness")
        except KeyboardInterrupt:
            if not results or results[-1].code != 130:
                log = logs / "interrupted.log"
                log.write_text("ERROR interrupted: exit=130\n")
                results.append(Result("interrupted", 130, log))
            warnings.append("WARN: 検証を中断しました。残りの工程は未実行です")
        except (ValueError, TypeError, KeyError, OSError) as error:
            log = logs / "verification-error.log"
            log.write_text(f"ERROR verification: {error}\n")
            results.append(Result("verification-error", 2, log))
    payload = {"scopes": scopes, "results": [dict(name=r.name, exit_code=r.code, log=str(r.log)) for r in results],
               "warnings": warnings}
    (logs / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(summarize(results, warnings, logs))
    return 1 if any(result.code for result in results) else 0


def credit_value(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("クレジットは有限の非負数で指定してください") from error
    if not amount.is_finite() or not 0 <= amount <= Decimal("1000000000"):
        raise ValueError("クレジットは0〜1,000,000,000の有限数で指定してください")
    return amount


def validated_record(task, model, status, credits, source, at) -> dict:
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", task):
        raise ValueError("タスクIDは100文字以内の英数字・ハイフン・ピリオド・下線で指定してください")
    if model not in MODELS or status not in ("success", "failed", "aborted"):
        raise ValueError("モデルまたは結果が不正です")
    if not isinstance(source, str) or not source.strip() or len(source) > 240 or any(ord(c) < 32 for c in source):
        raise ValueError("測定元は240文字以内の空でない1行で指定してください")
    try:
        instant = datetime.fromisoformat(at)
    except (ValueError, TypeError) as error:
        raise ValueError("日時はタイムゾーン付きISO 8601で指定してください") from error
    if instant.tzinfo is None:
        raise ValueError("日時にはタイムゾーンが必要です")
    return {"task": task, "model": model, "status": status,
            "credits": None if credits is None else str(credit_value(credits)),
            "source": source.strip(), "at": instant.astimezone(JST).isoformat()}


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = [validated_record(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    except (TypeError, ValueError) as error:
        raise ValueError(f"利用記録が不正です: {path}: {error}") from error
    if len({row["task"] for row in rows}) != len(rows):
        raise ValueError("利用記録にタスクIDの重複があります")
    return rows


@contextmanager
def ledger_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_ledger(path: Path, rows: list[dict]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_usage(path: Path, task: str, model: str, status: str, credits: str | None, source: str, at: str) -> None:
    row = validated_record(task, model, status, credits, source, at)
    with ledger_lock(path):
        rows = read_ledger(path)
        if any(existing["task"] == task for existing in rows):
            raise ValueError("タスクIDが重複しています。再実行には別IDを指定してください")
        write_ledger(path, [*rows, row])


def settle_usage(path: Path, task: str, credits: str, source: str) -> None:
    with ledger_lock(path):
        rows = read_ledger(path)
        existing = next((row for row in rows if row["task"] == task), None)
        if existing is None or existing["credits"] is not None:
            raise ValueError("未計測のタスクIDを指定してください（記録済み実測値は上書きしません）")
        updated = validated_record(**{**existing, "credits": credits, "source": source})
        write_ledger(path, [updated if row["task"] == task else row for row in rows])


def usage_summary(path: Path, month: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("月はYYYY-MMで指定してください")
    datetime.strptime(month, "%Y-%m")
    rows = [row for row in read_ledger(path) if row["at"].startswith(month)]
    total = sum((credit_value(row["credits"]) for row in rows if row["credits"] is not None), Decimal(0))
    unknown = sum(row["credits"] is None for row in rows)
    level = "STOP" if total >= 3000 else "WARN" if total >= 2400 or unknown else "OK"
    note = " / 新規作業を停止してください" if total >= 3000 else ""
    if unknown:
        note += " / 未計測を含むため残額は暫定値です"
    return (f"{level} {month} JST: 実測合計={total} 残額={max(Decimal(0), Decimal(3000) - total)} "
            f"/ 予算=3000 未計測={unknown} 記録数={len(rows)}{note}")


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify_parser = commands.add_parser("verify")
    target = verify_parser.add_mutually_exclusive_group()
    target.add_argument("--scope", choices=SCOPES)
    target.add_argument("--changed", action="store_true")
    usage = commands.add_parser("usage").add_subparsers(dest="action", required=True)
    record = usage.add_parser("record")
    record.add_argument("--id", required=True)
    record.add_argument("--model", required=True, choices=MODELS)
    record.add_argument("--status", required=True, choices=("success", "failed", "aborted"))
    record.add_argument("--credits")
    record.add_argument("--source")
    record.add_argument("--at", default=datetime.now(JST).isoformat())
    settle = usage.add_parser("settle")
    settle.add_argument("--id", required=True)
    settle.add_argument("--credits", required=True)
    settle.add_argument("--source", required=True)
    usage.add_parser("summary").add_argument("--month", default=datetime.now(JST).strftime("%Y-%m"))
    args = parser.parse_args(argv)
    ledger = root / ".harness/usage.jsonl"
    try:
        if args.command == "verify":
            paths = changed_files(root) if args.changed else None
            scopes = select_scopes(paths) if paths is not None else ((args.scope,) if args.scope else SCOPES)
            return verify(root, scopes)
        if args.action == "record":
            source = args.source or ("未計測" if args.credits is None else "")
            record_usage(ledger, args.id, args.model, args.status, args.credits, source, args.at)
            print(f"RECORDED {args.id}: {ledger}")
        elif args.action == "settle":
            settle_usage(ledger, args.id, args.credits, args.source)
            print(f"SETTLED {args.id}: {ledger}")
        else:
            print(usage_summary(ledger, args.month))
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {str(error)[:500]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
