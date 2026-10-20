"""Deterministic synthetic market-data generator for development and testing.

Generates clean and intentionally dirty fixtures in CSV, JSON, and XLSX with a
consistent schema. Runs offline: no Django configuration, database, or market
API is required.

Example:
    python scripts/generate_market_data.py --symbols 5 --days 100 --seed 42 \
        --output-dir sample_data
"""

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

EXCHANGES = [
    ("XNYS", "New York Stock Exchange", "America/New_York"),
    ("XNAS", "Nasdaq", "America/New_York"),
    ("XLON", "London Stock Exchange", "Europe/London"),
]
SYMBOL_POOL = [
    ("ACME", "Acme Industrial"),
    ("BETA", "Beta Materials"),
    ("CHRP", "Chipwave Systems"),
    ("DLTA", "Delta Logistics"),
    ("EPSN", "Epsilon Energy"),
    ("FBNK", "Fjord Banking Group"),
    ("GLXO", "Glaxo Robotics"),
    ("HRZN", "Horizon Retail"),
    ("INFX", "Infinitix Data"),
    ("JVNL", "Javelin Networks"),
]
ASSET_CLASSES = ["equity", "etf", "equity", "equity", "crypto"]
CURRENCIES = ["USD", "USD", "USD", "USD", "EUR"]

COLUMNS = [
    "exchange",
    "symbol",
    "name",
    "asset_class",
    "currency",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
]

# Kinds of corruption injected into dirty fixtures. Every kind appears at least
# once; a "duplicate_date" row is also appended verbatim from the clean set.
ERROR_KINDS = [
    "missing_field",
    "bad_ohlc",
    "negative_volume",
    "bad_date",
    "duplicate_date",
    "null_close",
    "bad_type",
    "malformed_number",
    "empty_string",
    "negative_price",
]


def build_instruments(count: int) -> list[dict]:
    """Return `count` instrument descriptors drawn from a fixed vocabulary."""
    if count < 1:
        raise ValueError("At least one instrument is required.")
    instruments = []
    for index in range(count):
        base_symbol, base_name = SYMBOL_POOL[index % len(SYMBOL_POOL)]
        generation = index // len(SYMBOL_POOL)
        symbol = base_symbol if generation == 0 else f"{base_symbol}{generation}"
        exchange_code, exchange_name, timezone = EXCHANGES[index % len(EXCHANGES)]
        instruments.append(
            {
                "exchange": exchange_code,
                "exchange_name": exchange_name,
                "timezone": timezone,
                "symbol": symbol,
                "name": base_name if generation == 0 else f"{base_name} {generation}",
                "asset_class": ASSET_CLASSES[index % len(ASSET_CLASSES)],
                "currency": CURRENCIES[index % len(CURRENCIES)],
            }
        )
    return instruments


def build_dates(start: date, days: int) -> list[date]:
    """Return `days` consecutive weekdays so symbols share trading dates."""
    if days < 1:
        raise ValueError("At least one trading day is required.")
    dates: list[date] = []
    current = start
    while len(dates) < days:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _assert_clean(row: dict) -> None:
    """Guard against accidentally invalid rows in the clean dataset."""
    assert row["low"] <= min(row["open"], row["close"]), row
    assert row["high"] >= max(row["open"], row["close"]), row
    assert row["open"] > 0 and row["close"] > 0, row
    assert row["adjusted_close"] is None or row["adjusted_close"] > 0, row
    assert isinstance(row["volume"], int) and row["volume"] >= 0, row


def generate_rows(
    instruments: list[dict], dates: list[date], rng: np.random.Generator
) -> list[dict]:
    """Generate plausible OHLCV rows with rounding-safe intra-day ranges."""
    rows: list[dict] = []
    last_close: dict[str, float] = {}
    for day in dates:
        for item in instruments:
            symbol = item["symbol"]
            base = last_close.get(symbol, float(rng.uniform(20, 150)))
            close = max(1.0, base * float(rng.normal(1.0, 0.02)))
            open_price = max(1.0, close * float(rng.normal(1.0, 0.005)))
            high = max(open_price, close) * float(rng.uniform(1.0001, 1.02))
            low = min(open_price, close) * float(rng.uniform(0.98, 0.9999))
            adjusted = close * float(rng.uniform(0.99, 0.999))
            volume = int(rng.integers(1_000, 10_000_000))
            last_close[symbol] = close
            row = {
                "exchange": item["exchange"],
                "exchange_name": item["exchange_name"],
                "timezone": item["timezone"],
                "symbol": symbol,
                "name": item["name"],
                "asset_class": item["asset_class"],
                "currency": item["currency"],
                "timestamp": day.isoformat(),
                "open": round(open_price, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "adjusted_close": round(adjusted, 4),
                "volume": volume,
            }
            _assert_clean(row)
            rows.append(row)
    return rows


def make_dirty_rows(rows: list[dict], rng: np.random.Generator) -> list[dict]:
    """Corrupt a copy of the rows so every error kind appears at least once.

    Very small datasets apply only the corruption kinds that fit; the
    duplicate-date row is always appended last.
    """
    dirty = [dict(row) for row in rows]
    count = len(dirty)
    slot_kinds = [kind for kind in ERROR_KINDS if kind != "duplicate_date"]
    apply_kinds = slot_kinds[: min(count, len(slot_kinds))]
    # Corruption sites spread evenly across the file; rng drives the actual
    # error values so the whole output stays seed-deterministic.
    slots = np.linspace(0, count - 1, num=len(apply_kinds)).astype(int)
    for kind, index in zip(apply_kinds, slots):
        row = dirty[int(index)]
        if kind == "missing_field":
            row.pop("volume", None)
        elif kind == "bad_ohlc":
            row["high"], row["low"] = row["low"], row["high"]
        elif kind == "negative_volume":
            row["volume"] = -int(rng.integers(1, 5_000))
        elif kind == "bad_date":
            row["timestamp"] = f"{row['timestamp']}tail"
        elif kind == "null_close":
            row["close"] = None
        elif kind == "bad_type":
            row["volume"] = "12k"
        elif kind == "malformed_number":
            row["open"] = f'{row["open"]:,.4f}'
        elif kind == "empty_string":
            row["adjusted_close"] = ""
        elif kind == "negative_price":
            row["low"] = round(row["low"] - row["close"] * 2, 4)
    return dirty + [dict(rows[0])]


def write_csv(
    rows: list[dict], path: Path, columns: list[str] = COLUMNS
) -> None:
    pd.DataFrame(rows, columns=pd.Index(columns)).to_csv(path, index=False)


def write_json(rows: list[dict], path: Path) -> None:
    payload = [{key: row.get(key) for key in COLUMNS} for row in rows]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_xlsx(
    rows: list[dict], path: Path, columns: list[str] = COLUMNS
) -> None:
    pd.DataFrame(rows, columns=pd.Index(columns)).to_excel(path, index=False)


def generate(
    output_dir: Path,
    symbols: int = 5,
    days: int = 100,
    seed: int = 42,
    start: date | None = None,
) -> dict[str, int]:
    """Write clean and dirty fixtures in all three formats; return row counts."""
    rng = np.random.default_rng(seed)
    instruments = build_instruments(symbols)
    dates = build_dates(start or date(2024, 1, 2), days)
    rows = generate_rows(instruments, dates, rng)
    dirty_rows = make_dirty_rows(rows, rng)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    writers = {"csv": write_csv, "json": write_json, "xlsx": write_xlsx}
    payloads = {"clean": rows, "dirty": dirty_rows}
    for extension, writer in writers.items():
        for label, payload in payloads.items():
            stem = f"synthetic_market_{label}.{extension}"
            writer(payload, out / stem)
            counts[stem] = len(payload)

    # Empty fixtures: parsers must reject these without confusing schemas.
    counts["synthetic_market_empty.csv"] = 0
    counts["synthetic_market_empty.json"] = 0
    counts["synthetic_market_empty.xlsx"] = 0
    (out / "synthetic_market_empty.csv").write_bytes(b"")
    (out / "synthetic_market_empty.json").write_bytes(b"")
    write_xlsx([], out / "synthetic_market_empty.xlsx")

    # Unexpected extra column appended to an otherwise valid subset.
    unexpected = [
        dict(row, unexpected_extra="ignore-me") for row in rows[: max(len(rows) // 4, 1)]
    ]
    counts["synthetic_market_unexpected_columns.csv"] = len(unexpected)
    write_csv(unexpected, out / "synthetic_market_unexpected_columns.csv",
              columns=COLUMNS + ["unexpected_extra"])
    return counts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic OHLCV fixtures (clean and dirty) in CSV, JSON, and XLSX."
    )
    parser.add_argument(
        "--symbols", type=int, default=5, help="number of instruments (default: 5)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=100,
        help="trading days per instrument (default: 100)",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="seed for reproducible data (default: 42)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("sample_data"),
        help="directory for generated files (default: sample_data)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default="2024-01-02",
        help="first trading date, YYYY-MM-DD (default: 2024-01-02)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    start = date.fromisoformat(args.start_date)
    counts = generate(
        args.output_dir,
        symbols=args.symbols,
        days=args.days,
        seed=args.seed,
        start=start,
    )
    total = sum(counts.values())
    logger.info(
        "Generated %d files (%d fixture rows) in %s",
        len(counts),
        total,
        args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
