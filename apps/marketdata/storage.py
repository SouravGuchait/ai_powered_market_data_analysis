"""Upload storage adapter.

Files are written through Django's storage API, so development uses the
configured local media backend and a deployment can select any storage backend
that implements the same API, including an S3 backend. Storage keys are
generated server-side and never contain user-provided paths.
"""

import hashlib
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from django.core.files.storage import default_storage
from django.core.files.storage.base import Storage

logger = logging.getLogger(__name__)

STORAGE_PREFIX = "uploads"
READ_CHUNK_BYTES = 64 * 1024
ALLOWED_EXTENSIONS = frozenset({"csv", "json", "xlsx"})


class UploadTooLarge(Exception):
    """Raised when the request body exceeds the configured upload limit."""


class StorageWriteError(Exception):
    """Raised when the storage backend cannot persist an upload."""


@dataclass(frozen=True)
class StoredUpload:
    storage_key: str
    checksum: str
    size_bytes: int


class UploadBody:
    """File-like adapter that bounds, hashes, and counts streamed bytes."""

    def __init__(self, chunks: Iterable[bytes], max_bytes: int) -> None:
        self._chunks = iter(chunks)
        self._max_bytes = max_bytes
        self._buffer = b""
        self._eof = False
        self.size = 0
        self.digest = hashlib.sha256()

    @property
    def name(self) -> str:
        return "upload"

    def _next_chunk(self) -> bytes:
        for chunk in self._chunks:
            if not chunk:
                continue
            self.size += len(chunk)
            if self.size > self._max_bytes:
                raise UploadTooLarge("Upload exceeds the configured size limit.")
            self.digest.update(chunk)
            return chunk
        self._eof = True
        return b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._buffer]
            self._buffer = b""
            while not self._eof:
                chunk = self._next_chunk()
                if chunk:
                    parts.append(chunk)
            return b"".join(parts)
        while len(self._buffer) < size and not self._eof:
            chunk = self._next_chunk()
            if chunk:
                self._buffer += chunk
        data = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return data

    def chunks(self, chunk_size: int = READ_CHUNK_BYTES) -> Iterable[bytes]:
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        """Storage backends may close their input; the source owns the handle."""


def get_upload_storage() -> Storage:
    return default_storage


def build_storage_key(owner_id: int, job_id: uuid.UUID, extension: str) -> str:
    normalized = extension.lower().lstrip(".")
    if normalized not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported upload extension.")
    return f"{STORAGE_PREFIX}/{owner_id}/{job_id}/{uuid.uuid4().hex}.{normalized}"


def save_upload(
    chunks: Iterable[bytes],
    *,
    owner_id: int,
    job_id: uuid.UUID,
    extension: str,
    max_bytes: int,
    storage: Storage | None = None,
) -> StoredUpload:
    backend = storage or get_upload_storage()
    key = build_storage_key(owner_id, job_id, extension)
    body = UploadBody(chunks, max_bytes)
    try:
        saved_key = backend.save(key, body)
    except UploadTooLarge:
        _delete_quietly(backend, key)
        raise
    except Exception as exc:
        _delete_quietly(backend, key)
        logger.exception("upload.storage.failed job_id=%s", job_id)
        raise StorageWriteError("The uploaded file could not be stored.") from exc
    return StoredUpload(
        storage_key=saved_key,
        checksum=body.digest.hexdigest(),
        size_bytes=body.size,
    )


def delete_stored_upload(storage_key: str, storage: Storage | None = None) -> None:
    backend = storage or get_upload_storage()
    _delete_quietly(backend, storage_key)


def _delete_quietly(storage: Storage, storage_key: str) -> None:
    try:
        storage.delete(storage_key)
    except Exception:  # noqa: BLE001 - cleanup must not mask the original error
        logger.warning("upload.storage.delete_failed")
