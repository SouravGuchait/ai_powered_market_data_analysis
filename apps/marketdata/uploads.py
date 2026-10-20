"""Transport-level upload validation.

Extensions and MIME types are hints only. The first bytes of every upload are
inspected before a job is created, but the file is never fully parsed during
the request.
"""

import codecs
import csv
import re
from dataclasses import dataclass
from pathlib import Path

PROBE_BYTES = 64 * 1024
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")

REQUIRED_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
EXTENSION_TYPES = {"csv": "csv", "json": "json", "xlsx": "xlsx"}

ALLOWED_MIME_TYPES = {
    "csv": frozenset(
        {
            "text/csv",
            "application/csv",
            "application/x-csv",
            "text/plain",
            "application/vnd.ms-excel",
            "application/octet-stream",
        }
    ),
    "json": frozenset(
        {
            "application/json",
            "text/json",
            "text/plain",
            "application/octet-stream",
        }
    ),
    "xlsx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/zip",
            "application/octet-stream",
        }
    ),
}

FILENAME_BY_MIME = {
    "text/csv": "upload.csv",
    "application/csv": "upload.csv",
    "application/x-csv": "upload.csv",
    "application/json": "upload.json",
    "text/json": "upload.json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        "upload.xlsx"
    ),
}

UTF8_CHARSETS = frozenset({"utf-8", "utf8", "us-ascii", "ascii"})


class UploadError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class UploadMetadata:
    source_type: str
    extension: str
    original_filename: str
    content_type: str


def sanitize_filename(name: str | None) -> str:
    if not name:
        raise UploadError("file_required", "Attach a file to upload.")
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in cleaned if char.isprintable() and char != "\x00")
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        raise UploadError("file_required", "Attach a file to upload.")
    if len(cleaned) > 255:
        stem, dot, extension = cleaned.rpartition(".")
        if dot and 0 < len(extension) <= 16:
            cleaned = f"{stem[: 255 - len(extension) - 1]}.{extension}"
        else:
            cleaned = cleaned[:255]
    return cleaned


def resolve_upload(
    *, filename: str | None, content_type: str | None
) -> UploadMetadata:
    mime, parameters = _split_content_type(content_type)
    safe_name = sanitize_filename(filename) if filename else _filename_from_mime(mime)
    extension = Path(safe_name).suffix.lower().lstrip(".")
    source_type = EXTENSION_TYPES.get(extension)
    if source_type is None:
        raise UploadError(
            "unsupported_file_type",
            "Upload a CSV, JSON, or XLSX file.",
            status_code=415,
        )
    if mime and mime not in ALLOWED_MIME_TYPES[source_type]:
        raise UploadError(
            "unsupported_media_type",
            "The declared content type does not match a supported upload type.",
            status_code=415,
        )
    _validate_charset(source_type, parameters)
    return UploadMetadata(
        source_type=source_type,
        extension=extension,
        original_filename=safe_name,
        content_type=mime[:64],
    )


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip()
    if not key:
        return None
    if len(key) > 64 or IDEMPOTENCY_KEY_PATTERN.fullmatch(key) is None:
        raise UploadError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 1-64 characters using letters, digits, dot, "
            "underscore, colon, or hyphen.",
        )
    return key


def validate_probe(source_type: str, probe: bytes) -> None:
    if not probe:
        raise UploadError("empty_file", "The uploaded file is empty.")
    if source_type == "csv":
        _validate_csv_header(probe)
    elif source_type == "json":
        _validate_json_probe(probe)
    else:
        _validate_xlsx_probe(probe)


def _split_content_type(value: str | None) -> tuple[str, list[str]]:
    parts = [part.strip() for part in (value or "").split(";")]
    return parts[0].lower(), [part for part in parts[1:] if part]


def _filename_from_mime(mime: str) -> str:
    filename = FILENAME_BY_MIME.get(mime)
    if filename is None:
        raise UploadError(
            "filename_required",
            "Provide an X-Filename header for raw uploads of this content type.",
        )
    return filename


def _validate_charset(source_type: str, parameters: list[str]) -> None:
    if source_type not in {"csv", "json"}:
        return
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() != "charset":
            continue
        charset = value.strip().strip('"').lower()
        if charset and charset not in UTF8_CHARSETS:
            raise UploadError(
                "unsupported_charset",
                "CSV and JSON uploads must use UTF-8 encoding.",
                status_code=415,
            )


def _validate_csv_header(probe: bytes) -> None:
    if b"\n" not in probe and len(probe) >= PROBE_BYTES:
        raise UploadError(
            "invalid_file_content",
            "The CSV header is too long or is not terminated by a newline.",
        )
    line = probe.split(b"\n", 1)[0].rstrip(b"\r")
    try:
        text = line.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError(
            "unsupported_encoding", "CSV uploads must use UTF-8 encoding."
        ) from None
    if not text.strip():
        raise UploadError("empty_file", "The uploaded file is empty.")
    try:
        header = next(csv.reader([text]))
    except csv.Error:
        raise UploadError(
            "invalid_file_content", "The CSV header could not be parsed."
        ) from None
    columns = [column.strip().lower() for column in header]
    if not columns or any(not column for column in columns):
        raise UploadError("invalid_file_content", "The CSV header is malformed.")
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        raise UploadError(
            "missing_columns",
            f"CSV header is missing required columns: {', '.join(missing)}.",
        )


def _validate_json_probe(probe: bytes) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(probe, final=False)
    except UnicodeDecodeError:
        raise UploadError(
            "unsupported_encoding", "JSON uploads must use UTF-8 encoding."
        ) from None
    stripped = probe.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped:
        raise UploadError("empty_file", "The uploaded file is empty.")
    if stripped[:1] not in {b"[", b"{"}:
        raise UploadError(
            "invalid_file_content",
            "The uploaded file is not a JSON array or object.",
        )


def _validate_xlsx_probe(probe: bytes) -> None:
    if not probe.startswith(b"PK\x03\x04"):
        raise UploadError(
            "invalid_file_content",
            "The uploaded file is not an XLSX workbook.",
        )
