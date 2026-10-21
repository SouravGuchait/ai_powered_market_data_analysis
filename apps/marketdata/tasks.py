"""Celery tasks. The task argument is a durable job identifier only."""

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.marketdata.jobs.ingestion.dispatch import (
    INGESTION_TASK_NAME,
    dispatch_pending_ingestion_jobs,
)
from apps.marketdata.jobs.ingestion.process import run_ingestion_job

logger = logging.getLogger(__name__)


@shared_task(name=INGESTION_TASK_NAME)
def process_ingestion_job(job_id: str) -> None:
    run_ingestion_job(job_id)


@shared_task(name="apps.marketdata.tasks.dispatch_pending_ingestion_jobs")
def scheduled_ingestion_dispatch() -> dict[str, int]:
    """Enqueue bounded, previously undispatched jobs using durable IDs only."""
    return dispatch_pending_ingestion_jobs(
        batch_size=settings.INGESTION_DISPATCH_BATCH_SIZE
    )


@shared_task(name="apps.marketdata.tasks.worker_heartbeat")
def worker_heartbeat() -> str:
    """Emit a periodic log proving Beat can dispatch work to a worker."""
    logger.info(
        "celery.worker.heartbeat task_id=%s timestamp=%s",
        worker_heartbeat.task_id,
        timezone.now().isoformat(),
    )
    return "ok"
