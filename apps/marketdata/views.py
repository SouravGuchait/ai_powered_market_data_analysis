from rest_framework.permissions import IsAuthenticated
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from drf_spectacular.utils import extend_schema
from rest_framework import status as http_status
from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.marketdata.jobs.ingestion.dispatch import (
    IngestionEnqueueError,
    enqueue_after_commit,
)
from apps.marketdata.models import IngestionFile, IngestionJob
from apps.marketdata.serializers import (
    IngestionJobSerializer,
    UploadAcceptedSerializer,
    UploadRequestSerializer,
)
from apps.marketdata.storage import (
    StorageWriteError,
    UploadTooLarge,
    delete_stored_upload,
    save_upload,
)
from apps.marketdata.uploads import (
    PROBE_BYTES,
    UploadError,
    resolve_upload,
    validate_idempotency_key,
    validate_probe,
)

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 64 * 1024
# Multipart framing and ordinary form fields add a small amount above the file
# size. Reject obviously oversized requests before parsing them.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


@dataclass
class UploadInput:
    stream: BinaryIO
    filename: str | None
    content_type: str
    idempotency_key: str | None
    uploaded_file: UploadedFile | None = None


class UploadView(APIView):
    permission_classes = [IsAuthenticated]
    @extend_schema(
        request=UploadRequestSerializer,
        responses={202: UploadAcceptedSerializer},
    )
    def post(self, request):
        started = time.monotonic()
        uploaded_file: UploadedFile | None = None
        try:
            upload = _read_upload(request)
            uploaded_file = upload.uploaded_file
            metadata = resolve_upload(
                filename=upload.filename, content_type=upload.content_type
            )
            existing = _existing_job(request.user, upload.idempotency_key)
            if existing is not None:
                logger.info("upload.replayed job_id=%s", existing.id)
                return _accepted(existing, replayed=True)

            probe = upload.stream.read(PROBE_BYTES)
            validate_probe(metadata.source_type, probe or b"")
            job_id = uuid.uuid4()
            stored = save_upload(
                _upload_chunks(probe or b"", upload.stream),
                owner_id=request.user.pk,
                job_id=job_id,
                extension=metadata.extension,
                max_bytes=settings.MAX_UPLOAD_SIZE_BYTES,
            )

            try:
                with transaction.atomic():
                    job = IngestionJob.objects.create(
                        id=job_id,
                        created_by=request.user,
                        source_type=metadata.source_type,
                        idempotency_key=upload.idempotency_key,
                        file_size_bytes=stored.size_bytes,
                    )
                    IngestionFile.objects.create(
                        ingestion_job=job,
                        original_filename=metadata.original_filename,
                        storage_key=stored.storage_key,
                        checksum=stored.checksum,
                        content_type=metadata.content_type,
                        file_size_bytes=stored.size_bytes,
                    )
                    enqueue_after_commit(job.id)
            except IntegrityError:
                delete_stored_upload(stored.storage_key)
                existing = _existing_job(request.user, upload.idempotency_key)
                if existing is not None:
                    logger.info("upload.replayed job_id=%s", existing.id)
                    return _accepted(existing, replayed=True)
                logger.exception("upload.persist.failed job_id=%s", job_id)
                return _error(
                    "persistence_error",
                    "The upload could not be recorded.",
                    http_status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            except IngestionEnqueueError:
                raise
            except Exception:
                delete_stored_upload(stored.storage_key)
                logger.exception("upload.persist.failed job_id=%s", job_id)
                return _error(
                    "persistence_error",
                    "The upload could not be recorded.",
                    http_status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            logger.info(
                "upload.accepted job_id=%s source_type=%s size_bytes=%s duration_ms=%.1f",
                job.id,
                metadata.source_type,
                stored.size_bytes,
                (time.monotonic() - started) * 1000,
            )
            return _accepted(job, replayed=False)
        except UploadError as exc:
            logger.info("upload.rejected code=%s", exc.code)
            return _error(exc.code, exc.message, exc.status_code)
        except (ParseError, UnsupportedMediaType):
            logger.info("upload.rejected code=invalid_request")
            return _error(
                "invalid_request",
                "The upload request could not be parsed.",
                http_status.HTTP_400_BAD_REQUEST,
            )
        except UploadTooLarge:
            logger.info("upload.rejected code=file_too_large")
            return _error(
                "file_too_large",
                "The uploaded file exceeds the configured size limit.",
                http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        except StorageWriteError:
            return _error(
                "storage_error",
                "The uploaded file could not be stored.",
                http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except IngestionEnqueueError:
            return _error(
                "enqueue_failed",
                "The upload could not be queued. Retry with a new idempotency key.",
                http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception("upload.failed")
            return _error(
                "internal_error",
                "The upload could not be processed.",
                http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        finally:
            if uploaded_file is not None:
                uploaded_file.close()


class JobDetailView(APIView):
    permission_classes = [IsAuthenticated]
    @extend_schema(responses=IngestionJobSerializer)
    def get(self, request, job_id):
        job = get_object_or_404(
            IngestionJob.objects.filter(created_by=request.user), pk=job_id
        )
        return Response(IngestionJobSerializer(job).data)


def _read_upload(request) -> UploadInput:
    content_type = request.content_type or ""
    is_multipart = content_type.split(";")[0].strip().lower() == "multipart/form-data"
    declared_size = _content_length(request)
    if is_multipart:
        # Reject obvious oversized multipart bodies before Django spools them.
        if (
            declared_size is not None
            and declared_size
            > settings.MAX_UPLOAD_SIZE_BYTES + MULTIPART_OVERHEAD_BYTES
        ):
            raise _too_large()
        files = request.FILES.getlist("file")
        if not files:
            raise UploadError("file_required", "Attach a file in the 'file' field.")
        if len(files) > 1:
            raise UploadError(
                "multiple_files", "Upload exactly one file per request."
            )
        uploaded = files[0]
        try:
            if (
                uploaded.size is not None
                and uploaded.size > settings.MAX_UPLOAD_SIZE_BYTES
            ):
                raise _too_large()
            header_key = request.headers.get("Idempotency-Key")
            form_key = request.data.get("idempotency_key")
            return UploadInput(
                stream=uploaded,
                filename=uploaded.name,
                content_type=uploaded.content_type or "",
                idempotency_key=_coalesce_keys(header_key, form_key),
                uploaded_file=uploaded,
            )
        except Exception:
            uploaded.close()
            raise

    if declared_size is not None and declared_size > settings.MAX_UPLOAD_SIZE_BYTES:
        raise _too_large()
    stream = request.stream
    if stream is None:
        # DRF returns None when Content-Length is absent. Fall back to the
        # underlying request so chunked raw bodies are still readable.
        stream = request._request
    return UploadInput(
        stream=stream,
        filename=request.headers.get("X-Filename"),
        content_type=content_type,
        idempotency_key=validate_idempotency_key(
            request.headers.get("Idempotency-Key")
        ),
    )


def _upload_chunks(probe: bytes, stream: BinaryIO) -> Iterable[bytes]:
    if probe:
        yield probe
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def _content_length(request) -> int | None:
    value = request.META.get("CONTENT_LENGTH")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coalesce_keys(header_value: str | None, form_value: str | None) -> str | None:
    header = (header_value or "").strip()
    form = (form_value or "").strip() if isinstance(form_value, str) else ""
    if header and form and header != form:
        raise UploadError(
            "conflicting_idempotency_key",
            "Idempotency-Key header and form field must match.",
        )
    return validate_idempotency_key(header or form)


def _too_large() -> UploadError:
    return UploadError(
        "file_too_large",
        "The uploaded file exceeds the configured size limit.",
        status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    )


def _existing_job(user, idempotency_key: str | None) -> IngestionJob | None:
    if not idempotency_key:
        return None
    return IngestionJob.objects.filter(
        created_by=user, idempotency_key=idempotency_key
    ).first()


def _accepted(job: IngestionJob, *, replayed: bool) -> Response:
    payload = UploadAcceptedSerializer(
        {"id": job.id, "status": job.status, "replayed": replayed}
    ).data
    response = Response(payload, status=http_status.HTTP_202_ACCEPTED)
    response["Location"] = reverse(
        "marketdata:job-detail", kwargs={"job_id": job.id}
    )
    return response


def _error(code: str, message: str, status_code: int) -> Response:
    return Response(
        {"error": {"code": code, "message": message}}, status=status_code
    )
