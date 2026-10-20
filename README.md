AI Powered Backend Application

Market-data backend: Django + Django REST Framework, MySQL, Celery/Redis, and
Pandas for processing. Current state: custom email/username user model and JWT
authentication. Market-data models, upload jobs, ingestion, and analytics are
not implemented yet.

## Requirements

- Python 3.12
- MySQL 8.0 for the default database
- Redis 7 for the Celery broker and shared cache (optional locally)
- Windows, macOS, or Linux for local development

Pinned ranges live in `requirements.txt` (runtime) and `requirements-dev.txt`
(tests and tooling).

## Local setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# edit .env: set DJANGO_SECRET_KEY and the DB_* values
```

Create the MySQL database named by `DB_NAME` and a user matching `DB_USER` /
`DB_PASSWORD`, then:

```powershell
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py createsuperuser
.\venv\Scripts\python.exe manage.py runserver
```

`createsuperuser` prompts for username, email, last name, and password. The
custom user model must exist before the first migration of a database. A
database migrated by milestone 01 with Django's built-in user has to be
recreated (or migrated manually) before applying `accounts.0001_initial`.

`manage.py check` needs no database connection. `migrate` and the startup
migration hook do.

### Redis in Docker

When Django runs on the host and Redis runs in Docker, publish the Redis port
and use `127.0.0.1` in `.env`:

```powershell
docker run -d --name marketdata-redis -p 6379:6379 redis:latest
```

```dotenv
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
REDIS_CACHE_URL=redis://127.0.0.1:6379/1
```

Use a Compose service name such as `redis` only when Django itself runs inside
the same Docker network. `redis://redis:6379` does not resolve from the Windows
host and fails with `getaddrinfo failed`.

## Configuration

Settings are read from the process environment, with `.env` (git-ignored) as a
local fallback. Host-provided variables win over `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | none | Required in every mode; no insecure fallback |
| `DJANGO_DEBUG` | `False` | Enables DEBUG and defaults docs on |
| `DJANGO_ALLOWED_HOSTS` | empty | Comma-separated; required when DEBUG is off |
| `DJANGO_TIME_ZONE` | `UTC` | `USE_TZ` is always on |
| `DJANGO_LOG_LEVEL` | `INFO` | Console logging level |
| `DB_ENGINE` | `mysql` | Parsed but currently ignored; MySQL is the wired backend |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | none | Required for MySQL |
| `DB_HOST`, `DB_PORT` | `127.0.0.1`, `3306` | MySQL address |
| `DB_TEST_NAME` | none | Test database name |
| `CELERY_BROKER_URL` | `memory://` in DEBUG | `REDIS_URL` is accepted as a fallback; required when DEBUG is off |
| `REDIS_CACHE_URL` | none | Unset means per-process LocMemCache |
| `JWT_ACCESS_MINUTES` | `15` | Access-token lifetime in minutes |
| `JWT_REFRESH_DAYS` | `7` | Refresh-token lifetime in days |
| `AUTH_THROTTLE_REGISTER` | `5/min` | DRF rate for the `register` scope |
| `AUTH_THROTTLE_LOGIN` | `10/min` | DRF rate for the `login` scope |
| `AUTH_THROTTLE_REFRESH` | `20/min` | DRF rate for the `refresh` scope |
| `AUTH_THROTTLE_LOGOUT` | `20/min` | DRF rate for the `logout` scope |
| `AUTO_MIGRATE` | `True` | Apply pending migrations when WSGI/ASGI starts |
| `SERVE_API_DOCS` | DEBUG | Serves `/api/schema/` and `/api/docs/` |
| `DJANGO_MEDIA_ROOT` | `media` | Filesystem media storage |
| `DJANGO_STATIC_ROOT` | `staticfiles` | `collectstatic` target |

Invalid configuration raises `ImproperlyConfigured` at import. There is no
silent fallback to SQLite, a default secret, or an empty allowed-host list.
Integration probes override `DATABASES` with SQLite explicitly.

## Authentication

- `apps.accounts.User` extends `AbstractUser`. The `accounts_user` columns are
  `username` (unique, not null), `email` (unique, not null), `lastname` (not
  null, Python field `last_name`), and `password` (Django hash).
- Login accepts either the username or the email in the `username` field.
  Matching is case-insensitive for both, with username taking precedence.
- Email is normalized to lowercase, including the local part, on save and on
  register/login. Duplicate emails and duplicate usernames with different casing
  are rejected by the serializer.
- Passwords use Django hashing and `AUTH_PASSWORD_VALIDATORS`, including the
  similarity check against the username and email. Hashes are never returned.
- API authentication is JWT via `djangorestframework-simplejwt`. Send
  `Authorization: Bearer <access>`. Access tokens last `JWT_ACCESS_MINUTES`;
  refresh tokens last `JWT_REFRESH_DAYS` and rotate on use.
- The API does not use CSRF or session cookies. Django's CSRF middleware stays
  enabled for `/admin/`, which is session-based.
- Unknown usernames/emails, wrong passwords, and inactive accounts return the
  same `401 {"detail": "No active account found with the given credentials"}`
  response.

| Method | Path | Access | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register/` | public, throttled | Creates a user; `201` |
| `POST` | `/api/v1/auth/login/` | public, throttled | Returns `access`, `refresh`, `user`; `200` |
| `POST` | `/api/v1/auth/token/refresh/` | public, throttled | Rotates the refresh token; `200` |
| `POST` | `/api/v1/auth/logout/` | public, throttled | Blacklists `refresh`; `200` |
| `GET` | `/api/v1/auth/me/` | Bearer access token | Current user |
| `GET` | `/api/schema/`, `/api/docs/` | public when enabled | OpenAPI schema and Swagger UI |
| - | `/admin/` | staff session | Django admin |

All other DRF views default to `IsAuthenticated`.

### Request examples

Registration:

```http
POST /api/v1/auth/register/
Content-Type: application/json

{"username": "owner", "email": "owner@example.com", "last_name": "Owner", "password": "CorrectHorseBatteryStaple9"}
```

Login. The `username` field takes either the username or the email:

```http
POST /api/v1/auth/login/
Content-Type: application/json

{"username": "owner", "password": "CorrectHorseBatteryStaple9"}
```

```http
POST /api/v1/auth/login/
Content-Type: application/json

{"username": "owner@example.com", "password": "CorrectHorseBatteryStaple9"}
```

Refresh, logout, and current user:

```http
POST /api/v1/auth/token/refresh/
Content-Type: application/json

{"refresh": "<refresh token>"}
```

```http
POST /api/v1/auth/logout/
Content-Type: application/json

{"refresh": "<refresh token>"}
```

```http
GET /api/v1/auth/me/
Authorization: Bearer <access token>
```

PowerShell smoke test:

```powershell
$login = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/auth/login/ `
  -ContentType application/json `
  -Body '{"username":"owner","password":"CorrectHorseBatteryStaple9"}'
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8000/api/v1/auth/me/ `
  -Headers @{ Authorization = "Bearer $($login.access)" }
```

### Throttling

DRF `ScopedRateThrottle` is the default throttle class, and the register, login,
refresh, and logout views also set it explicitly with
`throttle_classes = [ScopedRateThrottle]` and a `throttle_scope`. Rates come from
`AUTH_THROTTLE_REGISTER`, `AUTH_THROTTLE_LOGIN`, `AUTH_THROTTLE_REFRESH`, and
`AUTH_THROTTLE_LOGOUT`. Throttle state lives in the configured cache: use
`REDIS_CACHE_URL` in multi-process deployments, otherwise limits are per
process. Throttled requests return `429` with DRF's generic message and do not
include the submitted username, email, or password.

### Migrations on startup

`config/wsgi.py` and `config/asgi.py` call `config.startup.apply_migrations()`,
which runs `migrate --noinput` when `AUTO_MIGRATE` is true. `manage.py runserver`
loads the WSGI application, so the tables are created or updated before the
server starts listening. Set `AUTO_MIGRATE=False` and run `manage.py migrate` as
a release step to keep application processes read-only against the schema.

### Ownership

`apps/accounts/permissions.py:IsOwner` allows an object only when
`obj.owner_id == request.user.pk`. Upload-job endpoints, when added, must filter
querysets by the requesting user and combine that with this permission. The
cross-user denial case is covered by `apps/accounts/tests/test_permissions.py`.

## Layout

- `config/` - settings, URLs, WSGI/ASGI, startup migration hook, Celery bootstrap
- `config/settings_test.py` - test settings; keeps the configured database and
  forces LocMemCache so tests need no Redis
- `apps/accounts/` - custom user, manager, email-or-username backend, admin,
  serializers, JWT views, throttling scopes, owner permission, tests
- `apps/accounts/tests/integration/` - auth flow, migration, and startup probes
- `apps/marketdata/` - ingestion app placeholder; models and processing come later
- `manage.py` - command entrypoint
- `pytest.ini` - `DJANGO_SETTINGS_MODULE=config.settings_test`, tests under `apps/`

## Tests

```powershell
# Full suite, using the database configured by .env (MySQL by default)
.\venv\Scripts\python.exe -m pytest -q

# Integration marker only
.\venv\Scripts\python.exe -m pytest -q -m integration
```

Integration tests live in `apps/accounts/tests/integration/`:

- `test_auth_flow.py` drives register, login by username and email, `/me/`,
  refresh rotation, logout blacklisting, and CSRF-free API requests.
- `test_schema.py` starts fresh Django processes for two probes: migration
  `0002` against legacy `0001` rows and WSGI startup table creation. The probes
  use `probe_settings.py` and isolated SQLite files, so they do not touch the
  configured MySQL database.

The rest of the suite covers registration and email normalization, duplicate
username and email at the serializer and database layers, password validators,
login with username and with email, JWT refresh and blacklist logout, the
unauthenticated boundary, CSRF-free API POSTs, cross-user ownership denial,
superuser/admin creation, and throttling for the credential endpoints.

## Verification (run 2026-10-20, Python 3.12.0)

Installed versions: Django 5.2.18, DRF 3.16.1, djangorestframework-simplejwt
5.5.1, drf-spectacular 0.30.0, mysqlclient 2.3.0, pytest 8.4.2.

| Command | Result |
| --- | --- |
| `python manage.py check` | `System check identified no issues` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `pytest -q --create-db` with MySQL | 29 passed |
| `pytest -q -m integration` | 3 passed |
| Migration probe: `0001` with three users, then `0002` | usernames `alice`, `alice2`, `bob`; `lastname` column; unique username/email indexes; rows preserved |
| Startup probe: import `config.wsgi` with `AUTO_MIGRATE=True` | `accounts_user` and `token_blacklist_outstandingtoken` created; `username` and `lastname` present |
| `cache.set` / `cache.get` through `REDIS_CACHE_URL=redis://127.0.0.1:6379/1` | returned `ok` |
| Celery `app.connection().ensure_connection()` | broker connection succeeded |
| Six registration `POST`s at the default `5/min` | `201, 201, 201, 201, 201, 429` |
| `python manage.py spectacular --file <temp>` | exit 0, no schema errors |
| `ruff check apps config manage.py` | `All checks passed!` |
| `basedpyright apps` | 0 errors |
| `pyrefly check apps` | 0 errors |

The local MySQL server was available at `127.0.0.1:3306` and was used for the
main suite. The integration probes use isolated SQLite files in subprocesses.
Redis 7 was available through Docker; cache reads/writes and the Celery broker
connection succeeded, but cross-process throttle load behavior was not tested.
`basedpyright` 1.40.2 and `pyrefly` 1.4.0 are available in the local virtualenv
but are not pinned in `requirements-dev.txt`.

## Known limitations

- No password-reset endpoint (no email transport configured).
- Access tokens are stateless and cannot be revoked before they expire. Logout
  blacklists only the refresh token.
- `AUTO_MIGRATE=True` runs `migrate` from the WSGI/ASGI import hook. Multiple
  workers can start at the same time; use a release step with
  `AUTO_MIGRATE=False` for larger deployments.
- Django admin keeps session and CSRF protection; only the API is CSRF-free.
- Username uniqueness is case-insensitive in the serializer, but the SQLite
  unique index is case-sensitive. MySQL's default collation is case-insensitive.
- Throttle limits use LocMemCache when `REDIS_CACHE_URL` is unset, so they are
  per process. Redis cache connectivity was verified, but cross-process throttle
  limits were not load-tested.
- basedpyright and pyrefly report 0 errors in `apps/`; basedpyright still emits
  annotation warnings for untyped DRF and Django class attributes. Its
  `reportConstantRedefinition` diagnostics for `config/settings.py` predate this
  milestone.
- The `IsOwner` permission is implemented and tested, but no upload-job models or
  endpoints exist yet; owner-scoped querysets belong to the upload milestone.
- No market-data models, upload endpoints, Celery tasks, or Pandas processing.
- No Docker, deployment, or AWS configuration; `gunicorn` is listed for Linux
  container serving but was not run on Windows.
- Changing `AUTH_USER_MODEL` after a database has production data requires a
  migration plan; this milestone assumes no production data.
