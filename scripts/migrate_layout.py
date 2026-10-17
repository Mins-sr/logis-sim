#!/usr/bin/env python3
"""Move legacy root configuration into the managed ``config`` layout.

The helper is deliberately separate from database migrations.  It only moves
repository files and never invokes Docker, Alembic, or a running service.  A
complete preflight is performed before any file is changed, so a destination
collision leaves the source tree untouched.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import filecmp
import json
from pathlib import Path
import shutil
import sys
import tempfile
import os


@dataclass(frozen=True)
class Move:
    source: str
    destination: str


MOVES = (
    Move("package.json", "config/frontend/package.json"),
    Move("package-lock.json", "config/frontend/package-lock.json"),
    Move("vite.config.ts", "config/frontend/vite.config.ts"),
    Move("tsconfig.json", "config/frontend/tsconfig.json"),
    Move("pyproject.toml", "config/backend/pyproject.toml"),
    Move("requirements.txt", "config/backend/requirements.txt"),
    Move("requirements-dev.txt", "config/backend/requirements-dev.txt"),
    Move("alembic.ini", "config/backend/alembic.ini"),
    Move("compose.yaml", "config/docker/compose.yaml"),
    Move("compose.dev.yaml", "config/docker/compose.dev.yaml"),
    Move("docker/python.Dockerfile", "config/docker/python.Dockerfile"),
    Move("docker/proxy.Dockerfile", "config/docker/proxy.Dockerfile"),
    Move(".env.example", "config/env/.env.example"),
)


def move_paths(root: Path) -> tuple[tuple[Path, Path], ...]:
    return tuple((root / move.source, root / move.destination) for move in MOVES)


def _same_file(left: Path, right: Path) -> bool:
    return left.is_file() and right.is_file() and filecmp.cmp(left, right, shallow=False)


def plan(root: Path) -> dict[str, list[str]]:
    """Return missing sources, already migrated files, and collisions."""
    missing: list[str] = []
    migrated: list[str] = []
    collisions: list[str] = []
    for source, destination in move_paths(root):
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        label = f"{source.relative_to(root)} -> {destination.relative_to(root)}"
        if source_exists and destination_exists:
            if _same_file(source, destination):
                migrated.append(label)
            else:
                collisions.append(label)
        elif source_exists:
            missing.append(label)
        elif destination_exists:
            migrated.append(label)
        else:
            missing.append(label)
    return {"missing": missing, "migrated": migrated, "collisions": collisions}


def _preflight(root: Path) -> tuple[tuple[Path, Path], ...]:
    pairs = move_paths(root)
    collisions = plan(root)["collisions"]
    if collisions:
        raise FileExistsError("destination collision: " + ", ".join(collisions))
    for source, destination in pairs:
        if source.exists() and not source.is_file() and not source.is_symlink():
            raise IsADirectoryError(f"source is not a file: {source}")
        if destination.exists() and not destination.is_file() and not destination.is_symlink():
            raise IsADirectoryError(f"destination is not a file: {destination}")
    return pairs


def apply(root: Path) -> dict[str, list[str]]:
    """Apply the migration without overwriting a different destination.

    Files are first copied to a temporary sibling for each destination.  Only
    after every copy succeeds are the source files removed, preserving the
    source tree if a copy or preflight fails.
    """
    pairs = _preflight(root)
    pending = [(source, destination) for source, destination in pairs if source.exists()]
    if not pending:
        return plan(root)

    # Keep an independent backup of every source and destination.  A failure
    # during the source unlink phase must restore both sides, including files
    # whose unlink already succeeded before the injected/real failure.
    temporary_root = Path(tempfile.mkdtemp(prefix=".layout-migrate-", dir=root))
    source_backups: dict[Path, Path] = {}
    destination_backups: dict[Path, Path] = {}
    created_parents: list[Path] = []
    try:
        for source, destination in pending:
            source_backup = temporary_root / "sources" / source.relative_to(root)
            source_backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, source_backup)
            source_backups[source] = source_backup
            if destination.exists() or destination.is_symlink():
                destination_backup = temporary_root / "destinations" / destination.relative_to(root)
                destination_backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, destination_backup)
                destination_backups[destination] = destination_backup
            if not destination.parent.exists():
                created_parents.append(destination.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
        for source, destination in pending:
            shutil.copy2(source, destination)
        for source, _destination in pending:
            source.unlink()
        for directory in (root / "docker",):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
    except Exception:
        # Restore sources first so a partially removed legacy tree is never
        # left behind.  os.unlink/rmtree avoid re-triggering a Path.unlink
        # failure injected by a caller while testing rollback.
        for source, backup in source_backups.items():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, source)
        for source, destination in pending:
            backup = destination_backups.get(destination)
            if backup is not None:
                shutil.copy2(backup, destination)
            elif destination.exists() or destination.is_symlink():
                os.unlink(destination)
        for parent in sorted(created_parents, key=lambda path: len(path.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    return plan(root)


def _print_plan(result: dict[str, list[str]]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        result = plan(root) if args.action == "check" else apply(root)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    _print_plan(result)
    if args.action == "check" and (result["missing"] or result["collisions"]):
        return 2
    if result["collisions"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
