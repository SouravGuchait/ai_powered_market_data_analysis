"""Bounded ingestion: parsing, validation, dedupe, and job orchestration."""

import hashlib
import importlib.util
import io
import json
import logging
from datetime import date
from pathlib import Path

import openpyxl
import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.utils import timezone

from apps.marketdata.ingestion.normalize import normalize_chunk
from apps.marketdata.ingestion.parsing import iter_input_chunks
from apps.marketdata.ingestion.schema import InputError, IssueCode
from apps.marketdata.jobs.ingestion.pipeline import build_dataset
from apps.marketdata.jobs.ingestion.process import run_ingestion_job
from apps.marketdata.models import (
    Exchange,
    IngestionFile,
    IngestionJob,
    Instrument,
    PriceRecord,
    ValidationIssue,
)
from apps.marketdata.tasks import process_ingestion_job

pytestmark = pytest.mark.django_db

REPO_ROOT = Path(__file__).resolve().parents[3]
HEADER = b"symbol,timestamp,open,high,low,close,volume\n"


def csv_data(*rows: bytes) -> bytes:
    return HEADER + b"".join(rows)


def row(
    symbol: str = "AAPL",
    timestamp: str = "2024-01-02",
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100.5",
    volume: str = "1000",
) -> bytes:
    return (
        f"{symbol},{timestamp},{open_},{high},{low},{close},{volume}\n".encode()
    )


def json_data(records: list[dict]) -> bytes:
    return json.dumps(records).encode()


def xlsx_data(records: list[dict], columns: list[str] | None = None) -> bytes:
    columns = columns or [
        "symbol",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(columns)
    for record in records:
        sheet.append([record.get(column) for column in columns])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_job(source_type: str = "csv") -> IngestionJob:
    return IngestionJob.objects.create(source_type=source_type)


def ingest(
    data: bytes,
    *,
    source_type: str = "csv",
    chunk_size: int = 2,
    batch_size: int = 1,
    metric_calculator=None,
):
    job = make_job(source_type)
    outcome = build_dataset(
        job,
        io.BytesIO(data),
        source_type=source_type,
        chunk_size=chunk_size,
        batch_size=batch_size,
        metric_calculator=metric_calculator,
    )
    job.refresh_from_db()
    return job, outcome


@pytest.fixture
def upload_store(tmp_path, monkeypatch):
    backend = FileSystemStorage(location=tmp_path)
    monkeypatch.setattr(
        "apps.marketdata.storage.get_upload_storage", lambda: backend
    )
    return backend


def queue_upload(store, data: bytes, *, source_type: str = "csv") -> IngestionJob:
    job = IngestionJob.objects.create(source_type=source_type)
    key = store.save(
        f"uploads/1/{job.pk}/fixture.{source_type}", ContentFile(data)
    )
    IngestionFile.objects.create(
        ingestion_job=job,
        original_filename=f"fixture.{source_type}",
        storage_key=key,
        checksum=hashlib.sha256(data).hexdigest(),
        file_size_bytes=len(data),
    )
    return job


def codes(job: IngestionJob) -> list[str]:
    return list(
        ValidationIssue.objects.filter(ingestion_job=job)
        .order_by("row_number", "error_code")
        .values_list("error_code", flat=True)
    )


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "marketdata_generator", REPO_ROOT / "scripts" / "generate_market_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Parsing and normalization


def test_csv_chunks_preserve_row_numbers_and_report_malformed_rows():
    data = csv_data(
        row(),
        row("MSFT"),
        b"BROKEN,2024-01-04,1,2\n",
        b"\n",
        row("GOOG"),
    )
    chunks = list(iter_input_chunks(io.BytesIO(data), "csv", 2))
    assert [chunk.row_numbers for chunk in chunks] == [[2, 3], [6]]
    malformed = chunks[1].issues
    assert len(malformed) == 1
    assert malformed[0].row_number == 4
    assert malformed[0].error_code == IssueCode.MALFORMED_ROW
    assert chunks[0].frame["symbol"].tolist() == ["AAPL", "MSFT"]
    assert chunks[1].frame["symbol"].tolist() == ["GOOG"]


def test_header_is_case_and_space_insensitive_and_may_be_reordered():
    data = b" Volume ,Symbol,Close,Open,High,Low,Timestamp\n"
    data += b"1000,aapl,100.5,100,101,99,2024-01-02\n"
    chunks = list(iter_input_chunks(io.BytesIO(data), "csv", 10))
    result = normalize_chunk(
        chunks[0].frame,
        chunks[0].row_numbers,
        exchanges={"XNYS": "America/New_York"},
        default_exchange="XNYS",
        default_timezone="America/New_York",
    )
    assert len(result.records) == 1
    assert result.records[0].symbol == "AAPL"
    assert result.records[0].close == 100.5


def test_missing_required_column_is_a_file_level_error():
    with pytest.raises(InputError) as excinfo:
        list(
            iter_input_chunks(
                io.BytesIO(b"symbol,timestamp,open\nA,2024-01-02,1\n"), "csv", 10
            )
        )
    assert excinfo.value.code == IssueCode.MISSING_COLUMNS


def test_extra_columns_are_ignored_and_non_utf8_is_rejected():
    data = (
        b"symbol,timestamp,open,high,low,close,volume,extra\n"
        b"AAPL,2024-01-02,1,1,1,1,10,ignored\n"
    )
    _, outcome = ingest(data)
    assert outcome.rows_accepted == 1

    with pytest.raises(InputError) as excinfo:
        list(iter_input_chunks(io.BytesIO(b"symbol,timestamp\n\xff,2024-01-02\n"), "csv", 10))
    assert excinfo.value.code == IssueCode.INVALID_ENCODING


def test_empty_inputs_are_rejected_per_format():
    with pytest.raises(InputError) as excinfo:
        list(iter_input_chunks(io.BytesIO(b""), "csv", 10))
    assert excinfo.value.code == IssueCode.EMPTY_FILE

    with pytest.raises(InputError) as excinfo:
        list(iter_input_chunks(io.BytesIO(b"[]"), "json", 10))
    assert excinfo.value.code == IssueCode.EMPTY_FILE

    with pytest.raises(InputError) as excinfo:
        list(iter_input_chunks(io.BytesIO(xlsx_data([])), "xlsx", 10))
    assert excinfo.value.code == IssueCode.EMPTY_FILE


def test_malformed_json_and_xlsx_are_file_level_errors():
    with pytest.raises(InputError) as excinfo:
        list(iter_input_chunks(io.BytesIO(b'[{"symbol":'), "json", 10))
    assert excinfo.value.code == IssueCode.MALFORMED_INPUT

    with pytest.raises(InputError) as excinfo:
        list(iter_input_chunks(io.BytesIO(b"PK\x03\x04not-a-workbook"), "xlsx", 10))
    assert excinfo.value.code == IssueCode.MALFORMED_INPUT


def test_json_and_xlsx_are_consumed_in_chunks():
    records = [
        {
            "symbol": "AAPL",
            "timestamp": "2024-01-02",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1000,
        },
        {
            "symbol": "MSFT",
            "timestamp": "2024-01-03",
            "open": 200,
            "high": 201,
            "low": 199,
            "close": 200.5,
            "volume": 2000,
        },
    ]
    for source_type, payload in (
        ("json", json_data(records)),
        ("xlsx", xlsx_data(records)),
    ):
        chunks = list(iter_input_chunks(io.BytesIO(payload), source_type, 1))
        assert len(chunks) == 2
        for chunk in chunks:
            result = normalize_chunk(
                chunk.frame,
                chunk.row_numbers,
                exchanges={"XNYS": "America/New_York"},
                default_exchange="XNYS",
                default_timezone="America/New_York",
                structural_issues=chunk.issues,
            )
            assert len(result.records) == 1


def test_timestamp_timezone_policy():
    records = [
        ("A", "2024-01-03T02:00:00+00:00", date(2024, 1, 2)),
        ("B", "2024-01-03T02:00:00", date(2024, 1, 3)),
        ("C", "2024-01-03", date(2024, 1, 3)),
    ]
    data = csv_data(
        *[
            row(symbol, timestamp, "10", "10", "10", "10", "1")
            for symbol, timestamp, _ in records
        ]
    )
    chunk = next(iter_input_chunks(io.BytesIO(data), "csv", 10))
    result = normalize_chunk(
        chunk.frame,
        chunk.row_numbers,
        exchanges={"XNYS": "America/New_York"},
        default_exchange="XNYS",
        default_timezone="America/New_York",
    )
    assert [record.trading_date for record in result.records] == [
        expected for _, _, expected in records
    ]


def test_numeric_and_ohlc_edge_cases_are_reported_not_dropped():
    data = csv_data(
        row("STR", "2024-01-02", "1e2", "101", "99", "100.5", "1e3"),
        row("NAN", "2024-01-02", "NaN", "101", "99", "100.5", "1000"),
        row("INF", "2024-01-02", "inf", "101", "99", "100.5", "1000"),
        row("NINF", "2024-01-02", "1", "-inf", "99", "100.5", "1000"),
        row("ABC", "2024-01-02", "abc", "101", "99", "100.5", "1000"),
        row("NEG", "2024-01-02", "-1", "101", "99", "100.5", "1000"),
        row("SWAP", "2024-01-02", "100", "98", "99", "100.5", "1000"),
        row("OUT", "2024-01-02", "102", "101", "99", "100.5", "1000"),
        row("NEGVOL", "2024-01-02", "100", "101", "99", "100.5", "-1"),
        row("FRACVOL", "2024-01-02", "100", "101", "99", "100.5", "1.5"),
    )
    job, outcome = ingest(data)
    assert outcome.rows_received == 10
    assert outcome.rows_accepted == 1
    assert outcome.rows_rejected == 9
    found = codes(job)
    assert IssueCode.MISSING_PRICE in found
    assert IssueCode.NON_NUMERIC_PRICE in found
    assert IssueCode.NON_FINITE_PRICE in found
    assert IssueCode.NON_POSITIVE_PRICE in found
    assert IssueCode.INVALID_OHLC_RANGE in found
    assert IssueCode.PRICE_OUT_OF_BOUNDS in found
    assert IssueCode.NEGATIVE_VOLUME in found
    assert IssueCode.INVALID_VOLUME in found
    assert PriceRecord.objects.get(ingestion_job=job).volume == 1000


def test_blank_symbol_and_unknown_exchange_are_reported():
    data = (
        b"exchange,symbol,timestamp,open,high,low,close,volume\n"
        b"XNAS,   ,2024-01-02,1,1,1,1,1\n"
        b"XNAS,AAPL,2024-01-02,1,1,1,1,1\n"
    )
    job, outcome = ingest(data)
    assert outcome.rows_accepted == 0
    found = codes(job)
    assert IssueCode.MISSING_SYMBOL in found
    assert IssueCode.UNKNOWN_EXCHANGE in found


def test_integer_typed_decimals_round_to_six_places():
    data = csv_data(row("A", "2024-01-02", "1.2345678", "1.234568", "1.234567", "1.234568", "0"))
    _, outcome = ingest(data)
    assert outcome.rows_accepted == 1
    record = PriceRecord.objects.get()
    assert str(record.open) == "1.234568"
    assert record.volume == 0


# Duplicate policy


def test_duplicates_within_a_chunk_and_across_chunks_are_rejected():
    data = csv_data(
        row("AAPL", "2024-01-02"),
        row("AAPL", "2024-01-02"),
        row("AAPL", "2024-01-03"),
        row("AAPL", "2024-01-03"),
    )
    job, outcome = ingest(data, chunk_size=2, batch_size=1)
    assert outcome.rows_received == 4
    assert outcome.rows_accepted == 2
    assert outcome.rows_rejected == 2
    assert codes(job).count(IssueCode.DUPLICATE_RECORD) == 2
    assert PriceRecord.objects.filter(ingestion_job=job).count() == 2


def test_conflict_with_an_existing_canonical_record_is_rejected():
    exchange = Exchange.objects.create(
        code="XNYS", name="New York", timezone="America/New_York"
    )
    instrument = Instrument.objects.create(
        exchange=exchange, symbol="AAPL", asset_class="equity", currency="USD"
    )
    earlier_job = make_job()
    PriceRecord.objects.create(
        instrument=instrument,
        ingestion_job=earlier_job,
        trading_date=date(2024, 1, 2),
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=10,
    )
    job, outcome = ingest(csv_data(row()))
    assert outcome.rows_accepted == 0
    assert codes(job) == [IssueCode.DUPLICATE_RECORD]
    assert PriceRecord.objects.filter(ingestion_job=job).count() == 0


def test_same_symbol_on_different_exchanges_is_not_a_duplicate():
    Exchange.objects.create(code="XNYS", name="New York", timezone="America/New_York")
    Exchange.objects.create(code="XNAS", name="Nasdaq", timezone="America/New_York")
    data = (
        b"exchange,symbol,timestamp,open,high,low,close,volume\n"
        b"XNYS,AAPL,2024-01-02,1,1,1,1,1\n"
        b"XNAS,AAPL,2024-01-02,2,2,2,2,2\n"
    )
    _, outcome = ingest(data)
    assert outcome.rows_accepted == 2


# Job outcomes


def test_all_invalid_rows_accept_nothing():
    job, outcome = ingest(csv_data(row("AAPL", "bad-date"), row("", "2024-01-02")))
    assert outcome.rows_received == 2
    assert outcome.rows_accepted == 0
    assert outcome.rows_rejected == 2
    assert ValidationIssue.objects.filter(ingestion_job=job).count() >= 2
    assert not PriceRecord.objects.filter(ingestion_job=job).exists()


def test_header_only_file_fails_with_empty_file():
    with pytest.raises(InputError) as excinfo:
        ingest(HEADER)
    assert excinfo.value.code == IssueCode.EMPTY_FILE


def test_final_partial_batch_is_written():
    data = csv_data(
        row("A", "2024-01-02"),
        row("B", "2024-01-02"),
        row("C", "2024-01-02"),
    )
    _, outcome = ingest(data, chunk_size=2, batch_size=1)
    assert outcome.rows_accepted == 3


def test_metric_hook_runs_once_after_persistence():
    calls = []

    class Calculator:
        def on_job_completed(self, *, job):
            calls.append((job.pk, PriceRecord.objects.filter(ingestion_job=job).count()))

    data = csv_data(row("A"), row("B"))
    job, outcome = ingest(data, metric_calculator=Calculator())
    assert calls == [(job.pk, 2)]
    assert outcome.rows_accepted == 2


# Worker lifecycle


def test_worker_completes_job_deletes_upload_and_logs_counts(upload_store, caplog):
    data = csv_data(row("AAPL"), row("MSFT", "2024-01-03"))
    job = queue_upload(upload_store, data)
    key = job.files.get().storage_key

    with caplog.at_level(logging.INFO, logger="apps.marketdata.jobs.ingestion.process"):
        process_ingestion_job(str(job.pk))

    job.refresh_from_db()
    assert process_ingestion_job.name == "apps.marketdata.tasks.process_ingestion_job"
    assert job.status == IngestionJob.Status.COMPLETED
    assert job.rows_received == 2
    assert job.rows_accepted == 2
    assert job.rows_rejected == 0
    assert job.completed_at is not None
    assert not upload_store.exists(key)
    assert "ingestion.job.completed" in caplog.text
    assert str(job.pk) in caplog.text
    assert "received=2" in caplog.text
    assert "AAPL" not in caplog.text


def test_worker_marks_invalid_header_as_failed_and_cleans_up(upload_store):
    job = queue_upload(upload_store, b"symbol,timestamp,open\nA,2024-01-02,1\n")
    key = job.files.get().storage_key
    run_ingestion_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == IngestionJob.Status.FAILED
    assert job.error_code == IssueCode.MISSING_COLUMNS
    assert job.rows_accepted == 0
    assert job.completed_at is not None
    assert not upload_store.exists(key)


def test_worker_rolls_back_partial_rows_on_unexpected_failure(
    upload_store, monkeypatch, settings
):
    settings.INGESTION_CHUNK_SIZE = 2
    settings.INGESTION_DB_BATCH_SIZE = 1
    data = csv_data(
        row("A", "2024-01-02"),
        row("B", "2024-01-02"),
        row("C", "2024-01-02"),
    )
    job = queue_upload(upload_store, data)
    real_persist = __import__(
        "apps.marketdata.jobs.ingestion.pipeline", fromlist=["persist_records"]
    ).persist_records
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            raise RuntimeError("simulated failure")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(
        "apps.marketdata.jobs.ingestion.pipeline.persist_records", flaky
    )
    outcome = run_ingestion_job(str(job.pk))
    job.refresh_from_db()
    assert outcome is None
    assert job.status == IngestionJob.Status.FAILED
    assert job.error_code == IssueCode.PROCESSING_ERROR
    assert not PriceRecord.objects.filter(ingestion_job=job).exists()


def test_worker_skips_terminal_and_running_jobs(upload_store):
    data = csv_data(row())
    for status in (IngestionJob.Status.COMPLETED, IngestionJob.Status.RUNNING):
        job = queue_upload(upload_store, data)
        IngestionJob.objects.filter(pk=job.pk).update(
            status=status,
            started_at=timezone.now() if status == IngestionJob.Status.RUNNING else None,
            completed_at=(
                timezone.now() if status == IngestionJob.Status.COMPLETED else None
            ),
        )
        assert run_ingestion_job(str(job.pk)) is None
        assert not PriceRecord.objects.filter(ingestion_job=job).exists()


def test_repeated_processing_does_not_duplicate_rows_or_issues():
    job = make_job()
    data = csv_data(row("AAPL", "2024-01-02"), row("AAPL", "2024-01-02"))
    for _ in range(2):
        build_dataset(job, io.BytesIO(data), source_type="csv", chunk_size=2)
    assert PriceRecord.objects.filter(ingestion_job=job).count() == 1
    assert ValidationIssue.objects.filter(ingestion_job=job).count() == 1


# Synthetic fixtures


def test_generated_csv_json_xlsx_fixtures_are_consumed(tmp_path):
    generator = load_generator()
    generator.generate(tmp_path, symbols=1, days=2, seed=11)
    for source_type in ("csv", "json", "xlsx"):
        payload = (tmp_path / f"synthetic_market_clean.{source_type}").read_bytes()
        PriceRecord.objects.all().delete()
        _, outcome = ingest(payload, source_type=source_type, chunk_size=1)
        assert outcome.rows_accepted == 2
        assert outcome.rows_rejected == 0


def test_duplicate_date_fixture_is_rejected(tmp_path):
    generator = load_generator()
    generator.generate(tmp_path, symbols=1, days=2, seed=11)
    payload = (tmp_path / "synthetic_market_dirty.csv").read_bytes()
    job, outcome = ingest(payload, chunk_size=1)
    assert outcome.rows_received > outcome.rows_accepted
    found = codes(job)
    assert IssueCode.INVALID_OHLC_RANGE in found
    assert IssueCode.MISSING_VOLUME in found
