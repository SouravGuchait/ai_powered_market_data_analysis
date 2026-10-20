"""Bounded readers for CSV, JSON, and XLSX inputs.

All readers yield `Chunk` objects of at most `chunksize` data records. They
never keep the whole file in memory except for a JSON root object, which is
documented as a limitation. Malformed records become `RowIssue` entries instead
of being dropped silently.

Row numbering is defined per format:
- CSV: physical line number of the record start; the header is line 1.
- JSON: 1-based record index in the array.
- XLSX: 1-based worksheet row number; the header is its own row.
"""

import csv
import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, TextIO, cast

import pandas as pd

from apps.marketdata.ingestion.schema import (
    REQUIRED_COLUMNS,
    InputError,
    IssueCode,
    RowIssue,
)

JSON_READ_BYTES = 64 * 1024


@dataclass(frozen=True)
class Chunk:
    frame: pd.DataFrame
    row_numbers: list[int]
    issues: list[RowIssue]


def normalize_header(header: Iterable[Any]) -> list[str]:
    """Lowercase and strip header cells; reject blanks and duplicates."""
    columns: list[str] = []
    seen: set[str] = set()
    for cell in header:
        if cell is None:
            raise InputError(
                IssueCode.INVALID_HEADER, "The header contains an empty column."
            )
        name = str(cell).strip().lower()
        if not name:
            raise InputError(
                IssueCode.INVALID_HEADER, "The header contains an empty column."
            )
        if name in seen:
            raise InputError(
                IssueCode.INVALID_HEADER, f"The header repeats the column {name}."
            )
        seen.add(name)
        columns.append(name)
    if not columns:
        raise InputError(IssueCode.INVALID_HEADER, "The file has no header.")
    return columns


def validate_header(columns: list[str]) -> None:
    """Require the documented OHLCV columns; extra columns are ignored."""
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        raise InputError(
            IssueCode.MISSING_COLUMNS,
            f"The header is missing required columns: {', '.join(missing)}.",
        )


def iter_input_chunks(
    file_obj: BinaryIO | TextIO, source_type: str, chunksize: int
) -> Iterator[Chunk]:
    """Dispatch to the reader for a job's source type."""
    if chunksize < 1:
        raise ValueError("chunksize must be positive")
    readers = {
        "csv": iter_csv_chunks,
        "json": iter_json_chunks,
        "xlsx": iter_xlsx_chunks,
    }
    reader = readers.get(source_type)
    if reader is None:
        raise InputError(
            IssueCode.UNSUPPORTED_SOURCE,
            f"Unsupported source type: {source_type}.",
        )
    yield from reader(file_obj, chunksize)


def iter_csv_chunks(
    file_obj: BinaryIO | TextIO, chunksize: int = 5_000
) -> Iterator[Chunk]:
    """Read CSV records in bounded batches with accurate line numbers.

    The stdlib reader is used instead of `pandas.read_csv(chunksize=...)`
    because pandas cannot report the line number of a malformed record: it
    either raises on the first bad line or skips it silently. pandas is still
    used to build the per-chunk frame that carries the vectorized validation.
    """
    stream = _text_stream(file_obj)
    reader = csv.reader(stream)
    try:
        header = next(reader)
    except StopIteration:
        raise InputError(IssueCode.EMPTY_FILE, "The file is empty.") from None
    except (UnicodeDecodeError, csv.Error) as exc:
        raise InputError(
            IssueCode.INVALID_ENCODING, "The CSV file could not be decoded as UTF-8."
        ) from exc

    columns = normalize_header(header)
    validate_header(columns)
    width = len(columns)

    rows: list[dict[str, Any]] = []
    numbers: list[int] = []
    issues: list[RowIssue] = []
    produced = False
    previous_line = reader.line_num
    try:
        for record in reader:
            start_line = previous_line + 1
            previous_line = reader.line_num
            if not record or all(not field.strip() for field in record):
                continue
            if len(record) != width:
                issues.append(
                    RowIssue(
                        row_number=start_line,
                        error_code=IssueCode.MALFORMED_ROW,
                        message=(
                            f"Expected {width} fields but found {len(record)}."
                        ),
                    )
                )
                continue
            rows.append(dict(zip(columns, record)))
            numbers.append(start_line)
            if len(rows) >= chunksize:
                yield _chunk(rows, numbers, issues, columns)
                rows, numbers, issues = [], [], []
                produced = True
    except (UnicodeDecodeError, csv.Error) as exc:
        raise InputError(
            IssueCode.INVALID_ENCODING, "The CSV file could not be decoded as UTF-8."
        ) from exc

    if rows or issues:
        yield _chunk(rows, numbers, issues, columns)
    elif not produced:
        raise InputError(IssueCode.EMPTY_FILE, "The CSV file has no data rows.")


def iter_json_chunks(
    file_obj: BinaryIO | TextIO, chunksize: int = 5_000
) -> Iterator[Chunk]:
    """Stream a JSON array of objects in bounded batches."""
    stream = _text_stream(file_obj)
    columns: list[str] | None = None
    rows: list[dict[str, Any]] = []
    numbers: list[int] = []
    issues: list[RowIssue] = []
    try:
        for index, value in enumerate(_iter_json_values(stream), start=1):
            if not isinstance(value, dict):
                issues.append(
                    RowIssue(
                        row_number=index,
                        error_code=IssueCode.MALFORMED_ROW,
                        message="JSON record is not an object.",
                    )
                )
                continue
            if columns is None:
                columns = normalize_header(value.keys())
                validate_header(columns)
            rows.append({column: value.get(column) for column in columns})
            numbers.append(index)
            if len(rows) >= chunksize:
                yield _chunk(rows, numbers, issues, columns)
                rows, numbers, issues = [], [], []
    except UnicodeDecodeError as exc:
        raise InputError(
            IssueCode.INVALID_ENCODING, "The JSON file could not be decoded as UTF-8."
        ) from exc

    if rows or issues:
        yield _chunk(rows, numbers, issues, columns or [])
    elif columns is None:
        raise InputError(IssueCode.EMPTY_FILE, "The JSON file has no records.")


def iter_xlsx_chunks(
    file_obj: BinaryIO | TextIO, chunksize: int = 5_000
) -> Iterator[Chunk]:
    """Read the active worksheet row by row; only one chunk is materialized."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - pinned dependency
        raise InputError(
            IssueCode.MALFORMED_INPUT, "XLSX support is not installed."
        ) from exc

    if isinstance(file_obj, io.TextIOBase):
        raise InputError(
            IssueCode.MALFORMED_INPUT, "An XLSX upload must be opened in binary mode."
        )
    try:
        workbook = openpyxl.load_workbook(
            cast(BinaryIO, file_obj), read_only=True, data_only=True
        )
    except Exception as exc:
        raise InputError(
            IssueCode.MALFORMED_INPUT, "The XLSX workbook could not be read."
        ) from exc

    try:
        sheet = workbook.active
        if sheet is None:
            raise InputError(
                IssueCode.EMPTY_FILE, "The workbook has no active worksheet."
            )
        columns: list[str] | None = None
        width = 0
        rows: list[dict[str, Any]] = []
        numbers: list[int] = []
        issues: list[RowIssue] = []
        produced = False
        for row_number, cells in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = list(cells)
            if not values or all(_blank(cell) for cell in values):
                continue
            while values and _blank(values[-1]):
                values.pop()
            if columns is None:
                columns = normalize_header(values)
                validate_header(columns)
                width = len(columns)
                continue
            if len(values) > width:
                issues.append(
                    RowIssue(
                        row_number=row_number,
                        error_code=IssueCode.MALFORMED_ROW,
                        message=(
                            f"Expected {width} columns but found {len(values)}."
                        ),
                    )
                )
                continue
            if len(values) < width:
                values.extend([None] * (width - len(values)))
            rows.append(dict(zip(columns, values)))
            numbers.append(row_number)
            if len(rows) >= chunksize:
                yield _chunk(rows, numbers, issues, columns)
                rows, numbers, issues = [], [], []
                produced = True
        if columns is None:
            raise InputError(IssueCode.EMPTY_FILE, "The workbook has no header row.")
        if rows or issues:
            yield _chunk(rows, numbers, issues, columns)
        elif not produced:
            raise InputError(
                IssueCode.EMPTY_FILE, "The workbook has no data rows."
            )
    finally:
        workbook.close()


def _chunk(
    rows: list[dict[str, Any]],
    numbers: list[int],
    issues: list[RowIssue],
    columns: list[str],
) -> Chunk:
    frame = pd.DataFrame(rows, columns=pd.Index(columns))
    return Chunk(frame=frame, row_numbers=numbers, issues=issues)


def _text_stream(file_obj: BinaryIO | TextIO) -> TextIO:
    if isinstance(file_obj, io.TextIOBase):
        return cast(TextIO, file_obj)
    # utf-8-sig accepts an optional BOM without leaking it into the header.
    return io.TextIOWrapper(
        cast(BinaryIO, file_obj), encoding="utf-8-sig", newline=""
    )


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _iter_json_values(stream: TextIO) -> Iterator[Any]:
    """Incrementally decode a top-level JSON array using `raw_decode`."""
    buffer = ""
    position = 0
    eof = False
    decoder = json.JSONDecoder()

    def fill() -> bool:
        nonlocal buffer, eof
        if eof:
            return False
        text = stream.read(JSON_READ_BYTES)
        if not text:
            eof = True
            return False
        buffer += text
        return True

    while True:
        while position < len(buffer) and buffer[position] in " \t\r\n":
            position += 1
        if position < len(buffer):
            break
        buffer, position = "", 0
        if not fill():
            raise InputError(IssueCode.EMPTY_FILE, "The file is empty.")

    root = buffer[position]
    if root == "{":
        document = buffer[position:] + (stream.read() or "")
        try:
            yield json.loads(document)
        except json.JSONDecodeError as exc:
            raise InputError(
                IssueCode.MALFORMED_INPUT, "The JSON document is malformed."
            ) from exc
        return
    if root != "[":
        raise InputError(
            IssueCode.MALFORMED_INPUT, "The JSON root must be an array or object."
        )

    position += 1
    while True:
        while True:
            while position < len(buffer) and buffer[position] in " \t\r\n,":
                position += 1
            if position < len(buffer):
                break
            buffer, position = "", 0
            if not fill():
                raise InputError(
                    IssueCode.MALFORMED_INPUT, "The JSON array is not terminated."
                )
        if buffer[position] == "]":
            position += 1
            _reject_trailing_content(buffer[position:], stream)
            return
        try:
            value, position = decoder.raw_decode(buffer, position)
        except json.JSONDecodeError:
            if position:
                buffer, position = buffer[position:], 0
            if not fill():
                raise InputError(
                    IssueCode.MALFORMED_INPUT, "The JSON input is malformed."
                ) from None
            continue
        yield value


def _reject_trailing_content(remainder: str, stream: TextIO) -> None:
    tail = remainder + (stream.read() or "")
    if tail.strip():
        raise InputError(
            IssueCode.MALFORMED_INPUT, "Unexpected content after the JSON array."
        )
