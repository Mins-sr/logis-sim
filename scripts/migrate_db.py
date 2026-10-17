#!/usr/bin/env python3
"""Collision-safe local database migration helper.

This command executes Alembic in the host process. It never calls
``docker compose exec`` and therefore never applies a migration inside a
running container.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ALEMBIC = ROOT / "config/backend/alembic.ini"


def migration_command(action: str, root: Path = ROOT) -> list[str]:
    if action not in {"check", "apply"}:
        raise ValueError("migration action must be check or apply")
    command = [sys.executable, "-m", "alembic", "-c", str(root / "config/backend/alembic.ini")]
    command.append("check" if action == "check" else "upgrade")
    if action == "apply":
        command.append("head")
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply"))
    args = parser.parse_args(argv)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT), os.environ.get("PYTHONPATH", ""))),
    }
    try:
        return subprocess.run(migration_command(args.action), cwd=ROOT, env=env, check=False).returncode
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
