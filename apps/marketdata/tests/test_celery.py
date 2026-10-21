from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone

from apps.marketdata.jobs.ingestion import dispatch as ingestion_dispatch
from apps.marketdata.models import IngestionJob
from apps.marketdata.tasks import worker_heartbeat
from config.celery import app

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        "celery-owner", "celery-owner@example.com", "pw-12345678"
    )


@pytest.fixture
def job(user):
    return IngestionJob.objects.create(created_by=user, source_type="csv")


def test_worker_heartbeat_emits_a_worker_log(caplog):
    assert worker_heartbeat.run() == "ok"
    assert "celery.worker.heartbeat" in caplog.text
    assert "task_id=None" in caplog.text


def test_beat_schedules_the_worker_heartbeat():
    schedule = app.conf.beat_schedule["marketdata-worker-heartbeat"]
    assert schedule["task"] == "apps.marketdata.tasks.worker_heartbeat"
    assert schedule["schedule"] == 60.0


def test_periodic_ingestion_dispatch_is_disabled_in_tests():
    assert settings.INGESTION_DISPATCH_INTERVAL_SECONDS == 0
    assert "marketdata-pending-ingestion-dispatch" not in app.conf.beat_schedule


def test_dispatcher_selects_only_undispatched_queued_jobs_and_respects_limit(
    monkeypatch, user
):
    pending = [
        IngestionJob.objects.create(created_by=user, source_type="csv")
        for _ in range(3)
    ]
    IngestionJob.objects.create(
        created_by=user,
        source_type="csv",
        status=IngestionJob.Status.COMPLETED,
        completed_at=timezone.now(),
        task_id=None,
    )
    IngestionJob.objects.create(
        created_by=user,
        source_type="csv",
        status=IngestionJob.Status.FAILED,
        completed_at=timezone.now(),
        task_id=None,
    )
    IngestionJob.objects.create(
        created_by=user,
        source_type="csv",
        status=IngestionJob.Status.RUNNING,
        started_at=timezone.now(),
        task_id=None,
    )
    IngestionJob.objects.create(
        created_by=user, source_type="csv", task_id="already-sent"
    )
    sent = []

    def enqueue(job_id):
        sent.append(job_id)
        return f"task-{len(sent)}"

    monkeypatch.setattr(ingestion_dispatch, "dispatch_ingestion_job", enqueue)

    result = ingestion_dispatch.dispatch_pending_ingestion_jobs(batch_size=2)

    assert result == {"eligible": 3, "dispatched": 2, "failures": 0}
    assert len(sent) == 2
    assert IngestionJob.objects.filter(
        pk__in=[job.pk for job in pending], task_id__isnull=False
    ).count() == 2


def test_dispatcher_repeated_run_does_not_send_completed_or_dispatched_jobs(
    monkeypatch, job
):
    sent = []
    monkeypatch.setattr(
        ingestion_dispatch,
        "dispatch_ingestion_job",
        lambda job_id: sent.append(job_id) or "task-1",
    )

    first = ingestion_dispatch.dispatch_pending_ingestion_jobs(batch_size=1)
    second = ingestion_dispatch.dispatch_pending_ingestion_jobs(batch_size=1)

    assert first == {"eligible": 1, "dispatched": 1, "failures": 0}
    assert second == {"eligible": 0, "dispatched": 0, "failures": 0}
    assert sent == [str(job.pk)]


def test_dispatcher_leaves_broker_failures_eligible_for_next_tick(monkeypatch, job):
    attempts = []

    def enqueue(job_id):
        attempts.append(job_id)
        if len(attempts) == 1:
            raise OSError("broker unavailable")
        return "task-recovered"

    monkeypatch.setattr(ingestion_dispatch, "dispatch_ingestion_job", enqueue)

    failed = ingestion_dispatch.dispatch_pending_ingestion_jobs(batch_size=1)
    recovered = ingestion_dispatch.dispatch_pending_ingestion_jobs(batch_size=1)

    assert failed == {"eligible": 1, "dispatched": 0, "failures": 1}
    assert recovered == {"eligible": 1, "dispatched": 1, "failures": 0}
    job.refresh_from_db()
    assert job.task_id == "task-recovered"


@pytest.mark.django_db(transaction=True)
def test_concurrent_dispatchers_do_not_claim_the_same_job(monkeypatch, job):
    entered_enqueue = Event()
    release_enqueue = Event()
    calls = []

    def enqueue(job_id):
        calls.append(job_id)
        entered_enqueue.set()
        release_enqueue.wait(timeout=10)
        return "task-once"

    monkeypatch.setattr(ingestion_dispatch, "dispatch_ingestion_job", enqueue)

    def dispatch_in_thread():
        close_old_connections()
        try:
            return ingestion_dispatch.dispatch_pending_ingestion_jobs(batch_size=1)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatch_in_thread)
        assert entered_enqueue.wait(timeout=10)
        second = pool.submit(dispatch_in_thread)
        second_result = second.result(timeout=10)
        release_enqueue.set()
        first_result = first.result(timeout=10)

    assert first_result["dispatched"] == 1
    assert second_result["dispatched"] == 0
    assert calls == [str(job.pk)]
