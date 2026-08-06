from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from resolvate.config import get_settings

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024 * 1024


def export_media(data_dir: Path) -> None:
    root = data_dir / "web-media"
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                continue
            archive.add(path, arcname=path.relative_to(data_dir), recursive=False)


def _safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "web-media"
        or ".." in path.parts
        or not (member.isdir() or member.isfile())
    ):
        raise ValueError("media archive contains an unsafe entry")
    return path


def import_media(data_dir: Path, *, apply: bool) -> int:
    temporary_root: Path | None = None
    total_size = 0
    if apply:
        data_dir.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix=".web-media-restore-", dir=data_dir))
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
            for member in archive:
                path = _safe_member(member)
                total_size += member.size
                if total_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("media archive exceeds the safety limit")
                if not apply:
                    continue
                assert temporary_root is not None
                destination = temporary_root.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("media archive file cannot be read")
                with source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)
        if apply:
            assert temporary_root is not None
            restored = temporary_root / "web-media"
            restored.mkdir(parents=True, exist_ok=True)
            current = data_dir / "web-media"
            previous = data_dir / ".web-media-previous"
            if previous.exists():
                shutil.rmtree(previous)
            if current.exists():
                current.replace(previous)
            try:
                restored.replace(current)
            except Exception:
                if previous.exists() and not current.exists():
                    previous.replace(current)
                raise
            if previous.exists():
                shutil.rmtree(previous)
        return total_size
    finally:
        if temporary_root is not None and temporary_root.exists():
            shutil.rmtree(temporary_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export, validate, or restore Web media archive.")
    parser.add_argument("operation", choices=("export", "validate", "restore"))
    arguments = parser.parse_args()
    settings = get_settings()
    if arguments.operation == "export":
        export_media(settings.data_dir)
        return
    size = import_media(settings.data_dir, apply=arguments.operation == "restore")
    print(f"Validated Web media archive: {size} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
