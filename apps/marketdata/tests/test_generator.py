"""Offline tests for the synthetic-data generator (no database access)."""

import importlib.util
import json
from pathlib import Path

import openpyxl
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_COLUMNS = {
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
}


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "marketdata_generator", REPO_ROOT / "scripts" / "generate_market_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return load_generator()


@pytest.fixture(scope="module")
def clean_rows(generator):
    rng = generator.np.random.default_rng(7)
    instruments = generator.build_instruments(3)
    dates = generator.build_dates(generator.date(2024, 1, 2), 30)
    return generator.generate_rows(instruments, dates, rng)


def test_row_schema_and_plausibility(clean_rows):
    assert clean_rows, "expected generated rows"
    for row in clean_rows:
        assert REQUIRED_COLUMNS <= set(row)
        assert (
            row["low"]
            <= min(row["open"], row["close"])
            <= max(row["open"], row["close"])
            <= row["high"]
        )
        assert row["open"] > 0 and row["close"] > 0
        assert row["adjusted_close"] > 0
        assert isinstance(row["volume"], int) and row["volume"] >= 1_000


def test_overlapping_dates_across_symbols(clean_rows):
    symbols = {row["symbol"] for row in clean_rows}
    timestamps = {row["timestamp"] for row in clean_rows}
    for day in timestamps:
        day_symbols = {row["symbol"] for row in clean_rows if row["timestamp"] == day}
        assert day_symbols == symbols


def test_instruments_carry_reference_fields(generator):
    instruments = generator.build_instruments(5)
    for item in instruments:
        assert item["exchange"] and item["symbol"]
        assert item["asset_class"] in {
            "equity",
            "etf",
            "currency",
            "commodity",
            "crypto",
        }
        assert len(item["currency"]) == 3 and item["currency"].isupper()
        assert "/" in item["timezone"]  # IANA-style identifier


def test_generate_writes_matching_schemas(tmp_path, generator):
    out = tmp_path / "data"
    counts = generator.generate(out, symbols=2, days=5, seed=42)

    for extension in ("csv", "json", "xlsx"):
        for label in ("clean", "dirty"):
            path = out / f"synthetic_market_{label}.{extension}"
            assert path.exists(), path
            if extension == "csv":
                import csv

                with path.open(newline="", encoding="utf-8") as handle:
                    header = next(csv.reader(handle))
                assert REQUIRED_COLUMNS == set(header)
            elif extension == "json":
                rows = json.loads(path.read_text(encoding="utf-8"))
                assert counts[f"synthetic_market_{label}.json"] == len(rows)
                union = set().union(*(set(row) for row in rows))
                if label == "clean":
                    assert REQUIRED_COLUMNS <= union
                else:
                    assert union <= REQUIRED_COLUMNS
            else:
                sheet = openpyxl.load_workbook(path).active
                header = [cell.value for cell in sheet[1]]
                assert REQUIRED_COLUMNS == set(header)
                assert sheet.max_row - 1 == counts[f"synthetic_market_{label}.xlsx"]


def test_generate_reproducible_same_seed(tmp_path, generator):
    import pandas as pd

    first = tmp_path / "one"
    second = tmp_path / "two"
    for directory in (first, second):
        generator.generate(directory, symbols=3, days=4, seed=123)
    assert (first / "synthetic_market_clean.csv").read_bytes() == (
        second / "synthetic_market_clean.csv"
    ).read_bytes()
    assert (first / "synthetic_market_dirty.csv").read_bytes() == (
        second / "synthetic_market_dirty.csv"
    ).read_bytes()
    left = pd.read_excel(first / "synthetic_market_dirty.xlsx")
    right = pd.read_excel(second / "synthetic_market_dirty.xlsx")
    assert left.equals(right)


def test_dirty_fixture_errors_are_distinct(tmp_path, generator):
    out = tmp_path / "dirty"
    generator.generate(out, symbols=2, days=30, seed=42)
    rows = json.loads((out / "synthetic_market_dirty.json").read_text(encoding="utf-8"))
    clean = json.loads(
        (out / "synthetic_market_clean.json").read_text(encoding="utf-8")
    )
    assert len(rows) == len(clean) + 1  # one duplicate-date row appended

    from collections import Counter

    symbol_dates = Counter((row["symbol"], row["timestamp"]) for row in rows)
    assert any(count > 1 for count in symbol_dates.values()), "expected duplicate date"

    issues = set()
    for row in rows:
        if "volume" not in row or row["volume"] is None:
            issues.add("missing_field")
        elif not isinstance(row["volume"], int) or row["volume"] < 0:
            issues.add("non_positive_volume")
        if not isinstance(row["timestamp"], str) or len(row["timestamp"]) != 10:
            issues.add("bad_timestamp")
        if row["close"] is None:
            issues.add("null_close")
        if row["open"] is None or not isinstance(row["open"], (int, float)):
            issues.add("non_numeric_open")
        if (
            isinstance(row["high"], (int, float))
            and isinstance(row["low"], (int, float))
            and isinstance(row["open"], (int, float))
            and isinstance(row["close"], (int, float))
            and (
                row["low"] > min(row["open"], row["close"])
                or row["high"] < max(row["open"], row["close"])
            )
        ):
            issues.add("bad_ohlc")
        if row["adjusted_close"] == "":
            issues.add("empty_string")
    assert {
        "missing_field",
        "non_positive_volume",
        "bad_timestamp",
        "null_close",
        "non_numeric_open",
        "bad_ohlc",
        "empty_string",
    } <= issues


def test_unexpected_columns_and_empty_fixtures(tmp_path, generator):
    import csv

    out = tmp_path / "edge"
    generator.generate(out, symbols=1, days=12, seed=42)

    with (out / "synthetic_market_unexpected_columns.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        header = next(csv.reader(handle))
    assert "unexpected_extra" in header

    assert (out / "synthetic_market_empty.csv").stat().st_size == 0
    assert (out / "synthetic_market_empty.json").stat().st_size == 0

    sheet = openpyxl.load_workbook(out / "synthetic_market_empty.xlsx").active
    assert sheet.max_row == 1  # header only, zero data rows


def test_cli_help(capsys, generator):
    with pytest.raises(SystemExit) as excinfo:
        generator.parse_args(["--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--seed" in output
    assert "--output-dir" in output


def test_cli_run_produces_logged_files(tmp_path, generator, caplog):
    counts = generator.generate(
        tmp_path, symbols=2, days=3, seed=99, start=generator.date(2024, 5, 1)
    )
    assert counts["synthetic_market_clean.csv"] == 6
    assert counts["synthetic_market_dirty.csv"] == 7
