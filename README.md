AI Powered Backend Application

Market-data backend: Django + Django REST Framework, MySQL, Celery/Redis, and
Pandas for processing. Current state: bootstrap plus custom email-based user
authentication (JWT with rotation/revocation and endpoint throttling). Business
models and ingestion arrive in later milestones.

## Requirements

- Python 3.12
- MySQL 8.0 (runtime database)
- Redis 7 (Celery broker and cache)
- Windows, macOS, or Linux for local development; the container image targets Linux

Pinned ranges live in `requirements.txt` and `requirements-dev.txt`.

## Local setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# edit .env: DJANGO_SECRET_KEY, DB_* (and MYSQL_APP_* only for Compose)
```

Create the MySQL database named by `DB_NAME` and a user matching `DB_USER` /
`DB_PASSWORD`, then:

```powershell
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py runserver
```

`manage.py check` needs no database connection; `migrate` and `readyz` do.

## Configuration

Settings are read from the environment, with `.env` (git-ignored) loaded as a
local fallback. Host-provided variables win over `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | none | Required in every mode; no insecure fallback |
| `DJANGO_DEBUG` | `False` | Enables DEBUG and defaults docs on |
| `DJANGO_ALLOWED_HOSTS` | empty | Comma-separated; required when DEBUG is off |
| `DJANGO_TIME_ZONE` | `UTC` | `USE_TZ` is always on |
| `DJANGO_LOG_LEVEL` | `INFO` | Root and `django` loggers |
| `DB_ENGINE` | `mysql` | `sqlite` is an explicit local/test override |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | none | Required for MySQL |
| `DB_HOST`, `DB_PORT` | `127.0.0.1`, `3306` | `mysql` inside Compose |
| `DB_TEST_NAME` | none | Test database name |
| `CELERY_BROKER_URL` | `memory://` in DEBUG | Required when DEBUG is off |
| `REDIS_CACHE_URL` | none | Unset means LocMemCache |
| `SERVE_API_DOCS` | DEBUG | Serves `/api/schema/` and `/api/docs/` |
| `JWT_ACCESS_MINUTES` | `30` | Access-token lifetime |
| `JWT_REFRESH_DAYS` | `7` | Refresh-token lifetime; refresh tokens rotate |
| `AUTH_THROTTLE_REGISTER` | `5/hour` | DRF rate for `POST /api/v1/auth/register/` |
| `AUTH_THROTTLE_LOGIN` | `10/min` | DRF rate for `POST /api/v1/auth/token/` |
| `AUTH_THROTTLE_REFRESH` | `30/min` | DRF rate for `POST /api/v1/auth/token/refresh/` |
| `DJANGO_MEDIA_ROOT` | `media` | Filesystem media storage |
| `DJANGO_STATIC_ROOT` | `staticfiles` | `collectstatic` target |

Invalid configuration fails at import with `ImproperlyConfigured`; it never
falls back to SQLite, a default secret, or an empty allowed-host list.

## URLs

- `/healthz` - process liveness, no dependency contacted
- `/readyz` - readiness, returns 503 when MySQL is unreachable
- `/api/schema/`, `/api/docs/` - OpenAPI schema and Swagger UI (when enabled)
- `/admin/` - Django admin

## Authentication

`AUTH_USER_MODEL = "accounts.User"`. Login identity is the email address:
stored lowercase, unique at the database layer, and there is no username column.
`/admin/` creates users by email.

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `POST /api/v1/auth/register/` | public | Create a user; 201 returns the normalized `email` |
| `POST /api/v1/auth/token/` | public | Email + password -> access and refresh JWTs |
| `POST /api/v1/auth/token/refresh/` | public | Rotate a refresh token (the old one is blacklisted) |
| `GET /api/v1/auth/me/` | JWT | Current user (id, email, names, date joined) |
| `POST /api/v1/auth/logout/` | JWT | Blacklist the supplied refresh token |

JWT handling uses `djangorestframework-simplejwt`. Access tokens are short-lived
(`JWT_ACCESS_MINUTES`), refresh tokens rotate (`ROTATE_REFRESH_TOKENS`) and are
revoked after rotation (`BLACKLIST_AFTER_ROTATION`) through the
`token_blacklist` app, so replaying a rotated or logged-out refresh token returns
401. DRF's default permission is `IsAuthenticated`, so new endpoints are private
unless they opt out. Response bodies never contain password hashes or internals.

Credential endpoints use DRF `ScopedRateThrottle` with the scopes above;
anonymous callers are keyed by client IP and authenticated callers by user id.
Throttled calls return 429 with a generic `detail` and no account information.
Rates come from the environment and are read by `ScopedRateThrottle` at import,
so changing them requires a process restart.

`apps/accounts/tests/` forces `LocMemCache` for throttle counters, so the suite
runs without Redis.

### Which database am I hitting?

There are two separate MySQL instances, each with its own schema and data:

| Caller | Host and port | Database / user |
| --- | --- | --- |
| Host `manage.py` and `runserver` (`.env`) | `127.0.0.1:3306` | native MySQL, `ai_market_data` |
| Compose `web`, `worker`, `migrate` | `mysql:3306` inside the network | `ai_market_data`, `market_data` |
| Host client against Compose MySQL | `127.0.0.1:3407` | `ai_market_data`, `market_data` |

Migrations run independently for each. A user registered through
`localhost:8100` lives in the Compose database; `manage.py shell` on the host
sees only the native database. To point host tooling at the Compose database,
set `DB_HOST=127.0.0.1`, `DB_PORT=3407`, `DB_USER=market_data`,
`DB_PASSWORD=market_data_local_pw` in `.env`.

If registration returns 429, the register throttle (`5/hour` per IP) is active.
For development either raise `AUTH_THROTTLE_REGISTER` in `.env` and recreate the
web container, or clear the counters in the cache:

```powershell
docker compose exec redis redis-cli -n 1 DEL ":1:throttle_auth_register_172.18.0.1"
```

A 400 with `A user with this email already exists.` is the duplicate-email
check, not a missing schema. Both databases already contain `accounts_user`,
`token_blacklist_*`, and the Django tables; inspect with
`docker compose exec mysql mysql -umarket_data -pmarket_data_local_pw ai_market_data -e "SHOW TABLES;"`.

## Tests

```powershell
$env:REDIS_CACHE_URL=""   # keep throttle counters local
.\venv\Scripts\python.exe -m pytest apps/accounts -q
```

Covered: email normalization, duplicate email across case, password validation,
missing/invalid credentials, inactive accounts, unauthenticated access, token
rotation and blacklist, logout, and throttling of register/login/refresh.

## Docker artifacts

`Dockerfile`, `docker-compose.yml`, `docker-compose.override.yml`, `Makefile`,
`scripts/dev.ps1`, and `docker/entrypoint.sh` were already present and consume
this configuration (`config.wsgi:application`, `celery -A config`, `/readyz`,
`.env`). The Docker orchestration itself is a separate milestone and the image
has not been built here.

## Layout

- `config/` - settings, URLs, WSGI/ASGI, Celery bootstrap, health views
- `apps/accounts/` - custom email user, admin, JWT register/login/logout views, tests
- `apps/marketdata/` - ingestion app; processing, models, tasks in later milestones
- `pytest.ini` - `DJANGO_SETTINGS_MODULE=config.settings`, tests under `apps/`

## Running with Docker

The Compose project is `ai-powered-market-data-analysis`, a name derived from this
repository so it cannot collide with another local stack. `docker compose up`
merges `docker-compose.override.yml` (bind-mounted source, DEBUG on, published
ports):

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f web worker
```

| Host | Service |
| --- | --- |
| `http://localhost:8100/healthz` | liveness |
| `http://localhost:8100/readyz` | readiness (MySQL) |
| `http://localhost:8100/api/docs/` | Swagger UI |
| `http://localhost:8100/admin/` | Django admin |
| `localhost:3407` | MySQL (`ai_market_data`) |
| `localhost:6480` | Redis |

The one-shot `migrate` service runs before `web` and `worker`; both wait for it to
exit 0. Use `docker compose down` to stop (volumes persist) and
`docker compose down -v` to wipe the database.

## Verification (run 2026-10-20, Python 3.12.0)

Installed with `pip install -r requirements.txt` and
`pip install -r requirements-dev.txt`: Django 5.2.18, DRF 3.18.3,
drf-spectacular 0.30.0, mysqlclient 2.3.0, celery 5.6.3, redis 6.4.0,
pandas 3.0.6, numpy 2.5.4, whitenoise 6.12.0, pytest 9.1.1,
pytest-django 4.14.0.

| Command | Result |
| --- | --- |
| `python manage.py check` (with `.env`) | `System check identified no issues` |
| `DB_ENGINE=sqlite python manage.py check --database default` | no issues |
| `DB_ENGINE=sqlite DB_NAME=<temp> python manage.py migrate --noinput` | migrations applied |
| Test client: `/healthz`, `/readyz`, `/api/docs/`, `/api/schema/` | 200, 200, 200, 200 |
| `collectstatic` into temp `STATIC_ROOT` | 154 files; `/static/admin/css/base.css` 200 |
| `bash -n docker/entrypoint.sh` | exit 0 |
| Missing `DJANGO_SECRET_KEY` | `ImproperlyConfigured` |
| `DB_ENGINE=postgres` | `ImproperlyConfigured: Unsupported DB_ENGINE` |
| `DJANGO_DEBUG=False` without broker URL | `ImproperlyConfigured: CELERY_BROKER_URL` |
| `git check-ignore -v .env` | matched `.gitignore:11` |
| `docker compose up -d --build` | migrate exited 0; web, worker, mysql, redis healthy |
| `GET localhost:8100/healthz`, `/readyz`, `/api/docs/`, `/api/schema/` | 200, 200, 200, 200 |
| `GET localhost:8100/` | 404 (no root route yet) |
| Host ports `3407`, `6480`, `8100` | reachable; `8000` released |
| `ai_market_data.django_migrations` | 18 rows applied |
| worker log | `Connected to redis://redis:6379/0`, `celery@... ready` |

Milestone 02 (custom user, JWT auth, throttling), run on the native MySQL 8
instance at `127.0.0.1:3306` (fresh database `ai_market_data`, empty before
`migrate`):

| Command | Result |
| --- | --- |
| `python manage.py makemigrations accounts` | created `apps/accounts/migrations/0001_initial.py` |
| `python manage.py migrate --noinput` | all migrations applied, including `token_blacklist` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `python manage.py check` | `System check identified no issues` |
| `python manage.py spectacular --file <temp>` | OpenAPI schema generated, exit 0 |
| `$env:REDIS_CACHE_URL=""; python -m pytest apps/accounts -q` | `22 passed` |
| Live `POST localhost:8100/api/v1/auth/register|token|refresh` (Docker MySQL) | 201, 200, 200; `me` 200; logout 204; replayed refresh 401 |
| Duplicate email over the same endpoint | 400 `A user with this email already exists.` |
| Compose `migrate` status (`showmigrations`) | `accounts.0001` and `token_blacklist.*` applied |
| `SHOW TABLES` on Compose MySQL | `accounts_user`, `token_blacklist_*`, Django tables present |

Host-level runs before milestone 02 used SQLite; the auth suite now runs against
MySQL because it exercises the unique email constraint.

## Known limitations

- Upload-job ownership (user A cannot read user B's job) is not enforceable yet:
  `apps/marketdata` has no job models. `IsAuthenticated` is the DRF default, and
  job lookups must be scoped by owner when milestone 03/04 adds the models.
- Password reset is not implemented, so its throttling scope does not exist and
  no email delivery is configured.
- Throttle counters use the configured cache. With the default `LocMemCache`
  they are per process, so multiple workers multiply the effective rate; set
  `REDIS_CACHE_URL` to share counters across workers.
- `/` returns 404 (no root route), and the Docker stack was not rebuilt for this
  milestone; its MySQL volume is separate from the host database used above.
- The image was built and run locally, but no production deployment was exercised.
