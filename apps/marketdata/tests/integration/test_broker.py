"""Real broker connectivity. Skips when Redis is not running."""

import pytest
from django.conf import settings
from kombu.exceptions import OperationalError

from config.celery import app

pytestmark = pytest.mark.integration


def test_celery_broker_connection():
    if not settings.CELERY_BROKER_URL.startswith(("redis://", "rediss://")):
        pytest.skip("CELERY_BROKER_URL is not a Redis broker")
    connection = app.connection()
    try:
        connection.ensure_connection(max_retries=1, timeout=2)
    except (OperationalError, OSError) as exc:
        pytest.skip(f"Celery broker unavailable: {exc}")
    finally:
        connection.release()
