from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from suppsystem.config import get_settings
from suppsystem.database import Database
from suppsystem.media_storage import LocalMediaStorage
from suppsystem.web_models import MediaAsset


@dataclass(frozen=True)
class CleanupResult:
    temporary_files: int
    orphan_assets: int
    removed_files: int


def cleanup_media_files(
    storage: LocalMediaStorage,
    *,
    referenced_paths: set[str],
    apply: bool,
    now: datetime | None = None,
) -> CleanupResult:
    current = (now or datetime.now(UTC)).timestamp()
    temporary_cutoff = current - timedelta(hours=1).total_seconds()
    orphan_cutoff = current - timedelta(hours=24).total_seconds()
    temporary = (
        [
            path
            for path in storage.temp_root.glob("*")
            if path.is_file() and path.stat().st_mtime < temporary_cutoff
        ]
        if storage.temp_root.is_dir()
        else []
    )
    orphans = (
        [
            path
            for path in storage.asset_root.glob("**/*")
            if path.is_file()
            and path.relative_to(storage.data_dir).as_posix() not in referenced_paths
            and path.stat().st_mtime < orphan_cutoff
        ]
        if storage.asset_root.is_dir()
        else []
    )
    candidates = temporary + orphans
    if apply:
        for path in candidates:
            path.unlink(missing_ok=True)
    return CleanupResult(
        temporary_files=len(temporary),
        orphan_assets=len(orphans),
        removed_files=len(candidates) if apply else 0,
    )


async def run(*, apply: bool) -> CleanupResult:
    settings = get_settings()
    assert settings.database_url is not None
    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            referenced_paths = set((await session.scalars(select(MediaAsset.storage_path))).all())
        return cleanup_media_files(
            LocalMediaStorage(settings.data_dir),
            referenced_paths=referenced_paths,
            apply=apply,
        )
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find stale temporary and unreferenced Web media files."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete candidates. Without this flag the command is read-only.",
    )
    arguments = parser.parse_args()
    result = asyncio.run(run(apply=arguments.apply))
    mode = "removed" if arguments.apply else "found"
    print(
        f"{mode}: temporary={result.temporary_files}, "
        f"orphan_assets={result.orphan_assets}, removed={result.removed_files}"
    )


if __name__ == "__main__":
    main()
