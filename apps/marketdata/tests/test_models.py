"""Behavioral tests for market-data model constraints, policies, and indexes."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import ProtectedError
from django.db.utils import DataError, IntegrityError

from apps.marketdata.models import (
    AuditEvent,
    Exchange,
    IngestionFile,
    IngestionJob,
    Instrument,
    MetricSnapshot,
    PriceRecord,
    ValidationIssue,
)

pytestmark = pytest.mark.django_db


def price_kwargs(instrument, job, trading_date, **overrides):
    data = {
        "instrument": instrument,
        "ingestion_job": job,
        "trading_date": trading_date,
        "open": Decimal("98.100000"),
        "high": Decimal("101.900000"),
        "low": Decimal("97.800000"),
        "close": Decimal("100.500000"),
        "adjusted_close": Decimal("100.100000"),
        "volume": 1_500_000,
    }
    data.update(overrides)
    return data


def instrument_kwargs(exchange, **overrides):
    data = {
        "exchange": exchange,
        "symbol": "MSFT",
        "asset_class": "equity",
        "currency": "USD",
    }
    data.update(overrides)
    return data


def _assert_db_error(action, exc_type):
    """Run action inside a savepoint so failed statements roll back cleanly."""
    with pytest.raises(exc_type), transaction.atomic():
        action()


def _constraint_names(table):
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table)
    return set(constraints)


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        "owner", "owner@example.com", "pw-12345678", last_name="Owner"
    )


@pytest.fixture
def other_user():
    return get_user_model().objects.create_user(
        "other", "other@example.com", "pw-87654321", last_name="Other"
    )


@pytest.fixture
def exchange():
    return Exchange.objects.create(code="xnys", name="NYSE", timezone="America/New_York")


@pytest.fixture
def other_exchange():
    return Exchange.objects.create(code="xlon", name="LSE", timezone="Europe/London")


@pytest.fixture
def instrument(exchange):
    return Instrument.objects.create(
        exchange=exchange,
        symbol="aapl",
        name="Apple Inc.",
        asset_class="equity",
        currency="usd",
    )


@pytest.fixture
def job(user):
    return IngestionJob.objects.create(created_by=user, source_type="csv")


@pytest.fixture
def next_day():
    counter = {"next": 0}

    def factory():
        counter["next"] += 1
        return date(2024, 1, 1) + timedelta(days=counter["next"])

    return factory


@pytest.fixture
def record(instrument, job, next_day):
    return PriceRecord.objects.create(**price_kwargs(instrument, job, next_day()))


# --- Exchange and Instrument -------------------------------------------------


def test_exchange_code_normalized_and_unique(exchange):
    assert Exchange.objects.filter(code="XNYS").exists()
    _assert_db_error(
        lambda: Exchange.objects.create(code="XNYS", name="Dupe", timezone="UTC"),
        IntegrityError,
    )


def test_exchange_rejects_unknown_timezone_in_clean():
    exchange = Exchange(code="XBAD", name="Bad", timezone="Not/A_Zone")
    with pytest.raises(ValidationError) as excinfo:
        exchange.clean()
    assert "timezone" in excinfo.value.message_dict

    exchange.timezone = "Europe/London"
    exchange.clean()  # does not raise


def test_instrument_normalizes_symbol_and_currency(instrument):
    assert instrument.symbol == "AAPL"
    assert instrument.currency == "USD"


def test_instrument_symbol_unique_per_exchange(exchange, instrument):
    duplicate = Instrument(**instrument_kwargs(exchange, symbol="AAPL"))
    _assert_db_error(duplicate.save, IntegrityError)


def test_instrument_symbol_allowed_on_other_exchange(
    instrument, other_exchange, user
):
    Instrument.objects.create(**instrument_kwargs(other_exchange, symbol="AAPL"))
    assert Instrument.objects.filter(symbol="AAPL").count() == 2


def test_instrument_rejects_bad_currency_clean(exchange):
    bad = Instrument(**instrument_kwargs(exchange, symbol="TSLA", currency="US"))
    with pytest.raises(ValidationError) as excinfo:
        bad.clean()
    assert "currency" in excinfo.value.message_dict


# --- PriceRecord -------------------------------------------------------------


def test_price_record_roundtrip_at_documented_scale(instrument, job, next_day):
    record = PriceRecord.objects.create(
        **price_kwargs(
            instrument,
            job,
            next_day(),
            close=Decimal("123456789.123456"),
            low=Decimal("1.23456789"),
            high=Decimal("999999999999.999999"),
        )
    )
    record.refresh_from_db()
    assert record.close == Decimal("123456789.123456")
    assert record.low == Decimal("1.234568")  # extra decimals round at the 6-dp scale
    assert record.high == Decimal("999999999999.999999")


def test_price_record_rejects_duplicate_instrument_date(record, instrument, job):
    _assert_db_error(
        lambda: PriceRecord.objects.create(
            **price_kwargs(
                instrument, job, record.trading_date, open=Decimal("99.000000")
            )
        ),
        IntegrityError,
    )


def test_price_record_ohlc_shape_and_positive_checks(instrument, job, next_day):
    bad_shape = price_kwargs(instrument, job, next_day(), high=Decimal("90.000000"))
    _assert_db_error(lambda: PriceRecord.objects.create(**bad_shape), IntegrityError)

    zeroed = price_kwargs(
        instrument,
        job,
        next_day(),
        open=Decimal("0.000000"),
        low=Decimal("0.000000"),
        high=Decimal("0.000000"),
        close=Decimal("0.000000"),
    )
    _assert_db_error(lambda: PriceRecord.objects.create(**zeroed), IntegrityError)

    negative_close = price_kwargs(instrument, job, next_day(), close=Decimal("-1.000000"))
    _assert_db_error(lambda: PriceRecord.objects.create(**negative_close), IntegrityError)

    negative_adjusted = price_kwargs(
        instrument, job, next_day(), adjusted_close=Decimal("-0.000001")
    )
    _assert_db_error(
        lambda: PriceRecord.objects.create(**negative_adjusted), IntegrityError
    )


def test_price_record_adjusted_close_optional(instrument, job, next_day):
    record = PriceRecord.objects.create(
        **price_kwargs(instrument, job, next_day(), adjusted_close=None)
    )
    assert record.adjusted_close is None


def test_price_record_volume_bounds(instrument, job, next_day):
    _assert_db_error(
        lambda: PriceRecord.objects.create(
            **price_kwargs(instrument, job, next_day(), volume=-1)
        ),
        DataError,
    )

    zero = PriceRecord.objects.create(
        **price_kwargs(instrument, job, next_day(), volume=0)
    )
    zero.refresh_from_db()
    assert zero.volume == 0

    huge = PriceRecord.objects.create(
        **price_kwargs(instrument, job, next_day(), volume=2**40)
    )
    huge.refresh_from_db()
    assert huge.volume == 2**40


def test_price_record_price_overflow(instrument, job, next_day):
    overflowing = price_kwargs(
        instrument, job, next_day(), close=Decimal("99999999999999.9")
    )
    _assert_db_error(lambda: PriceRecord.objects.create(**overflowing), DataError)


def test_price_record_indexes_exist(record):
    names = _constraint_names(PriceRecord._meta.db_table)
    assert {"md_price_daily_unique", "md_price_ohlc_shape", "md_price_positive"} <= names


# --- IngestionJob ------------------------------------------------------------


def test_job_defaults(job, user):
    assert job.created_by == user
    assert job.status == IngestionJob.Status.QUEUED
    assert job.processing_version == "1"
    assert job.rows_received == job.rows_accepted == job.rows_rejected == 0
    assert job.requested_at is not None
    assert job.completed_at is None
    assert not job.is_terminal


def test_job_rejects_unsupported_source_type(user):
    job = IngestionJob(created_by=user, source_type="parquet")
    with pytest.raises(ValidationError):
        job.full_clean()


def test_job_counters_non_negative(job):
    job.rows_received = 10
    job.rows_accepted = -1
    _assert_db_error(job.save, DataError)

    job.refresh_from_db()
    job.rows_accepted = 0
    job.rows_rejected = -3
    _assert_db_error(job.save, DataError)


def test_job_counters_bounded_by_rows_received(job, record):
    job.refresh_from_db()
    job.rows_received = 2  # one canonical price record already attached
    job.rows_accepted = 3
    _assert_db_error(job.save, IntegrityError)

    job.refresh_from_db()
    job.rows_accepted = 0
    job.rows_rejected = 5
    _assert_db_error(job.save, IntegrityError)

    job.refresh_from_db()
    job.rows_accepted = 2
    job.rows_rejected = 0
    job.rows_received = 2
    job.save()  # consistent counts are accepted


def test_job_terminal_requires_completed_at_db_level(user, other_user):
    failed = IngestionJob(created_by=user, source_type="csv", status=IngestionJob.Status.FAILED)
    _assert_db_error(failed.save, IntegrityError)

    cancelled_ok = IngestionJob(
        created_by=other_user,
        source_type="json",
        status=IngestionJob.Status.CANCELLED,
        completed_at="2024-01-01 10:00:00+00:00",
    )
    cancelled_ok.save()
    cancelled_ok.refresh_from_db()
    assert cancelled_ok.completed_at is not None


def test_job_running_requires_started_at_db_level(user):
    running = IngestionJob(created_by=user, source_type="csv", status=IngestionJob.Status.RUNNING)
    _assert_db_error(running.save, IntegrityError)


def test_job_clean_enforces_legal_transitions(job):
    job.status = IngestionJob.Status.RUNNING
    with pytest.raises(ValidationError) as excinfo:
        job.clean()
    assert "started_at" in excinfo.value.message_dict

    job.started_at = job.requested_at
    job.status = IngestionJob.Status.COMPLETED
    with pytest.raises(ValidationError) as excinfo:
        job.clean()
    assert "completed_at" in excinfo.value.message_dict


def test_job_assert_transition(job):
    job.assert_transition(IngestionJob.Status.RUNNING)
    job.status = IngestionJob.Status.RUNNING
    job.assert_transition(IngestionJob.Status.FAILED)
    job.status = IngestionJob.Status.FAILED
    with pytest.raises(ValueError):
        job.assert_transition(IngestionJob.Status.QUEUED)
    with pytest.raises(ValueError):
        job.assert_transition(IngestionJob.Status.RUNNING)
    job.assert_transition(IngestionJob.Status.FAILED)  # same-status is a no-op


def test_job_idempotency_unique_per_owner(user, other_user):
    first = IngestionJob.objects.create(
        created_by=user, source_type="csv", idempotency_key="up-0001"
    )
    _assert_db_error(
        lambda: IngestionJob.objects.create(
            created_by=user, source_type="csv", idempotency_key="up-0001"
        ),
        IntegrityError,
    )
    # Same key from another owner is legal; owner scoping is enforced.
    from_other = IngestionJob.objects.create(
        created_by=other_user, source_type="csv", idempotency_key="up-0001"
    )
    assert from_other.created_by == other_user

    # Multiple jobs without a key remain legal per owner.
    no_key = IngestionJob.objects.create(created_by=user, source_type="json")
    assert str(no_key.id) != str(first.id)
    new = IngestionJob.objects.create(created_by=user, source_type="xlsx")
    assert new.idempotency_key is None


def test_job_ownership_filter_ordering(user, other_user):
    from django.utils import timezone

    second = IngestionJob.objects.create(
        created_by=user, source_type="json", requested_at=timezone.now()
    )
    theirs = IngestionJob.objects.create(created_by=other_user, source_type="csv")
    mine = IngestionJob.objects.filter(created_by=user).order_by("requested_at")
    assert second in mine
    assert theirs not in mine
    ids = [str(value.id) for value in mine]
    assert ids.index(str(second.id)) >= len(ids) - 1


# --- Ownership and deletion policies ----------------------------------------


def test_user_deletion_preserves_jobs_and_audit(user, job):
    AuditEvent.objects.create(user=user, action="upload.request", object_type="job", object_id=str(job.id))
    user.delete()
    job.refresh_from_db()
    assert job.created_by is None
    event = AuditEvent.objects.get(object_id=str(job.id))
    assert event.user is None


def test_job_delete_cascades_files_and_issues_but_not_prices(record, job):
    file = IngestionFile.objects.create(
        ingestion_job=job,
        original_filename="prices.csv",
        storage_key="2026/01/prices-uuid.csv",
        checksum="a" * 64,
        content_type="text/csv",
        file_size_bytes=128,
    )
    ValidationIssue.objects.create(
        ingestion_job=job, row_number=3, error_code="bad_date", message="Bad date"
    )
    with pytest.raises(ProtectedError):
        job.delete()
    assert file.ingestion_job_id == job.id

    record.delete()
    job.delete()
    assert IngestionFile.objects.filter(pk=file.id).count() == 0
    assert ValidationIssue.objects.filter(ingestion_job_id=job.id).count() == 0


def test_ingestion_file_size_supports_above_32_bits(job):
    file = IngestionFile.objects.create(
        ingestion_job=job,
        original_filename="large.csv",
        storage_key="uploads/1/2/large.csv",
        checksum="b" * 64,
        content_type="text/csv",
        file_size_bytes=5 * 1024**3,
    )

    file.refresh_from_db()

    assert file.file_size_bytes == 5 * 1024**3


def test_instrument_deletion_protected(record, instrument, exchange):
    with pytest.raises(ProtectedError) as excinfo:
        instrument.delete()
    assert "pricerecord" in str(excinfo.value).lower()

    record.delete()
    MetricSnapshot.objects.create(
        instrument=instrument,
        as_of_date="2024-01-05",
        metric_type=MetricSnapshot.MetricType.DAILY_RETURN,
        value=Decimal("0.00100000"),
        calculation_version="1",
    )
    with pytest.raises(ProtectedError):
        instrument.delete()


def test_exchange_deletion_protected(instrument, exchange):
    with pytest.raises(ProtectedError):
        exchange.delete()


# --- ValidationIssue --------------------------------------------------------


def test_validation_issue_multiple_per_row(job):
    first = ValidationIssue.objects.create(
        ingestion_job=job,
        row_number=2,
        field_name="close",
        error_code="missing_close",
        message="close is missing",
    )
    second = ValidationIssue.objects.create(
        ingestion_job=job,
        row_number=2,
        field_name="volume",
        error_code="negative_volume",
        message="volume is negative",
    )
    job_level = ValidationIssue.objects.create(
        ingestion_job=job, row_number=None, field_name=None,
        error_code="empty_file", message="No rows found",
    )
    assert first.row_number == second.row_number
    assert ValidationIssue.objects.filter(ingestion_job=job).count() == 3
    assert job_level.raw_row is None


def test_validation_issue_indexes_exist(job):
    ValidationIssue.objects.create(ingestion_job=job, error_code="c", message="m")
    names = _constraint_names(ValidationIssue._meta.db_table)
    assert {"md_issue_job_row", "md_issue_job_code"} <= names


# --- AuditEvent -------------------------------------------------------------


def test_audit_event_defaults_and_system_generated(user, job):
    event = AuditEvent.objects.create(
        user=None,
        action="ingestion.failed",
        object_type="ingestion_job",
        object_id=str(job.id),
        request_id="req-abc",
        metadata={"error_code": "storage_unavailable"},
    )
    event.refresh_from_db()
    assert event.user is None
    assert event.created_at is not None
    assert event.metadata == {"error_code": "storage_unavailable"}

    named = AuditEvent.objects.create(
        user=user, action="download.report", object_type="report", object_id="1"
    )
    assert named.user == user


def test_audit_event_indexes_exist(user, job):
    AuditEvent.objects.create(user=user, action="a", object_type="t", object_id="1")
    names = _constraint_names(AuditEvent._meta.db_table)
    assert {"md_audit_user_created", "md_audit_object"} <= names


# --- MetricSnapshot ---------------------------------------------------------


def metric_kwargs(instrument, **overrides):
    data = {
        "instrument": instrument,
        "as_of_date": date(2024, 1, 31),
        "metric_type": MetricSnapshot.MetricType.ROLLING_VOLATILITY,
        "lookback_days": 20,
        "value": Decimal("0.01000000"),
        "calculation_version": "1",
    }
    data.update(overrides)
    return data


def test_metric_snapshot_unique_natural_key(instrument, other_exchange):
    MetricSnapshot.objects.create(**metric_kwargs(instrument))
    _assert_db_error(
        lambda: MetricSnapshot.objects.create(**metric_kwargs(instrument)),
        IntegrityError,
    )
    # A different lookback window or calculation version is a distinct metric.
    other_window = MetricSnapshot.objects.create(
        **metric_kwargs(instrument, lookback_days=60)
    )
    assert other_window.lookback_days == 60
    MetricSnapshot.objects.create(**metric_kwargs(instrument, calculation_version="2"))
    other = Instrument.objects.create(
        exchange=other_exchange, symbol="MSFT", asset_class="equity", currency="USD"
    )
    MetricSnapshot.objects.create(**metric_kwargs(other))


def test_metric_snapshot_windowless_metric_uses_sentinel(instrument):
    point = MetricSnapshot.objects.create(
        **metric_kwargs(
            instrument,
            lookback_days=0,
            metric_type=MetricSnapshot.MetricType.DAILY_RETURN,
        )
    )
    assert point.lookback_days == 0
    _assert_db_error(
        lambda: MetricSnapshot.objects.create(
            **metric_kwargs(
                instrument,
                lookback_days=0,
                metric_type=MetricSnapshot.MetricType.DAILY_RETURN,
            )
        ),
        IntegrityError,
    )


def test_metric_snapshot_value_must_be_storeable(instrument):
    MetricSnapshot.objects.create(
        **metric_kwargs(instrument, value=Decimal("0.00000001"))
    )
    fetched = MetricSnapshot.objects.get(
        instrument=instrument, as_of_date=date(2024, 1, 31)
    )
    assert fetched.value == Decimal("0.00000001")


def test_metric_snapshot_indexes_exist(instrument):
    names = _constraint_names(MetricSnapshot._meta.db_table)
    assert "md_metric_snapshot_unique" in names
    assert "md_metric_type_date" in names


# --- IngestionFile ----------------------------------------------------------


def test_ingestion_file_requires_job_and_indexes_exist(job):
    file = IngestionFile.objects.create(
        ingestion_job=job,
        original_filename="data.csv",
        storage_key="files/2026/10/data.csv",
        checksum="f" * 64,
    )
    file.refresh_from_db()
    assert file.created_at is not None
    names = _constraint_names(IngestionFile._meta.db_table)
    assert "md_file_checksum" in names


def test_job_status_index_exists(job):
    names = _constraint_names(IngestionJob._meta.db_table)
    assert {"md_job_status", "md_job_owner_requested", "md_job_idempotency_unique"} <= names
