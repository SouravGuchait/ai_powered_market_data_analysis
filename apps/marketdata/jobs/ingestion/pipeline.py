"""Bounded dataset construction: parse, normalize, persist, repeat."""

import logging
import time
from dataclasses import dataclass

from django.conf import settings

from apps.marketdata.ingestion.metrics import MetricCalculator
from apps.marketdata.ingestion.normalize import normalize_chunk
from apps.marketdata.ingestion.parsing import iter_input_chunks
from apps.marketdata.ingestion.schema import InputError, IssueCode
from apps.marketdata.jobs.ingestion.metrics import DailyReturnCalculator
from apps.marketdata.jobs.ingestion.persist import (
    InstrumentResolver,
    persist_issues,
    persist_records,
)
from apps.marketdata.models import IngestionJob, PriceRecord, ValidationIssue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionOutcome:
    rows_received: int
    rows_accepted: int
    rows_rejected: int
    issues: int
    duration_ms: int


def build_dataset(
    job: IngestionJob,
    file_obj,
    *,
    source_type: str,
    chunk_size: int | None = None,
    batch_size: int | None = None,
    metric_calculator: MetricCalculator | None = None,
) -> IngestionOutcome:
    """Consume chunks and persist each one before reading the next.

    `chunk_size` bounds the frame handed to pandas; `batch_size` bounds each
    `bulk_create`. The final partial batch is written by the same call.
    """
    if chunk_size is None:
        chunk_size = int(settings.INGESTION_CHUNK_SIZE)
    if batch_size is None:
        batch_size = int(settings.INGESTION_DB_BATCH_SIZE)
    if chunk_size < 1 or batch_size < 1:
        raise ValueError("chunk_size and batch_size must be positive")

    started = time.monotonic()
    # A repeated delivery must not append a second copy of the same rows.
    PriceRecord.objects.filter(ingestion_job=job).delete()
    ValidationIssue.objects.filter(ingestion_job=job).delete()
    IngestionJob.objects.filter(pk=job.pk).update(
        rows_received=0, rows_accepted=0, rows_rejected=0
    )

    resolver = InstrumentResolver()
    default_exchange = settings.INGESTION_DEFAULT_EXCHANGE
    default_timezone = settings.INGESTION_DEFAULT_EXCHANGE_TIMEZONE
    resolver.ensure_exchange(default_exchange, default_timezone)

    received = 0
    issue_count = 0
    logger.info(
        "ingestion.dataset.started job_id=%s source_type=%s chunk_size=%d batch_size=%d",
        job.pk,
        source_type,
        chunk_size,
        batch_size,
    )
    for chunk in iter_input_chunks(file_obj, source_type, chunk_size):
        result = normalize_chunk(
            chunk.frame,
            chunk.row_numbers,
            exchanges=resolver.timezones(),
            default_exchange=default_exchange,
            default_timezone=default_timezone,
            structural_issues=chunk.issues,
        )
        received += result.received
        _, duplicates = persist_records(
            job, result.records, resolver, batch_size=batch_size
        )
        issue_count += persist_issues(
            job, [*result.issues, *duplicates], batch_size=batch_size
        )
        IngestionJob.objects.filter(pk=job.pk).update(rows_received=received)
        logger.debug(
            "ingestion.dataset.chunk job_id=%s received=%d accepted_chunk=%d issues_chunk=%d",
            job.pk,
            received,
            len(result.records),
            len(result.issues) + len(duplicates),
        )

    if received == 0:
        raise InputError(IssueCode.EMPTY_FILE, "The file has no data rows.")

    accepted = PriceRecord.objects.filter(ingestion_job=job).count()
    rejected = received - accepted
    IngestionJob.objects.filter(pk=job.pk).update(
        rows_received=received,
        rows_accepted=accepted,
        rows_rejected=rejected,
    )
    (metric_calculator or DailyReturnCalculator()).on_job_completed(job=job)

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "ingestion.dataset.completed job_id=%s received=%d accepted=%d rejected=%d issues=%d duration_ms=%d",
        job.pk,
        received,
        accepted,
        rejected,
        issue_count,
        duration_ms,
    )
    return IngestionOutcome(
        rows_received=received,
        rows_accepted=accepted,
        rows_rejected=rejected,
        issues=issue_count,
        duration_ms=duration_ms,
    )
