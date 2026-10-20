"""Settings for subprocess integration probes: always an isolated SQLite file."""

import os

from config.settings import *

DATABASES = {  # pyright: ignore[reportConstantRedefinition]
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ["DB_NAME"],
    }
}

CACHES = {  # pyright: ignore[reportConstantRedefinition]
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "integration-probe",
    }
}
