"""Ingestion job dispatch and rollback for unaccepted uploads."""

import logging
import uuid

from django.db import transaction

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
