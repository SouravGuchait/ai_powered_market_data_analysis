"""Vectorized normalization from source rows to the canonical PriceRecord shape.

The module is intentionally ORM-free: it receives a DataFrame chunk, the
matching source row numbers, and the known exchange timezones. It returns
validated records plus one `RowIssue` per failed rule and never raises for a
bad row.
"""

import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from apps.marketdata.ingestion.schema import (
    DEFAULT_ASSET_CLASS,
    DEFAULT_CURRENCY,
    MAX_PRICE,
    MAX_SYMBOL_LENGTH,
    MAX_VOLUME,
    PRICE_QUANTUM,
    IssueCode,
    NormalizedChunk,
    NormalizedRecord,
    RowIssue,
    raw_row_preview,
)

ASSET_CLASSES = frozenset({"equity", "etf", "currency", "commodity", "crypto"})
_PRICE_FIELDS = ("open", "high", "low", "close")


def normalize_chunk(
    frame: pd.DataFrame,
    row_numbers: list[int],
    *,
    exchanges: dict[str, str],
    default_exchange: str,
    default_timezone: str,
    structural_issues: list[RowIssue] | None = None,
) -> NormalizedChunk:
    """Validate and transform one bounded chunk into canonical records."""
    issues = list(structural_issues or [])
    if frame.empty or "symbol" not in frame.columns:
        return NormalizedChunk([], issues, len(issues))

    frame = frame.reset_index(drop=True).copy()
    symbols = cast(pd.Series, frame["symbol"]).astype("string").str.strip().str.upper()  # pyrefly: ignore[redundant-cast]
    blank_symbol = symbols.isna() | symbols.eq("")
    long_symbol = symbols.str.len().fillna(0) > MAX_SYMBOL_LENGTH

    exchange = _exchange_series(frame, default_exchange)
    known_exchange = exchange.isin(list(exchanges))

    dates = _parse_timestamp_column(
        cast(pd.Series, frame["timestamp"]), exchange, exchanges, default_timezone  # pyrefly: ignore[redundant-cast]
    )

    def add(mask: pd.Series, code: str, field: str, message: str) -> None:
        for position in np.flatnonzero(mask.to_numpy(dtype=bool)):
            issues.append(
                RowIssue(
                    row_number=row_numbers[int(position)],
                    error_code=code,
                    field_name=field,
                    message=message,
                    raw_row=raw_row_preview(frame.iloc[int(position)]),
                )
            )

    add(blank_symbol, IssueCode.MISSING_SYMBOL, "symbol", "symbol is required.")
    add(
        long_symbol,
        IssueCode.INVALID_SYMBOL,
        "symbol",
        f"symbol is longer than {MAX_SYMBOL_LENGTH} characters.",
    )
    add(
        ~known_exchange,
        IssueCode.UNKNOWN_EXCHANGE,
        "exchange",
        "symbol refers to an exchange that is not configured.",
    )
    add(
        dates.isna(),
        IssueCode.INVALID_TIMESTAMP,
        "timestamp",
        "timestamp is missing or could not be parsed.",
    )

    usable = ~(blank_symbol | long_symbol | ~known_exchange | dates.isna())

    for field in _PRICE_FIELDS:
        raw = cast(pd.Series, frame[field])  # pyrefly: ignore[redundant-cast]
        blank = _blank(raw)
        numeric = cast(pd.Series, pd.to_numeric(raw, errors="coerce")).astype("float64")  # pyrefly: ignore[redundant-cast]
        non_numeric = ~blank & numeric.isna()
        infinite = pd.Series(np.isinf(numeric), index=frame.index)
        finite = pd.Series(np.isfinite(numeric), index=frame.index)
        non_positive = finite & (numeric <= 0)
        out_of_range = finite & (numeric.abs() >= float(MAX_PRICE))
        add(blank, IssueCode.MISSING_PRICE, field, f"{field} is required.")
        add(
            non_numeric,
            IssueCode.NON_NUMERIC_PRICE,
            field,
            f"{field} is not numeric.",
        )
        add(
            infinite,
            IssueCode.NON_FINITE_PRICE,
            field,
            f"{field} is not finite.",
        )
        add(
            non_positive,
            IssueCode.NON_POSITIVE_PRICE,
            field,
            f"{field} must be positive.",
        )
        add(
            out_of_range,
            IssueCode.PRICE_OUT_OF_RANGE,
            field,
            f"{field} exceeds the supported price range.",
        )
        usable &= ~(blank | non_numeric | infinite | non_positive | out_of_range)

    raw_volume = cast(pd.Series, frame["volume"])  # pyrefly: ignore[redundant-cast]
    volume_blank = _blank(raw_volume)
    volume_numeric = cast(pd.Series, pd.to_numeric(raw_volume, errors="coerce")).astype("float64")  # pyrefly: ignore[redundant-cast]
    volume_non_numeric = ~volume_blank & volume_numeric.isna()
    volume_infinite = pd.Series(np.isinf(volume_numeric), index=frame.index)
    volume_finite = pd.Series(np.isfinite(volume_numeric), index=frame.index)
    volume_negative = volume_finite & (volume_numeric < 0)
    volume_fractional = (
        volume_finite & (volume_numeric % 1 != 0)
    )
    volume_too_large = volume_finite & (volume_numeric > MAX_VOLUME)
    add(volume_blank, IssueCode.MISSING_VOLUME, "volume", "volume is required.")
    add(
        volume_non_numeric,
        IssueCode.NON_NUMERIC_VOLUME,
        "volume",
        "volume is not numeric.",
    )
    add(
        volume_infinite,
        IssueCode.NON_FINITE_VOLUME,
        "volume",
        "volume is not finite.",
    )
    add(
        volume_negative,
        IssueCode.NEGATIVE_VOLUME,
        "volume",
        "volume must not be negative.",
    )
    add(
        volume_fractional,
        IssueCode.INVALID_VOLUME,
        "volume",
        "volume must be a whole number.",
    )
    add(
        volume_too_large,
        IssueCode.VOLUME_OUT_OF_RANGE,
        "volume",
        "volume exceeds the supported range.",
    )
    usable &= ~(
        volume_blank
        | volume_non_numeric
        | volume_infinite
        | volume_negative
        | volume_fractional
        | volume_too_large
    )

    candidates = pd.DataFrame(
        {
            "row_number": pd.Series(row_numbers, index=frame.index),
            "symbol": symbols,
            "exchange": exchange,
            "trading_date": dates,
            "open": frame["open"],
            "high": frame["high"],
            "low": frame["low"],
            "close":  frame["close"],
            "adjusted_close": (
                frame["adjusted_close"]
                if "adjusted_close" in frame.columns
                else pd.NA
            ),
            "volume": raw_volume,
            "name": _metadata(frame, "name", "", 128),
            "asset_class": _metadata(frame, "asset_class", DEFAULT_ASSET_CLASS, 16),
            "currency": _metadata(frame, "currency", DEFAULT_CURRENCY, 3),
        }
    ).loc[usable]

    records: list[NormalizedRecord] = []
    previews = {
        int(cast(Any, row.row_number)): raw_row_preview(
            frame.iloc[int(cast(Any, row.Index))]
        )
        for row in candidates.itertuples()
    }

    def reject(row_number: int, code: str, field: str | None, message: str) -> None:
        issues.append(
            RowIssue(
                row_number=row_number,
                error_code=code,
                field_name=field,
                message=message,
                raw_row=previews.get(row_number),
            )
        )

    # Only candidates reach this loop; `Index` is the source frame position.
    for row in candidates.itertuples():
        row_number = int(cast(Any, row.row_number))
        prices: dict[str, Decimal] = {}
        invalid = False
        for field in _PRICE_FIELDS:
            value = _price_decimal(getattr(row, field))
            if value is None:
                reject(
                    row_number,
                    IssueCode.PRICE_OUT_OF_RANGE,
                    field,
                    f"{field} exceeds the supported price range.",
                )
                invalid = True
                break
            prices[field] = value
        if invalid:
            continue
        if prices["high"] < prices["low"]:
            reject(
                row_number,
                IssueCode.INVALID_OHLC_RANGE,
                None,
                "high must be greater than or equal to low.",
            )
            continue
        bounds = [
            field
            for field in ("open", "close")
            if not prices["low"] <= prices[field] <= prices["high"]
        ]
        if bounds:
            for field in bounds:
                reject(
                    row_number,
                    IssueCode.PRICE_OUT_OF_BOUNDS,
                    field,
                    f"{field} must be between low and high.",
                )
            continue
        adjusted = _optional_price(row.adjusted_close)
        if adjusted is False:
            reject(
                row_number,
                IssueCode.INVALID_ADJUSTED_CLOSE,
                "adjusted_close",
                "adjusted_close must be a positive finite number in range.",
            )
            continue
        volume = _volume_int(row.volume)
        if volume is None:
            reject(
                row_number,
                IssueCode.INVALID_VOLUME,
                "volume",
                "volume must be a whole number in range.",
            )
            continue
        if not isinstance(row.trading_date, date):
            reject(
                row_number,
                IssueCode.INVALID_TIMESTAMP,
                "timestamp",
                "timestamp is missing or could not be parsed.",
            )
            continue
        records.append(
            NormalizedRecord(
                row_number=row_number,
                symbol=str(row.symbol),
                exchange_code=str(row.exchange),
                trading_date=row.trading_date,
                open=prices["open"],
                high=prices["high"],
                low=prices["low"],
                close=prices["close"],
                adjusted_close=cast(Decimal | None, adjusted),
                volume=volume,
                name=str(row.name or "")[:128],
                asset_class=str(row.asset_class or DEFAULT_ASSET_CLASS),
                currency=str(row.currency or DEFAULT_CURRENCY),
            )
        )

    issues.sort(key=lambda issue: (issue.row_number or 0, issue.error_code))
    return NormalizedChunk(records, issues, len(frame) + len(structural_issues or []))


def _price_decimal(value: object) -> Decimal | None:
    quantized = _decimal(value)
    if quantized is None:
        return None
    try:
        quantized = quantized.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if quantized <= 0 or quantized >= MAX_PRICE:
        return None
    return quantized


def _optional_price(value: object) -> Decimal | None | bool:
    """Return a rounded price, None for a missing optional value, or False."""
    if _blank_scalar(value):
        return None
    return _price_decimal(value) or False


def _volume_int(value: object) -> int | None:
    decimal = _decimal(value)
    if decimal is None or decimal < 0 or decimal > MAX_VOLUME:
        return None
    if decimal != decimal.to_integral_value():
        return None
    return int(decimal)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _exchange_series(frame: pd.DataFrame, default_exchange: str) -> pd.Series:
    if "exchange" not in frame.columns:
        return pd.Series(default_exchange, index=frame.index, dtype="string")
    exchange = frame["exchange"].astype("string").str.strip().str.upper()
    return exchange.mask(exchange.isna() | exchange.eq(""), default_exchange)


def _parse_timestamp_column(
    raw: pd.Series,
    exchange: pd.Series,
    exchanges: dict[str, str],
    default_timezone: str,
) -> pd.Series:
    """Return a trading date per row, honoring each exchange's timezone.

    A date-only value is a trading date and is used as-is. A naive datetime is
    interpreted as local time at the exchange. An offset-aware datetime is
    converted to the exchange timezone before the date is taken.
    """
    result = pd.Series(None, index=raw.index, dtype=object)
    for code in exchange.dropna().unique():
        mask = exchange.eq(code)
        timezone = _zone(exchanges.get(str(code), default_timezone))
        result.loc[mask] = _parse_dates(raw.loc[mask], timezone)
    return result


def _parse_dates(raw: pd.Series, timezone: ZoneInfo) -> pd.Series:
    text = raw.astype("string").str.strip()
    parsed = pd.to_datetime(text, errors="coerce", format="mixed", utc=True)
    aware = text.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
    aware_dates = parsed.dt.tz_convert(timezone).dt.date
    naive_dates = parsed.dt.tz_localize(None).dt.date
    dates = naive_dates.copy()
    dates[aware] = aware_dates[aware]
    dates = dates.astype(object)
    dates.loc[parsed.isna()] = None
    return dates


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def _metadata(
    frame: pd.DataFrame, column: str, default: str, max_length: int
) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="string")
    values = frame[column].astype("string").str.strip()
    if column == "asset_class":
        values = values.str.lower()
        values = values.where(values.isin(ASSET_CLASSES), default)
    elif column == "currency":
        values = values.str.upper()
        valid = values.str.fullmatch(r"[A-Z]{3}").fillna(False)
        values = values.where(valid, default)
    values = values.mask(values.isna() | values.eq(""), default)
    return values.str.slice(0, max_length)


def _blank(raw: pd.Series) -> pd.Series:
    text = raw.astype("string").str.strip()
    return (
        raw.isna()
        | text.isna()
        | text.eq("")
        | text.str.lower().isin(["nan", "none", "null"])
    )


def _blank_scalar(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if type(value).__name__ == "NAType":  # pandas.NA
        return True
    return isinstance(value, str) and (
        not value.strip() or value.strip().lower() in {"nan", "none", "null"}
    )
