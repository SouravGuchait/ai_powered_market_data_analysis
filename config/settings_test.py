"""Test settings: keep the configured database, avoid an external cache."""

from config.settings import *

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "marketdata-tests",
    }
}

INGESTION_DISPATCH_INTERVAL_SECONDS = 0
CELERY_BEAT_SCHEDULE.pop("marketdata-pending-ingestion-dispatch", None)
