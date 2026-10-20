"""Durable market-data schema: jobs, files, instruments, prices, issues, audit.

Policies implemented here are documented in DATA_MODEL.md and README.md:
- PriceRecord keeps one canonical daily row per (instrument, trading_date);
  conflicting imports are rejected, never silently overwritten.
- Ingestion job counters are non-negative and MySQL CHECK constraints bound
  accepted/rejected counts by rows_received.
- Decimal storage is used for every price and metric value; no float mixing.
"""

import uuid
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

# Daily OHLC prices: 12 integer digits plus 6 fractional digits. The scale is
# what files and analytics must honor; writers round, they never mix floats.
PRICE_MAX_DIGITS = 18
PRICE_DECIMAL_PLACES = 6

# Metric fractions such as returns or volatility need headroom below 1e-8.
METRIC_MAX_DIGITS = 20
METRIC_DECIMAL_PLACES = 8

# Sentinel for metrics that are not window-based: unique constraints must not
# rely on NULL-vs-NULL duplicates, so non-windowed metrics use 0.
NO_LOOKBACK_DAYS = 0


class Exchange(models.Model):
    """Trading venue. `code` is the canonical uppercase identifier (XNYS)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=64)
    # Valid IANA identifier, checked in application validation.
    timezone = models.CharField(max_length=64)

    class Meta:
        ordering = ("code",)

    def __str__(self) -> str:
        return f"{self.code} ({self.timezone})"

    def clean(self) -> None:
        try:
            ZoneInfo(self.timezone or "")
        except (ValueError, KeyError):
            raise ValidationError({"timezone": "Unknown IANA timezone."}) from None
        self.code = (self.code or "").strip().upper()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)


class Instrument(models.Model):
    """Tradeable instrument; unique per exchange, never across exchanges."""

    class AssetClass(models.TextChoices):
        EQUITY = "equity"
        ETF = "etf"
        CURRENCY = "currency"
        COMMODITY = "commodity"
        CRYPTO = "crypto"

    exchange = models.ForeignKey(
        Exchange, on_delete=models.PROTECT, related_name="instruments"
    )
    symbol = models.CharField(max_length=32)
    name = models.CharField(max_length=128, blank=True, default="")
    asset_class = models.CharField(max_length=16, choices=AssetClass.choices)
    currency = models.CharField(max_length=3)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("symbol",)
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["exchange", "symbol"],
                name="md_instrument_symbol_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} @ {self.exchange.code}"

    def clean(self) -> None:
        self.symbol = (self.symbol or "").strip().upper()
        self.currency = (self.currency or "").strip().upper()
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValidationError(
                {"currency": "Currency must be a 3-letter ISO 4217 code."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.symbol = (self.symbol or "").strip().upper()
        self.currency = (self.currency or "").strip().upper()
        super().save(*args, **kwargs)


class PriceRecord(models.Model):
    """Canonical daily OHLCV row for one instrument and trading date.

    Duplicate policy: reject on conflict against the existing canonical row
    (an ingestion issue is recorded by the ingestion layer, milestone 04).
    """

    instrument = models.ForeignKey(
        Instrument, on_delete=models.PROTECT, related_name="price_records"
    )
    ingestion_job = models.ForeignKey(
        "IngestionJob",
        on_delete=models.PROTECT,
        related_name="price_records",
    )
    trading_date = models.DateField()
    open = models.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    high = models.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    low = models.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    close = models.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
    )
    adjusted_close = models.DecimalField(
        max_digits=PRICE_MAX_DIGITS,
        decimal_places=PRICE_DECIMAL_PLACES,
        null=True,
        blank=True,
    )
    volume = models.PositiveBigIntegerField()
    source = models.CharField(max_length=32, default="upload")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("instrument", "-trading_date")
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["instrument", "trading_date"],
                name="md_price_daily_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    open__gt=0, high__gt=0, low__gt=0, close__gt=0
                ),
                name="md_price_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(low__lte=models.F("open"))
                & models.Q(open__lte=models.F("high"))
                & models.Q(low__lte=models.F("close"))
                & models.Q(close__lte=models.F("high")),
                name="md_price_ohlc_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(adjusted_close__gt=0)
                | models.Q(adjusted_close__isnull=True),
                name="md_price_adjusted_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.instrument.symbol} {self.trading_date}"


class IngestionJob(models.Model):
    """One upload/import execution. Authoritative state lives in MySQL."""

    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"

    class SourceType(models.TextChoices):
        CSV = "csv"
        JSON = "json"
        XLSX = "xlsx"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ingestion_jobs",
    )
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    rows_received = models.PositiveBigIntegerField(default=0)
    rows_accepted = models.PositiveBigIntegerField(default=0)
    rows_rejected = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=32, null=True, blank=True)
    error_summary = models.CharField(max_length=255, blank=True, default="")
    task_id = models.CharField(max_length=64, null=True, blank=True)
    processing_version = models.CharField(max_length=16, default="1")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    TERMINAL_STATUSES = frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED})
    LEGAL_TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        Status.QUEUED: frozenset({Status.RUNNING, Status.CANCELLED}),
        Status.RUNNING: frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED}),
        Status.COMPLETED: frozenset(),
        Status.FAILED: frozenset(),
        Status.CANCELLED: frozenset(),
    }

    class Meta:
        ordering = ("-requested_at",)
        indexes = [  # noqa: RUF012
            # Owner job list ordered by requested_at (recent jobs endpoint).
            models.Index(
                fields=["created_by", "requested_at"],
                name="md_job_owner_requested",
            ),
            # Worker claim of queued jobs and admin status filtering.
            models.Index(fields=["status"], name="md_job_status"),
        ]
        constraints = [  # noqa: RUF012
            # MySQL unique indexes treat NULLs as distinct, so multiple rows
            # without an idempotency key remain legal per owner.
            models.UniqueConstraint(
                fields=["created_by", "idempotency_key"],
                name="md_job_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(rows_received__gte=models.F("rows_accepted")),
                name="md_job_rows_accepted",
            ),
            models.CheckConstraint(
                condition=models.Q(rows_received__gte=models.F("rows_rejected")),
                name="md_job_rows_rejected",
            ),
            models.CheckConstraint(
                condition=models.Q(started_at__isnull=False)
                | models.Q(status__in=["queued", "completed", "failed", "cancelled"]),
                name="md_job_running_started",
            ),
            models.CheckConstraint(
                condition=models.Q(completed_at__isnull=False)
                | models.Q(status__in=["queued", "running"]),
                name="md_job_terminal_completed",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.id} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in {value for value in type(self).TERMINAL_STATUSES}

    def clean(self) -> None:
        """Validate lifecycle consistency for surfaces that call full_clean."""
        errors: dict[str, str] = {}
        if self.status == self.Status.RUNNING and self.started_at is None:
            errors["started_at"] = "Running jobs require started_at."
        if self.is_terminal and self.completed_at is None:
            errors["completed_at"] = "Terminal jobs require completed_at."
        if self.rows_accepted > self.rows_received:
            errors["rows_accepted"] = "rows_accepted cannot exceed rows_received."
        if self.rows_rejected > self.rows_received:
            errors["rows_rejected"] = "rows_rejected cannot exceed rows_received."
        if errors:
            raise ValidationError(errors)

    def assert_transition(self, new_status: str) -> None:
        """Raise ValueError when the current status may not move to new_status."""
        if self.status != new_status and new_status not in type(self).LEGAL_TRANSITIONS[
            self.status
        ]:
            raise ValueError(
                f"Illegal status transition {self.status} -> {new_status}"
            )


class IngestionFile(models.Model):
    """File metadata for a job. Files live in configured storage, never here."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingestion_job = models.ForeignKey(
        IngestionJob, on_delete=models.CASCADE, related_name="files"
    )
    original_filename = models.CharField(max_length=255)
    # Storage-relative key; never an absolute machine path or API payload.
    storage_key = models.CharField(max_length=255)
    # Lowercase SHA-256 hex digest of the stored file content.
    checksum = models.CharField(max_length=64)
    content_type = models.CharField(max_length=64, blank=True, default="")
    file_size_bytes = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [  # noqa: RUF012
            # Duplicate-upload detection by content hash.
            models.Index(fields=["checksum"], name="md_file_checksum"),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.ingestion_job.pk})"


class ValidationIssue(models.Model):
    """Row- or field-level data-quality issue raised during ingestion."""

    ingestion_job = models.ForeignKey(
        IngestionJob, on_delete=models.CASCADE, related_name="validation_issues"
    )
    row_number = models.PositiveBigIntegerField(null=True, blank=True)
    field_name = models.CharField(max_length=64, null=True, blank=True)
    error_code = models.CharField(max_length=64)
    message = models.CharField(max_length=255)
    # Bounded JSON preview of the offending row; the ingestion layer limits
    # size and strips private columns (milestone 05 policy).
    raw_row = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("ingestion_job", "row_number", "id")
        indexes = [  # noqa: RUF012
            # Job detail page renders issues ordered by row number.
            models.Index(
                fields=["ingestion_job", "row_number"], name="md_issue_job_row"
            ),
            # Issue summary by validation rule per job.
            models.Index(
                fields=["ingestion_job", "error_code"], name="md_issue_job_code"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.error_code} @ row {self.row_number}"


class AuditEvent(models.Model):
    """Security-relevant or operationally significant action record."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64)
    object_id = models.CharField(max_length=64)
    request_id = models.CharField(max_length=64, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [  # noqa: RUF012
            # Per-user audit timeline queries.
            models.Index(
                fields=["user", "created_at"], name="md_audit_user_created"
            ),
            # Investigations by target object.
            models.Index(
                fields=["object_type", "object_id"], name="md_audit_object"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.object_type}:{self.object_id}"


class MetricSnapshot(models.Model):
    """Persisted analytical result. Units are fractions unless stated otherwise.

    lookback_days is normalized: 0 means the metric has no lookback window, so
    the unique key never has to compare NULLs.
    """

    class MetricType(models.TextChoices):
        DAILY_RETURN = "daily_return"
        MOVING_AVERAGE = "moving_average"
        ROLLING_VOLATILITY = "rolling_volatility"
        MAX_DRAWDOWN = "max_drawdown"

    instrument = models.ForeignKey(
        Instrument, on_delete=models.PROTECT, related_name="metric_snapshots"
    )
    as_of_date = models.DateField()
    metric_type = models.CharField(max_length=32, choices=MetricType.choices)
    lookback_days = models.PositiveIntegerField(default=NO_LOOKBACK_DAYS)
    value = models.DecimalField(
        max_digits=METRIC_MAX_DIGITS, decimal_places=METRIC_DECIMAL_PLACES
    )
    metadata = models.JSONField(default=dict, blank=True)
    calculation_version = models.CharField(max_length=16)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("instrument", "-as_of_date", "metric_type")
        indexes = [  # noqa: RUF012
            # Instrument/metric/date-range analytics queries.
            models.Index(
                fields=["instrument", "metric_type", "as_of_date"],
                name="md_metric_type_date",
            ),
        ]
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=[
                    "instrument",
                    "as_of_date",
                    "metric_type",
                    "lookback_days",
                    "calculation_version",
                ],
                name="md_metric_snapshot_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.metric_type} {self.instrument.symbol} {self.as_of_date}"
