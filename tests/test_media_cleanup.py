from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from resolvate.media_cleanup import cleanup_media_files
from resolvate.media_storage import LocalMediaStorage


def test_media_cleanup_is_dry_run_by_default_and_preserves_references(tmp_path: Path) -> None:
    storage = LocalMediaStorage(tmp_path)
    storage._prepare()
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    temporary = storage.temp_root / "stale.upload"
    temporary.write_bytes(b"temporary")
    orphan = storage.asset_root / "aa" / "orphan.png"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    linked = storage.asset_root / "bb" / "linked.png"
    linked.parent.mkdir(parents=True)
    linked.write_bytes(b"linked")
    for path in (temporary, orphan, linked):
        os.utime(path, (old, old))

    referenced = {linked.relative_to(tmp_path).as_posix()}
    preview = cleanup_media_files(storage, referenced_paths=referenced, apply=False)
    assert preview.temporary_files == 1
    assert preview.orphan_assets == 1
    assert preview.removed_files == 0
    assert temporary.exists() and orphan.exists() and linked.exists()

    applied = cleanup_media_files(storage, referenced_paths=referenced, apply=True)
    assert applied.removed_files == 2
    assert not temporary.exists() and not orphan.exists()
    assert linked.exists()
