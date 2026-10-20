"""Environment-driven Django settings.

Process environment wins; `.env` is only a local fallback. Missing or
unsupported production configuration fails at import instead of falling back
to SQLite, a default secret, or an empty host list.
"""

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def required_env(name):
    value = env(name)
    if not value:
        raise ImproperlyConfigured(f"{name} must be set")
    return value


def env_bool(name, default=False):
    value = env(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean")


def env_int(name, default):
    value = env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc


def env_list(name, default=()):
    value = env(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_path(name, default):
    value = env(name)
    path = Path(value) if value else Path(default)
    return path if path.is_absolute() else BASE_DIR / path


SECRET_KEY = required_env("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set when DEBUG is off")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "apps.accounts",
    "apps.marketdata",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.EmailOrUsernameBackend"]

DB_ENGINE = (env("DB_ENGINE", "mysql") or "").strip().lower()
# if DB_ENGINE == "sqlite":
#     DATABASES = {
#         "default": {
#             "ENGINE": "django.db.backends.sqlite3",
#             "NAME": env("DB_NAME", str(BASE_DIR / "db.sqlite3")),
#         }
#     }
# elif DB_ENGINE == "mysql":
test_database = env("DB_TEST_NAME")
DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": required_env("DB_NAME"),
            "USER": required_env("DB_USER"),
            "PASSWORD": required_env("DB_PASSWORD"),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
            **({"TEST": {"NAME": test_database}} if test_database else {}),
        }
    }
# else:
#     raise ImproperlyConfigured(f"Unsupported DB_ENGINE: {DB_ENGINE!r}")

REDIS_CACHE_URL = env("REDIS_CACHE_URL")
if REDIS_CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_CACHE_URL,
            "KEY_PREFIX": "marketdata",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "marketdata-local",
        }
    }

CELERY_BROKER_URL = env("CELERY_BROKER_URL") or env("REDIS_URL")
if not CELERY_BROKER_URL:
    if DEBUG:
        CELERY_BROKER_URL = "memory://"
    else:
        raise ImproperlyConfigured(
            "CELERY_BROKER_URL (or REDIS_URL) must be set when DEBUG is off"
        )
CELERY_RESULT_BACKEND = None
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = env("DJANGO_TIME_ZONE", "UTC")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = env_path("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles")
MEDIA_URL = "/media/"
MEDIA_ROOT = env_path("DJANGO_MEDIA_ROOT", BASE_DIR / "media")

# Upload transport bound. Files are streamed to storage and rejected before
# completion when they exceed this limit.
MAX_UPLOAD_SIZE_BYTES = env_int("MAX_UPLOAD_SIZE_BYTES", 50 * 1024 * 1024)

# Ingestion processing bounds. CHUNK_SIZE caps the frame handed to pandas;
# DB_BATCH_SIZE caps each bulk_create call. The final partial batch is written
# by the same bulk_create call, so no flush setting is needed.
INGESTION_CHUNK_SIZE = env_int("INGESTION_CHUNK_SIZE", 5_000)
INGESTION_DB_BATCH_SIZE = env_int("INGESTION_DB_BATCH_SIZE", 1_000)
# Used when a file has no `exchange` column. The exchange row is created on
# first use; files that name other exchange codes require those rows to exist.
INGESTION_DEFAULT_EXCHANGE = (
    env("INGESTION_DEFAULT_EXCHANGE", "XNYS") or ""
).strip().upper()
INGESTION_DEFAULT_EXCHANGE_TIMEZONE = env(
    "INGESTION_DEFAULT_EXCHANGE_TIMEZONE", "America/New_York"
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "register": env("AUTH_THROTTLE_REGISTER", "5/min"),
        "login": env("AUTH_THROTTLE_LOGIN", "10/min"),
        "refresh": env("AUTH_THROTTLE_REFRESH", "20/min"),
        "logout": env("AUTH_THROTTLE_LOGOUT", "20/min"),
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env_int("JWT_ACCESS_MINUTES", 15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env_int("JWT_REFRESH_DAYS", 7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Market Data API",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
SERVE_API_DOCS = env_bool("SERVE_API_DOCS", DEBUG)
AUTO_MIGRATE = env_bool("AUTO_MIGRATE", True)

LOG_LEVEL = (env("DJANGO_LOG_LEVEL", "INFO") or "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "celery": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
