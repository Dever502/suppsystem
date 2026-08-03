from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from starlette.datastructures import UploadFile

MAX_WEB_PHOTO_BYTES = 10 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
ALLOWED_PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class TelegramDownloader(Protocol):
    async def download(self, file: str, destination: Path) -> object: ...


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class StoredMedia:
    id: str
    storage_path: str
    mime_type: str
    size_bytes: int
    sha256: str
    original_filename: str | None

    def message_metadata(self) -> dict[str, object]:
        return {
            "type": "photo",
            "media_id": self.id,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def _detected_mime(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


class LocalMediaStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.root = self.data_dir / "web-media"
        self.temp_root = self.root / "tmp"
        self.asset_root = self.root / "assets"

    def _prepare(self) -> None:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.asset_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _inspect(temp_path: Path) -> tuple[int, bytes, str]:
        size = temp_path.stat().st_size
        with temp_path.open("rb") as source:
            header = source.read(16)
            digest = hashlib.sha256(header)
            while chunk := source.read(UPLOAD_CHUNK_BYTES):
                digest.update(chunk)
        return size, header, digest.hexdigest()

    @staticmethod
    def _write_chunk(destination: BinaryIO, chunk: bytes) -> None:
        destination.write(chunk)

    def _finalize(
        self,
        *,
        temp_path: Path,
        media_id: str,
        declared_mime: str | None,
        original_filename: str | None,
        inspection: tuple[int, bytes, str] | None = None,
    ) -> StoredMedia:
        size, header, sha256 = self._inspect(temp_path) if inspection is None else inspection
        if size == 0:
            temp_path.unlink(missing_ok=True)
            raise MediaValidationError("photo must not be empty")
        if size > MAX_WEB_PHOTO_BYTES:
            temp_path.unlink(missing_ok=True)
            raise MediaValidationError("photo is too large")
        detected_mime = _detected_mime(header)
        if detected_mime is None or detected_mime not in ALLOWED_PHOTO_MIME_TYPES:
            temp_path.unlink(missing_ok=True)
            raise MediaValidationError("unsupported photo format")
        if declared_mime and declared_mime.casefold() not in {
            detected_mime,
            "application/octet-stream",
        }:
            temp_path.unlink(missing_ok=True)
            raise MediaValidationError("photo MIME type does not match its content")
        relative = (
            Path("web-media")
            / "assets"
            / media_id[:2]
            / (media_id + MIME_EXTENSIONS[detected_mime])
        )
        destination = self.data_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(destination)
        return StoredMedia(
            id=media_id,
            storage_path=relative.as_posix(),
            mime_type=detected_mime,
            size_bytes=size,
            sha256=sha256,
            original_filename=(Path(original_filename).name[:255] if original_filename else None),
        )

    async def save_upload(self, upload: UploadFile) -> StoredMedia:
        await asyncio.to_thread(self._prepare)
        media_id = str(uuid.uuid4())
        temp_path = self.temp_root / f"{media_id}.upload"
        size = 0
        header = bytearray()
        digest = hashlib.sha256()
        destination: BinaryIO | None = None
        try:
            destination = await asyncio.to_thread(temp_path.open, "xb")
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_WEB_PHOTO_BYTES:
                    raise MediaValidationError("photo is too large")
                if len(header) < 16:
                    header.extend(chunk[: 16 - len(header)])
                await asyncio.to_thread(self._write_chunk, destination, chunk)
                digest.update(chunk)
            await asyncio.to_thread(destination.close)
            return await asyncio.to_thread(
                self._finalize,
                temp_path=temp_path,
                media_id=media_id,
                declared_mime=upload.content_type,
                original_filename=upload.filename,
                inspection=(size, bytes(header), digest.hexdigest()),
            )
        except BaseException:
            if destination is not None:
                await asyncio.to_thread(destination.close)
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            raise
        finally:
            await upload.close()

    async def save_telegram_photo(self, bot: TelegramDownloader, *, file_id: str) -> StoredMedia:
        await asyncio.to_thread(self._prepare)
        media_id = str(uuid.uuid4())
        temp_path = self.temp_root / f"{media_id}.telegram"
        try:
            await bot.download(file_id, destination=temp_path)
            return await asyncio.to_thread(
                self._finalize,
                temp_path=temp_path,
                media_id=media_id,
                declared_mime="image/jpeg",
                original_filename=None,
            )
        except BaseException:
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            raise

    def resolve(self, storage_path: str) -> Path:
        candidate = (self.data_dir / storage_path).resolve()
        try:
            candidate.relative_to(self.asset_root.resolve())
        except ValueError as error:
            raise MediaValidationError("invalid media path") from error
        return candidate

    async def resolve_file(self, storage_path: str) -> Path | None:
        def resolve_existing_file() -> Path | None:
            path = self.resolve(storage_path)
            return path if path.is_file() else None

        return await asyncio.to_thread(resolve_existing_file)

    async def delete(self, media: StoredMedia) -> None:
        def delete_file() -> None:
            self.resolve(media.storage_path).unlink(missing_ok=True)

        await asyncio.to_thread(delete_file)
