from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

from suppsystem.media_archive import import_media


class BinaryInput:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


def archive(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return output.getvalue()


def test_media_archive_restore_replaces_media_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "web-media" / "assets" / "old.jpg"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"old")
    payload = archive({"web-media/assets/aa/new.png": b"new"})
    monkeypatch.setattr(sys, "stdin", BinaryInput(payload))

    size = import_media(tmp_path, apply=True)

    assert size == 3
    assert not current.exists()
    assert (tmp_path / "web-media" / "assets" / "aa" / "new.png").read_bytes() == b"new"


def test_media_archive_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", BinaryInput(archive({"../outside": b"unsafe"})))
    with pytest.raises(ValueError, match="unsafe"):
        import_media(tmp_path, apply=True)
    assert not (tmp_path.parent / "outside").exists()
