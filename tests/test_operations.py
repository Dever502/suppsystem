from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from resolvate.operations import (
    create_sqlite_backup,
    resolve_sqlite_database_path,
    restore_sqlite_backup,
    sqlite_integrity_check,
)


def create_database(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE control (value TEXT NOT NULL)")
        connection.execute("INSERT INTO control (value) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def control_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM control").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def test_custom_sqlite_database_path_is_resolved_inside_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "persistent"
    expected = data_dir / "custom.db"

    actual = resolve_sqlite_database_path(
        "sqlite+aiosqlite:///./persistent/custom.db",
        data_dir=Path("./persistent"),
        working_directory=tmp_path,
    )

    assert actual == expected


def test_sqlite_database_outside_data_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside DATA_DIR"):
        resolve_sqlite_database_path(
            f"sqlite+aiosqlite:///{tmp_path / 'outside.db'}",
            data_dir=tmp_path / "persistent",
        )


def test_sqlite_backup_and_atomic_restore_preserve_control_data(tmp_path: Path) -> None:
    database_path = tmp_path / "support.db"
    backup_path = tmp_path / "support.backup.db"
    create_database(database_path, "before")
    create_sqlite_backup(database_path, backup_path)

    database_path.unlink()
    create_database(database_path, "after")
    Path(f"{database_path}-wal").write_bytes(b"stale wal")
    Path(f"{database_path}-shm").write_bytes(b"stale shm")
    restore_sqlite_backup(backup_path, database_path)

    assert control_value(database_path) == "before"
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()
    sqlite_integrity_check(database_path)


def test_corrupt_sqlite_backup_does_not_replace_destination(tmp_path: Path) -> None:
    database_path = tmp_path / "support.db"
    backup_path = tmp_path / "corrupt.db"
    create_database(database_path, "live")
    backup_path.write_bytes(b"not a sqlite database")

    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_sqlite_backup(backup_path, database_path)

    assert control_value(database_path) == "live"
