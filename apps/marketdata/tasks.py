"""Celery tasks. The task argument is a durable job identifier only."""

from celery import shared_task

from apps.marketdata.jobs.ingestion.dispatch import INGESTION_TASK_NAME
from apps.marketdata.jobs.ingestion.process import run_ingestion_job


@shared_task(name=INGESTION_TASK_NAME)
def process_ingestion_job(job_id: str) -> None:
    run_ingestion_job(job_id)
