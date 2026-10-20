"""Test settings: keep the configured database, avoid an external cache."""

from config.settings import *

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "marketdata-tests",
    }
}
