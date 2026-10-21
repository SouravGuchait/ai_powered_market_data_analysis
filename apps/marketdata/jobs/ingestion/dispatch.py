"""Ingestion job dispatch and rollback for unaccepted uploads."""

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketdata.models import IngestionFile, IngestionJob
from apps.marketdata.storage import delete_stored_upload

logger = logging.getLogger(__name__)

INGESTION_TASK_NAME = "apps.marketdata.tasks.process_ingestion_job"


class IngestionEnqueueError(Exception):
    """Raised when a committed job cannot be handed to the queue."""


def dispatch_ingestion_job(job_id: str) -> str | None:
    """Send a durable job identifier to Celery and return its task id."""
    from config.celery import app

    result = app.send_task(INGESTION_TASK_NAME, args=[job_id])
    return result.id


def dispatch_pending_ingestion_jobs(batch_size: int | None = None) -> dict[str, int]:
    """Dispatch queued jobs without a recorded task ID in a locked bounded batch."""
    limit = batch_size or settings.INGESTION_DISPATCH_BATCH_SIZE
    eligible = IngestionJob.objects.filter(
        status=IngestionJob.Status.QUEUED, task_id__isnull=True
    )
    eligible_count = eligible.count()
    dispatched = 0
    failures = 0

    with transaction.atomic():
        jobs = list(
            eligible.order_by("requested_at", "pk")
            .select_for_update(skip_locked=True)[:limit]
        )
        for job in jobs:
            try:
                task_id = dispatch_ingestion_job(str(job.pk))
            except Exception as exc:  # noqa: BLE001 - broker errors leave jobs eligible
                failures += 1
                logger.warning(
                    "ingestion.scheduled_dispatch.failed job_id=%s error_type=%s",
                    job.pk,
                    type(exc).__name__,
                )
                continue

            job.task_id = task_id or str(uuid.uuid4())
            job.save(update_fields=["task_id", "updated_at"])
            dispatched += 1
            logger.info(
                "ingestion.scheduled_dispatch.succeeded job_id=%s task_id=%s",
                job.pk,
                job.task_id,
            )

    logger.info(
        "ingestion.scheduled_dispatch.finished eligible=%d dispatched=%d failures=%d batch_size=%d timestamp=%s",
        eligible_count,
        dispatched,
        failures,
        limit,
        timezone.now().isoformat(),
    )
    return {"eligible": eligible_count, "dispatched": dispatched, "failures": failures}


def enqueue_after_commit(job_id: uuid.UUID) -> None:
    """Defer dispatch until the job row is visible to workers."""
    transaction.on_commit(lambda: _enqueue(job_id))


def _enqueue(job_id: uuid.UUID) -> None:
    try:
        task_id = dispatch_ingestion_job(str(job_id))
    except Exception as exc:
        logger.exception("ingestion.enqueue.failed job_id=%s", job_id)
        try:
            _discard_unaccepted_job(job_id)
        except Exception:
            logger.exception("ingestion.enqueue.cleanup_failed job_id=%s", job_id)
        raise IngestionEnqueueError("The upload could not be queued.") from exc
    if task_id:
        try:
            IngestionJob.objects.filter(pk=job_id).update(task_id=task_id[:64])
        except Exception:
            logger.exception("ingestion.enqueue.task_id_update_failed job_id=%s", job_id)
    logger.info("ingestion.enqueue.succeeded job_id=%s task_id=%s", job_id, task_id)


def _discard_unaccepted_job(job_id: uuid.UUID) -> None:
    """Remove a job that was committed but never handed to a worker."""
    job = IngestionJob.objects.filter(pk=job_id).first()
    if job is None:
        return

    for stored_file in IngestionFile.objects.filter(ingestion_job=job):
        delete_stored_upload(stored_file.storage_key)

    job.delete()
    logger.warning("ingestion.enqueue.rolled_back job_id=%s", job_id)
