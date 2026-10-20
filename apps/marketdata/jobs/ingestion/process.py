"""Worker entrypoint: claim a queued job, build its dataset, mark the outcome."""

import logging
import time
import uuid

from django.db import transaction
from django.utils import timezone

from apps.marketdata import storage
from apps.marketdata.ingestion.schema import InputError, IssueCode
from apps.marketdata.jobs.ingestion.pipeline import IngestionOutcome, build_dataset
from apps.marketdata.models import IngestionFile, IngestionJob, PriceRecord

logger = logging.getLogger(__name__)


def run_ingestion_job(job_id: str | uuid.UUID) -> IngestionOutcome | None:
    """Process one durable job. Terminal and running jobs are not reprocessed."""
    job = IngestionJob.objects.filter(pk=job_id).first()
    if job is None:
        logger.warning("ingestion.job.missing job_id=%s", job_id)
        return None
    if job.status != IngestionJob.Status.QUEUED:
        logger.info(
            "ingestion.job.skipped job_id=%s status=%s", job.pk, job.status
        )
        return None

    claimed = IngestionJob.objects.filter(
        pk=job.pk, status=IngestionJob.Status.QUEUED
    ).update(
        status=IngestionJob.Status.RUNNING,
        started_at=timezone.now(),
        error_code=None,
        error_summary="",
    )
    if not claimed:
        logger.info("ingestion.job.claim_lost job_id=%s", job.pk)
        return None
    job.refresh_from_db()
    logger.info("ingestion.job.started job_id=%s source_type=%s", job.pk, job.source_type)

    stored_file = IngestionFile.objects.filter(ingestion_job=job).first()
    if stored_file is None:
        _fail(job, IssueCode.FILE_MISSING, "The job has no stored file.")
        return None

    handle = None
    started = time.monotonic()
    try:
        handle = storage.get_upload_storage().open(stored_file.storage_key, "rb")
        outcome = build_dataset(job, handle, source_type=job.source_type)
    except InputError as exc:
        logger.warning(
            "ingestion.job.rejected job_id=%s code=%s duration_ms=%d",
            job.pk,
            exc.code,
            int((time.monotonic() - started) * 1000),
        )
        _fail(job, exc.code, exc.summary)
        return None
    except FileNotFoundError:
        logger.warning("ingestion.job.file_missing job_id=%s", job.pk)
        _fail(job, IssueCode.FILE_MISSING, "The stored file is unavailable.")
        return None
    except Exception:
        logger.exception(
            "ingestion.job.failed job_id=%s code=%s duration_ms=%d",
            job.pk,
            IssueCode.PROCESSING_ERROR,
            int((time.monotonic() - started) * 1000),
        )
        _fail(
            job,
            IssueCode.PROCESSING_ERROR,
            "The file could not be processed.",
        )
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the outcome
                logger.warning("ingestion.cleanup.close_failed job_id=%s", job.pk)
        _delete_stored_files(job)

    _complete(job, outcome)
    return outcome


def _complete(job: IngestionJob, outcome: IngestionOutcome) -> None:
    updated = IngestionJob.objects.filter(
        pk=job.pk, status=IngestionJob.Status.RUNNING
    ).update(
        status=IngestionJob.Status.COMPLETED,
        completed_at=timezone.now(),
        rows_received=outcome.rows_received,
        rows_accepted=outcome.rows_accepted,
        rows_rejected=outcome.rows_rejected,
        error_code=None,
        error_summary="",
    )
    if not updated:
        logger.warning("ingestion.job.completion_lost job_id=%s", job.pk)
        return
    logger.info(
        "ingestion.job.completed job_id=%s received=%d accepted=%d rejected=%d duration_ms=%d",
        job.pk,
        outcome.rows_received,
        outcome.rows_accepted,
        outcome.rows_rejected,
        outcome.duration_ms,
    )


def _fail(job: IngestionJob, code: str, summary: str) -> None:
    """Remove partial rows, then record a terminal failure with safe counts."""
    with transaction.atomic():
        PriceRecord.objects.filter(ingestion_job=job).delete()
        received = (
            IngestionJob.objects.filter(pk=job.pk)
            .values_list("rows_received", flat=True)
            .first()
            or 0
        )
        IngestionJob.objects.filter(
            pk=job.pk, status=IngestionJob.Status.RUNNING
        ).update(
            status=IngestionJob.Status.FAILED,
            completed_at=timezone.now(),
            rows_accepted=0,
            rows_rejected=received,
            error_code=code[:32],
            error_summary=summary[:255],
        )
    logger.warning(
        "ingestion.job.marked_failed job_id=%s code=%s received=%d",
        job.pk,
        code,
        received,
    )


def _delete_stored_files(job: IngestionJob) -> None:
    for stored_file in IngestionFile.objects.filter(ingestion_job=job):
        try:
            storage.delete_stored_upload(stored_file.storage_key)
        except Exception:  # noqa: BLE001 - cleanup must not mask the outcome
            logger.warning("ingestion.cleanup.failed job_id=%s", job.pk)
