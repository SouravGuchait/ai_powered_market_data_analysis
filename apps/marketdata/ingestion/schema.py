"""Shared ingestion vocabulary: columns, issue codes, and row shapes.

`PriceRecord` stores six decimal places, so a price may have at most twelve
integer digits. All comparisons and rounding use `Decimal`, never floats.
"""

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

REQUIRED_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")

PRICE_QUANTUM = Decimal("0.000001")
MAX_PRICE = Decimal(1000000000000)  # 12 integer digits for DecimalField(18, 6)
MAX_VOLUME = 2**63 - 1

MAX_SYMBOL_LENGTH = 32
MAX_RAW_ROW_FIELDS = 12
MAX_RAW_VALUE_LENGTH = 64
MAX_ISSUE_MESSAGE_LENGTH = 255

# The default exchange is created on demand from these settings when a file
# does not carry an `exchange` column.
DEFAULT_ASSET_CLASS = "equity"
DEFAULT_CURRENCY = "USD"


class IssueCode:
    """Stable codes stored in `ValidationIssue.error_code`."""

    MISSING_SYMBOL = "missing_symbol"
    INVALID_SYMBOL = "invalid_symbol"
    INVALID_TIMESTAMP = "invalid_timestamp"
    MISSING_PRICE = "missing_price"
    NON_NUMERIC_PRICE = "non_numeric_price"
    NON_FINITE_PRICE = "non_finite_price"
    NON_POSITIVE_PRICE = "non_positive_price"
    PRICE_OUT_OF_RANGE = "price_out_of_range"
    INVALID_OHLC_RANGE = "invalid_ohlc_range"
    PRICE_OUT_OF_BOUNDS = "price_out_of_bounds"
    INVALID_ADJUSTED_CLOSE = "invalid_adjusted_close"
    MISSING_VOLUME = "missing_volume"
    NON_NUMERIC_VOLUME = "non_numeric_volume"
    NON_FINITE_VOLUME = "non_finite_volume"
    NEGATIVE_VOLUME = "negative_volume"
    INVALID_VOLUME = "invalid_volume"
    VOLUME_OUT_OF_RANGE = "volume_out_of_range"
    UNKNOWN_EXCHANGE = "unknown_exchange"
    DUPLICATE_RECORD = "duplicate_record"
    MALFORMED_ROW = "malformed_row"

    # Job-level failures.
    EMPTY_FILE = "empty_file"
    INVALID_HEADER = "invalid_header"
    MISSING_COLUMNS = "missing_columns"
    INVALID_ENCODING = "invalid_encoding"
    MALFORMED_INPUT = "malformed_input"
    UNSUPPORTED_SOURCE = "unsupported_source"
    FILE_MISSING = "file_missing"
    STORAGE_ERROR = "storage_error"
    PROCESSING_ERROR = "processing_error"


@dataclass(frozen=True)
class RowIssue:
    """One safe, bounded data-quality issue for a single source row."""

    row_number: int | None
    error_code: str
    message: str
    field_name: str | None = None
    raw_row: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if len(self.error_code) > 64:
            raise ValueError("issue code is too long")
        if len(self.message) > MAX_ISSUE_MESSAGE_LENGTH:
            raise ValueError("issue message is too long")


@dataclass(frozen=True)
class NormalizedRecord:
    """One validated row, still missing its persisted instrument identity."""

    row_number: int
    symbol: str
    exchange_code: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal | None
    volume: int
    name: str
    asset_class: str
    currency: str

    @property
    def instrument_key(self) -> tuple[str, str]:
        return (self.exchange_code, self.symbol)


@dataclass(frozen=True)
class NormalizedChunk:
    records: list[NormalizedRecord]
    issues: list[RowIssue]
    received: int


class InputError(Exception):
    """A file-level failure that prevents any further row processing."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code[:32]
        self.summary = summary[:255]


def raw_row_preview(row: Any) -> dict[str, str]:
    """Bound a source row for `ValidationIssue.raw_row` (no full payloads)."""
    preview: dict[str, str] = {}
    for name in list(row.index)[:MAX_RAW_ROW_FIELDS]:
        value = row[name]
        text = "" if _is_missing(value) else str(value)
        preview[str(name)[:MAX_RAW_VALUE_LENGTH]] = text[:MAX_RAW_VALUE_LENGTH]
    return preview


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return type(value).__name__ in {"NAType", "NaTType"}  # pandas NA / NaT
