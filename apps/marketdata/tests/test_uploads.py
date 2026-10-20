"""Upload API and job lifecycle behavior."""

import hashlib
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.marketdata.models import IngestionFile, IngestionJob

pytestmark = pytest.mark.django_db

CSV_BYTES = (
    b"symbol,timestamp,open,high,low,close,volume\n"
    b"AAPL,2024-01-02,100.0,101.0,99.0,100.5,1000\n"
)
OTHER_CSV_BYTES = (
    b"symbol,timestamp,open,high,low,close,volume\n"
    b"MSFT,2024-01-03,200.0,201.0,199.0,200.5,2000\n"
)


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
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def other_client(other_user):
    client = APIClient()
    client.force_authenticate(user=other_user)
    return client


@pytest.fixture(autouse=True)
def upload_storage(tmp_path, monkeypatch):
    backend = FileSystemStorage(location=tmp_path)
    monkeypatch.setattr(
        "apps.marketdata.storage.get_upload_storage", lambda: backend
    )
    return backend


@pytest.fixture(autouse=True)
def dispatch_calls(monkeypatch):
    calls = []

    def fake_dispatch(job_id):
        calls.append(job_id)
        return f"task-{len(calls)}"

    monkeypatch.setattr(
        "apps.marketdata.jobs.ingestion.dispatch.dispatch_ingestion_job",
        fake_dispatch,
    )
    return calls


def upload_multipart(
    client,
    *,
    content=CSV_BYTES,
    filename="prices.csv",
    content_type="text/csv",
    idempotency_key=None,
    form_key=None,
):
    data = {"file": SimpleUploadedFile(filename, content, content_type=content_type)}
    if form_key is not None:
        data["idempotency_key"] = form_key
    headers = {}
    if idempotency_key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
    return client.post(
        reverse("marketdata:upload"), data, format="multipart", **headers
    )


def upload_raw(
    client,
    *,
    content=CSV_BYTES,
    content_type="text/csv",
    filename=None,
    idempotency_key=None,
):
    headers = {}
    if filename is not None:
        headers["HTTP_X_FILENAME"] = filename
    if idempotency_key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
    return client.post(
        reverse("marketdata:upload"),
        data=content,
        content_type=content_type,
        **headers,
    )


def stored_files(backend):
    root = Path(backend.location)
    return [path for path in root.rglob("*") if path.is_file()]


def error_code(response):
    return response.json()["error"]["code"]


def test_multipart_csv_creates_queued_job(
    api_client,
    user,
    upload_storage,
    dispatch_calls,
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        response = upload_multipart(api_client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["replayed"] is False
    assert response["Location"] == reverse(
        "marketdata:job-detail", kwargs={"job_id": body["id"]}
    )

    job = IngestionJob.objects.get(id=body["id"])
    assert job.created_by_id == user.pk
    assert job.source_type == "csv"
    assert job.file_size_bytes == len(CSV_BYTES)
    assert job.task_id == "task-1"
    assert dispatch_calls == [body["id"]]

    stored = IngestionFile.objects.get(ingestion_job=job)
    assert stored.original_filename == "prices.csv"
    assert stored.content_type == "text/csv"
    assert stored.file_size_bytes == len(CSV_BYTES)
    assert stored.checksum == hashlib.sha256(CSV_BYTES).hexdigest()
    assert stored.storage_key.startswith(f"uploads/{user.pk}/{job.id}/")
    assert stored.storage_key.endswith(".csv")
    assert upload_storage.exists(stored.storage_key)
    assert "storage_key" not in body


@pytest.mark.parametrize(
    ("content", "content_type", "filename", "expected_type"),
    [
        (CSV_BYTES, "text/csv", "prices.csv", "csv"),
        (b'[{"symbol": "AAPL"}]', "application/json", None, "json"),
        (b"PK\x03\x04rest-of-workbook", (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ), None, "xlsx"),
    ],
)
def test_raw_uploads_are_accepted(
    api_client,
    dispatch_calls,
    django_capture_on_commit_callbacks,
    content,
    content_type,
    filename,
    expected_type,
):
    with django_capture_on_commit_callbacks(execute=True):
        response = upload_raw(
            api_client,
            content=content,
            content_type=content_type,
            filename=filename,
        )

    assert response.status_code == 202
    job = IngestionJob.objects.get(id=response.json()["id"])
    assert job.source_type == expected_type
    assert len(dispatch_calls) == 1


def test_upload_without_file_is_rejected(api_client):
    response = api_client.post(reverse("marketdata:upload"), {}, format="multipart")

    assert response.status_code == 400
    assert error_code(response) == "file_required"
    assert IngestionJob.objects.count() == 0


def test_wrong_extension_is_rejected(api_client):
    response = upload_multipart(
        api_client, filename="prices.txt", content_type="text/plain"
    )

    assert response.status_code == 415
    assert error_code(response) == "unsupported_file_type"
    assert IngestionJob.objects.count() == 0


def test_mismatched_mime_hint_is_rejected(api_client):
    response = upload_multipart(api_client, content_type="image/png")

    assert response.status_code == 415
    assert error_code(response) == "unsupported_media_type"
    assert IngestionJob.objects.count() == 0


@override_settings(MAX_UPLOAD_SIZE_BYTES=32)
def test_oversized_file_is_rejected(api_client, upload_storage):
    response = upload_multipart(api_client)

    assert response.status_code == 413
    assert error_code(response) == "file_too_large"
    assert IngestionJob.objects.count() == 0
    assert stored_files(upload_storage) == []


@override_settings(MAX_UPLOAD_SIZE_BYTES=32)
def test_oversized_raw_file_is_rejected_before_storage(api_client, upload_storage):
    response = upload_raw(api_client)

    assert response.status_code == 413
    assert error_code(response) == "file_too_large"
    assert IngestionJob.objects.count() == 0
    assert stored_files(upload_storage) == []


def test_missing_required_csv_columns_are_rejected(api_client, upload_storage):
    response = upload_multipart(
        api_client, content=b"symbol,timestamp\nAAPL,2024-01-02\n"
    )

    assert response.status_code == 400
    assert error_code(response) == "missing_columns"
    assert "open" in response.json()["error"]["message"]
    assert IngestionJob.objects.count() == 0
    assert stored_files(upload_storage) == []


def test_empty_file_is_rejected(api_client):
    response = upload_multipart(api_client, content=b"")

    assert response.status_code == 400
    assert error_code(response) == "empty_file"
    assert IngestionJob.objects.count() == 0


def test_non_utf8_csv_header_is_rejected(api_client):
    response = upload_multipart(
        api_client,
        content=(
            b"symbol,timestamp,open,high,low,close,volume,caf\xe9\n"
            b"AAPL,2024-01-02,1,1,1,1,1\n"
        ),
    )

    assert response.status_code == 400
    assert error_code(response) == "unsupported_encoding"


def test_malformed_json_probe_is_rejected(api_client):
    response = upload_multipart(
        api_client,
        filename="prices.json",
        content=b"not-json",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert error_code(response) == "invalid_file_content"


def test_invalid_xlsx_probe_is_rejected(api_client):
    response = upload_multipart(
        api_client,
        filename="prices.xlsx",
        content=b"not-a-zip",
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )

    assert response.status_code == 400
    assert error_code(response) == "invalid_file_content"


def test_upload_requires_authentication():
    response = APIClient().post(
        reverse("marketdata:upload"), {"file": SimpleUploadedFile("p.csv", CSV_BYTES)}
    )

    assert response.status_code == 401
    assert IngestionJob.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_dispatch_runs_only_after_commit(api_client, dispatch_calls):
    with transaction.atomic():
        response = upload_multipart(api_client)
        assert response.status_code == 202
        assert dispatch_calls == []

    assert dispatch_calls == [response.json()["id"]]


@pytest.mark.django_db(transaction=True)
def test_enqueue_failure_returns_503_and_removes_job(
    api_client, upload_storage, monkeypatch
):
    def fail_dispatch(job_id):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "apps.marketdata.jobs.ingestion.dispatch.dispatch_ingestion_job",
        fail_dispatch,
    )

    response = upload_multipart(api_client)

    assert response.status_code == 503
    assert error_code(response) == "enqueue_failed"
    assert IngestionJob.objects.count() == 0
    assert IngestionFile.objects.count() == 0
    assert stored_files(upload_storage) == []


def test_storage_failure_returns_503_and_creates_no_job(
    api_client, monkeypatch
):
    class BrokenStorage:
        def save(self, name, content):
            raise OSError("disk full")

        def delete(self, name):
            return None

        def exists(self, name):
            return False

    monkeypatch.setattr(
        "apps.marketdata.storage.get_upload_storage", lambda: BrokenStorage()
    )

    response = upload_multipart(api_client)

    assert response.status_code == 503
    assert error_code(response) == "storage_error"
    assert IngestionJob.objects.count() == 0
    assert IngestionFile.objects.count() == 0


def test_duplicate_idempotency_key_replays_existing_job(
    api_client,
    dispatch_calls,
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        first = upload_multipart(api_client, idempotency_key="retry-1")
        second = upload_multipart(
            api_client,
            content=OTHER_CSV_BYTES,
            idempotency_key="retry-1",
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert IngestionJob.objects.count() == 1
    assert IngestionFile.objects.count() == 1
    assert dispatch_calls == [first.json()["id"]]


def test_idempotency_key_is_owner_scoped(
    api_client,
    other_client,
    dispatch_calls,
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        first = upload_multipart(api_client, idempotency_key="shared-key")
        second = upload_multipart(other_client, idempotency_key="shared-key")

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["replayed"] is False
    assert IngestionJob.objects.count() == 2
    assert len(dispatch_calls) == 2


def test_invalid_idempotency_key_is_rejected(api_client):
    response = upload_multipart(api_client, idempotency_key="x" * 65)

    assert response.status_code == 400
    assert error_code(response) == "invalid_idempotency_key"
    assert IngestionJob.objects.count() == 0


def test_conflicting_idempotency_values_are_rejected(api_client):
    response = upload_multipart(
        api_client, idempotency_key="header-key", form_key="form-key"
    )

    assert response.status_code == 400
    assert error_code(response) == "conflicting_idempotency_key"
    assert IngestionJob.objects.count() == 0


def test_user_paths_never_become_storage_keys(
    api_client,
    user,
    upload_storage,
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        response = upload_multipart(
            api_client, filename="..\\..\\secret.csv"
        )

    assert response.status_code == 202
    stored = IngestionFile.objects.get(ingestion_job_id=response.json()["id"])
    assert stored.original_filename == "secret.csv"
    assert stored.storage_key.startswith(f"uploads/{user.pk}/")
    assert "secret" not in stored.storage_key
    assert ".." not in stored.storage_key
    assert upload_storage.exists(stored.storage_key)


def test_job_detail_is_owner_scoped(api_client, other_client, user):
    job = IngestionJob.objects.create(
        created_by=user,
        source_type="csv",
        rows_received=10,
        rows_accepted=8,
        rows_rejected=2,
    )

    response = api_client.get(
        reverse("marketdata:job-detail", kwargs={"job_id": job.id})
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(job.id)
    assert body["status"] == "queued"
    assert body["rows_received"] == 10
    assert body["rows_accepted"] == 8
    assert body["rows_rejected"] == 2
    assert "storage_key" not in body
    assert "idempotency_key" not in body

    denied = other_client.get(
        reverse("marketdata:job-detail", kwargs={"job_id": job.id})
    )
    assert denied.status_code == 404

    anonymous = APIClient().get(
        reverse("marketdata:job-detail", kwargs={"job_id": job.id})
    )
    assert anonymous.status_code == 401
