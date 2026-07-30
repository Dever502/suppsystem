from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from sqlalchemy import make_url


def resolve_sqlite_database_path(
    database_url: str,
    *,
    data_dir: Path,
    working_directory: Path | None = None,
) -> Path:
    """Resolve the actual SQLite file and require it to live in the persistent data directory."""

    try:
        parsed = make_url(database_url)
    except Exception as error:
        raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from error
    if parsed.drivername not in {"sqlite", "sqlite+aiosqlite"}:
        raise ValueError("SQLite backup/restore requires a SQLite DATABASE_URL")
    if not parsed.database or parsed.database == ":memory:":
        raise ValueError("SQLite backup/restore requires a file-backed database")
    if parsed.query:
        raise ValueError("SQLite backup/restore does not support DATABASE_URL query parameters")

    cwd = Path.cwd() if working_directory is None else working_directory
    persistent_root = data_dir if data_dir.is_absolute() else cwd / data_dir
    raw_database_path = Path(parsed.database)
    database_path = (
        raw_database_path if raw_database_path.is_absolute() else cwd / raw_database_path
    )
    persistent_root = persistent_root.resolve()
    database_path = database_path.resolve()
    try:
        database_path.relative_to(persistent_root)
    except ValueError as error:
        raise ValueError("SQLite DATABASE_URL must point inside DATA_DIR") from error
    return database_path


def sqlite_integrity_check(path: Path) -> None:
    if not path.is_file():
        raise ValueError("SQLite database file does not exist")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = source.execute("PRAGMA integrity_check").fetchall()
    finally:
        source.close()
    if rows != [("ok",)]:
        raise ValueError("SQLite database failed integrity_check")


def create_sqlite_backup(source_path: Path, output_path: Path) -> None:
    sqlite_integrity_check(source_path)
    output_path.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(output_path)
        output_path.chmod(0o600)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    sqlite_integrity_check(output_path)


def restore_sqlite_backup(backup_path: Path, destination_path: Path) -> None:
    """Validate and atomically replace a stopped application's SQLite database."""

    sqlite_integrity_check(backup_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination_path.parent,
        prefix=f".{destination_path.name}.",
        suffix=".restore",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        create_sqlite_backup(backup_path, temporary_path)
        temporary_path.chmod(0o600)
        with temporary_path.open("rb") as restored:
            os.fsync(restored.fileno())
        for suffix in ("-wal", "-shm"):
            Path(f"{destination_path}{suffix}").unlink(missing_ok=True)
        os.replace(temporary_path, destination_path)
        directory_descriptor = os.open(destination_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def _configured_sqlite_path() -> Path:
    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    database_url = os.getenv("DATABASE_URL") or f"sqlite+aiosqlite:///{data_dir / 'support.db'}"
    return resolve_sqlite_database_path(database_url, data_dir=data_dir)


def _stdin_to_temporary_file() -> Path:
    descriptor, name = tempfile.mkstemp(dir="/tmp", suffix=".db")
    try:
        with os.fdopen(descriptor, "wb") as destination:
            shutil.copyfileobj(sys.stdin.buffer, destination)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe suppsystem data operations")
    parser.add_argument(
        "command",
        choices=("sqlite-backup", "sqlite-validate", "sqlite-restore", "sqlite-path"),
    )
    command = parser.parse_args().command
    database_path = _configured_sqlite_path()

    if command == "sqlite-path":
        print(database_path)
        return
    if command == "sqlite-backup":
        descriptor, name = tempfile.mkstemp(dir="/tmp", suffix=".db")
        os.close(descriptor)
        backup_path = Path(name)
        try:
            create_sqlite_backup(database_path, backup_path)
            with backup_path.open("rb") as backup:
                shutil.copyfileobj(backup, sys.stdout.buffer)
        finally:
            backup_path.unlink(missing_ok=True)
        return

    input_path = _stdin_to_temporary_file()
    try:
        if command == "sqlite-validate":
            sqlite_integrity_check(input_path)
        else:
            restore_sqlite_backup(input_path, database_path)
    finally:
        input_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
