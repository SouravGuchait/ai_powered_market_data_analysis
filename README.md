AI Powered Backend Application

Market-data backend: Django + Django REST Framework, MySQL, Celery/Redis, and
Pandas for processing. Current state: custom email/username user model and JWT
authentication, the market-data ORM schema (ingestion jobs, files,
exchanges/instruments, daily price records, validation issues, audit events,
metric snapshots), a synthetic-data generator, the upload API with durable
job records and owner-scoped status, and bounded CSV/JSON/XLSX ingestion
processing. Daily returns and simple moving averages are calculated after
ingestion and stored as `MetricSnapshot` rows.

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
| `MAX_UPLOAD_SIZE_BYTES` | `52428800` (50 MiB) | Streamed upload limit; larger requests get `413`. Size columns are 64-bit, so raising it needs no migration |
| `INGESTION_CHUNK_SIZE` | `5000` | Rows per pandas frame/worker chunk |
| `INGESTION_DB_BATCH_SIZE` | `1000` | Rows per `bulk_create` call; the final partial batch is written by the same call |
| `INGESTION_DEFAULT_EXCHANGE` | `XNYS` | Exchange for files without an `exchange` column; created on first use |
| `INGESTION_DEFAULT_EXCHANGE_TIMEZONE` | `America/New_York` | Timezone for the auto-created default exchange |

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

## Uploads and jobs (milestone 04)

`POST /api/v1/uploads/` accepts one CSV, JSON, or XLSX file, records an
`IngestionJob` plus `IngestionFile`, and queues the job after the transaction
commits. `GET /api/v1/jobs/{uuid}/` returns status and row counts to the owning
user only. The worker processes queued jobs as described under
"Ingestion processing (milestone 05)".

| Method | Path | Access | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/v1/uploads/` | Bearer access token | Multipart file or raw body; `202 Accepted` |
| `GET` | `/api/v1/jobs/{uuid}/` | Bearer access token, owner only | `200`; another user gets `404` |

### Request formats

- Multipart: one file in the `file` field. Send `Idempotency-Key` as a header
  or `idempotency_key` as a form field, not both with different values.
- Raw: send the bytes as the request body with `Content-Type: text/csv`,
  `application/json`, or the XLSX MIME type. Raw `text/plain` and
  `application/octet-stream` bodies require an `X-Filename` header.
- Extensions `.csv`, `.json`, and `.xlsx` are required. Extension and MIME type
  are hints; the first 64 KiB are checked before a job is created. CSV/JSON must
  be UTF-8 (a UTF-8 BOM is accepted).
- CSV header must contain `symbol`, `timestamp`, `open`, `high`, `low`, `close`,
  and `volume`; extra columns are allowed. JSON must start with `[` or `{`.
  XLSX must start with the ZIP signature.
- Files are streamed to storage in 64 KiB chunks. `MAX_UPLOAD_SIZE_BYTES`
  bounds the body. No full-file row parsing happens inside the request.

Successful response:

```json
{"id": "4e55b7a0-5e6c-4b12-9e57-0e5f4f3c4a11", "status": "queued", "replayed": false}
```

The response also sets `Location` to the job detail path. `202` means the job
was recorded and handed to the queue; it does not mean processing finished.

Errors are stable and do not include local paths or traces:

```json
{"error": {"code": "missing_columns", "message": "CSV header is missing required columns: open, high."}}
```

| Code | HTTP | Meaning |
| --- | --- | --- |
| `file_required`, `multiple_files` | `400` | No file, or more than one file |
| `unsupported_file_type` | `415` | Extension is not csv/json/xlsx |
| `unsupported_media_type`, `unsupported_charset` | `415` | MIME or declared charset is not accepted |
| `filename_required` | `400` | Raw body cannot be typed without `X-Filename` |
| `file_too_large` | `413` | Above `MAX_UPLOAD_SIZE_BYTES` |
| `empty_file` | `400` | Zero bytes or no header content |
| `missing_columns`, `invalid_file_content` | `400` | Header/content probe failed |
| `invalid_idempotency_key`, `conflicting_idempotency_key` | `400` | Bad or inconsistent key |
| `storage_error` | `503` | Storage backend failed; nothing was recorded |
| `enqueue_failed` | `503` | Broker dispatch failed; the job and stored object were removed |
| `persistence_error` | `503` | Database write failed; the stored object was removed |
| `invalid_request` | `400` | Multipart/body could not be parsed |
| `internal_error` | `500` | Unexpected request failure, details only in logs |

### Idempotency and retries

`Idempotency-Key` is optional, 1-64 characters (`A-Z`, `a-z`, `0-9`, `.`, `_`,
`:`, `-`), and scoped to the owner. A repeat with the same owner and key returns
the original job with `202` and `"replayed": true`; no second file or job is
created. A different user may reuse the same key. Requests without a key are
not deduplicated. After a failed or cancelled job, retry with a new key: the
original key continues to resolve to the original job.

### Storage and cleanup

Storage keys are generated as
`uploads/<user_id>/<job_id>/<random>.<extension>`. The uploaded filename is
stored only as sanitized `IngestionFile.original_filename`; it never becomes a
path. `storage_key` is not exposed by the API or the admin change form.

The default backend is Django's local media storage (`DJANGO_MEDIA_ROOT`). The
same calls use Django's storage interface, so an S3 backend that implements
that interface can replace it without changing views. The stored object is kept
for the worker and is deleted by ingestion cleanup after processing succeeds or
fails. Files are deleted immediately when validation, storage, database
persistence, or enqueue fails, and uploaded file handles are closed at the end
of the request.

### Dispatch and status

The job is committed inside `transaction.atomic()`, and
`transaction.on_commit` calls Celery for
`apps.marketdata.tasks.process_ingestion_job` with only the job UUID. If dispatch
fails, the API returns `503`, deletes the job row and stored object, and does not
return `202`. The worker entrypoint and failure handling are described under
"Ingestion processing (milestone 05)".

Job statuses are `queued -> running -> completed|failed|cancelled`; `running`
is the processing state in DATA_MODEL.md. Transitions are enforced by
`IngestionJob.assert_transition()` and terminal states are final. The detail
response includes `id`, `status`, `source_type`, `rows_received`,
`rows_accepted`, `rows_rejected`, `error_code`, `error_summary`, and timestamps.

Example (PowerShell 7+ for `-Form`):

```powershell
$login = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/auth/login/ `
  -ContentType application/json `
  -Body '{"username":"owner","password":"CorrectHorseBatteryStaple9"}'
$job = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/uploads/ `
  -Headers @{ Authorization = "Bearer $($login.access)"; "Idempotency-Key" = "upload-1" } `
  -Form @{ file = Get-Item .\sample_data\synthetic_market_clean.csv }
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/api/v1/jobs/$($job.id)/" `
  -Headers @{ Authorization = "Bearer $($login.access)" }
```

## Ingestion processing (milestone 05)

`apps.marketdata.tasks.process_ingestion_job` consumes only the job UUID that
the upload API queued. The worker claims `queued -> running` atomically, opens
the stored upload, processes it in bounded chunks, and records `completed` or
`failed`. A terminal or already-running job is skipped, so a duplicate Celery
delivery does not process the same job twice. On failure, the job's partial
`PriceRecord` rows are deleted, `ValidationIssue` rows are kept for diagnosis,
and the stored upload is removed. The stored upload is also removed after a
successful run.

### Formats and row numbers

All formats require the columns `symbol`, `timestamp`, `open`, `high`, `low`,
`close`, and `volume`. Header cells are stripped, lowercased, and may be
reordered; extra columns are ignored. `exchange`, `name`, `asset_class`,
`currency`, and `adjusted_close` are optional. Files must be UTF-8.

| Format | Reader | `row_number` meaning |
| --- | --- | --- |
| CSV | stdlib `csv.reader`, frames built per chunk | Physical line where the record starts; header is line 1 |
| JSON | Incremental array decoding with `json.JSONDecoder.raw_decode` | 1-based record index in the array |
| XLSX | `openpyxl` read-only active worksheet | 1-based worksheet row; the header has its own row |

CSV parsing deliberately uses the stdlib reader rather than
`pandas.read_csv(chunksize=...)`: pandas either raises on the first malformed
line or skips it without a reliable line number. The stdlib reader makes the
source line number exact and lets every malformed record become a
`malformed_row` issue. pandas still performs the vectorized validation and
normalization for each chunk. A blank CSV/XLSX row is skipped, not counted.

JSON arrays are streamed. A single JSON object carrying the canonical columns
is treated as one record; that form is read fully because there is no array to
stream. An XLSX workbook is opened read-only and only one chunk of rows is
materialized at a time.

### Validation and issues

Every failed rule creates a `ValidationIssue` with the row number, a stable
error code, a short message, and a bounded `raw_row` preview (at most 12 fields,
64 characters each). A row can have multiple issues. Rows with any issue are
not persisted.

| Rule | Issue code |
| --- | --- |
| Symbol blank or longer than 32 characters | `missing_symbol`, `invalid_symbol` |
| Timestamp unparseable or missing | `invalid_timestamp` |
| Price missing, non-numeric, infinite, `<= 0`, or above `DecimalField(18, 6)` | `missing_price`, `non_numeric_price`, `non_finite_price`, `non_positive_price`, `price_out_of_range` |
| `high < low`, or `open`/`close` outside `[low, high]` after rounding | `invalid_ohlc_range`, `price_out_of_bounds` |
| Optional `adjusted_close` invalid | `invalid_adjusted_close` |
| Volume missing, non-numeric, infinite, negative, fractional, or above `2^63-1` | `missing_volume`, `non_numeric_volume`, `non_finite_volume`, `negative_volume`, `invalid_volume`, `volume_out_of_range` |
| Exchange not configured | `unknown_exchange` |
| Canonical record already exists or repeats in the file | `duplicate_record` |
| Wrong CSV field count, non-object JSON value, wrong XLSX column count | `malformed_row` |

File-level failures fail the job instead of storing partial data: empty input
(`empty_file`), missing required columns (`missing_columns`), a malformed header
(`invalid_header`), non-UTF-8 CSV/JSON (`invalid_encoding`), and unreadable
JSON/XLSX (`malformed_input`). A file whose rows are all invalid completes with
`rows_accepted = 0` and the issues attached; the job is not marked failed
because the file itself was processed.

`rows_received` counts data records, including malformed records and duplicates.
`rows_accepted` is the number of `PriceRecord` rows committed for the job.
`rows_rejected = rows_received - rows_accepted`, so it counts rows, not issues;
one rejected row can have several issues.

### Normalization and resolution

- Symbols are stripped and uppercased. Instruments are created on first use and
  are unique per `(exchange, symbol)`; name, asset class, and currency come from
  the first row that creates the instrument and are not updated by later rows.
- A file without an `exchange` column uses `INGESTION_DEFAULT_EXCHANGE`. That
  exchange is created on first use with
  `INGESTION_DEFAULT_EXCHANGE_TIMEZONE`. Any other exchange code in a file must
  already exist in the `Exchange` table, otherwise its rows are rejected with
  `unknown_exchange`.
- Timestamps: a date-only value is the trading date. A naive datetime is
  interpreted in the exchange timezone. An offset-aware datetime is converted to
  the exchange timezone before the date is taken.
- Prices are parsed as decimals, rounded half-up to six places, then checked for
  positivity and OHLC shape. Volume must be a whole number in
  `[0, 2^63-1]`.

### Duplicate policy

Duplicates are rejected, never overwritten, matching `DATA_MODEL.md`. The first
valid row for an `(instrument, trading_date)` in a file wins; later copies in the
same chunk, in a later chunk, or in an earlier job become `duplicate_record`
issues. Cross-chunk and cross-job state comes from the database existence check,
so deduplication does not grow an in-memory set per file. `bulk_create(...,
ignore_conflicts=True)` protects the unique constraint against a concurrent
writer; a row lost only to that race is counted as rejected without its own
issue.

### Bounded processing

- `INGESTION_CHUNK_SIZE` (default 5000) bounds the frame handed to pandas.
- `INGESTION_DB_BATCH_SIZE` (default 1000) bounds each `bulk_create`.
- Each chunk is validated and persisted before the next is read, so failed
  rows, model instances, and file contents are not accumulated for the whole
  file. The final partial batch is written by the same `bulk_create` call.
- The instrument resolver caches instruments for the job; its size follows the
  number of distinct instruments, not rows. Duplicate detection is a per-chunk
  database query keyed by the chunk's instruments and date span.
- The processing package imports no Django request objects or views. Parsing and
  normalization (`apps/marketdata/ingestion/`) are ORM-free; persistence and job
  orchestration live under `apps/marketdata/jobs/ingestion/`.
- Daily fractional returns are calculated in
  `apps/marketdata/ingestion/metrics.py` and persisted by
  `apps/marketdata/jobs/ingestion/metrics.py` after accepted rows are committed.
  Returns are `close / prior_close - 1`, grouped by symbol and ordered by date;
  chunk state carries the last valid close. Invalid/missing closes do not
  advance state, so a return can span an invalid observation. Duplicate
  symbol/date observations are rejected by the pure calculator, and canonical
  persisted dates are unique per instrument. Snapshots are stored as fractions
  at eight decimal places. The first observation has no snapshot because its
  return is undefined and `MetricSnapshot.value` is non-null. The calculator
  reads complete canonical history in bounded ORM batches and stores values
  only for the completed job's rows.

Daily-return verification on 2026-10-20:

- `venv\\Scripts\\python.exe -m pytest -q apps/marketdata/tests/test_metrics.py` - 7 passed.
- `venv\\Scripts\\python.exe manage.py check` - no issues.
- `venv\\Scripts\\ruff.exe check apps/marketdata/ingestion/metrics.py apps/marketdata/jobs/ingestion/metrics.py apps/marketdata/jobs/ingestion/pipeline.py apps/marketdata/tests/test_metrics.py` - all checks passed.

### Simple moving averages (milestone 07)

The ingestion completion hook also stores a five-observation simple moving
average of canonical close prices. The observation window is per instrument,
ordered by trading date; calendar gaps do not change its size. Invalid closes
are skipped by the pure calculator, while canonical `PriceRecord` closes are
positive by database constraint. The first four observations have no snapshot.
Snapshots use `metric_type=moving_average`, `lookback_days=5`, version `1`,
and metadata `{"units":"price","window_type":"observations"}`. As with
daily returns, complete canonical history is streamed in bounded ORM batches
and only rows from the completed job receive snapshots. The natural key makes
reprocessing idempotent.

Moving-average verification on 2026-10-21:

- `venv\\Scripts\\python.exe -m pytest -q apps/marketdata/tests/test_metrics.py` - 11 passed against configured MySQL.
- `venv\\Scripts\\python.exe manage.py check` - no issues.
- `venv\\Scripts\\ruff.exe check apps/marketdata/ingestion/metrics.py apps/marketdata/jobs/ingestion/metrics.py apps/marketdata/jobs/ingestion/pipeline.py apps/marketdata/tests/test_metrics.py` - all checks passed.

The SMA window defaults to five; callers of the pure calculator and persistence
class can supply another positive window. Existing snapshots are not refreshed
if canonical history is later corrected; a correction workflow is outside this
milestone.

Worker logs include the job ID, source type, chunk and batch sizes, received/
accepted/rejected counts, issue count, duration, and a failure category. Raw
records, row contents, and file contents are not logged.

## Data models (milestone 03)

`apps/marketdata/models.py` implements the approved schema with database-level
constraints; the full decision list (deletion policies, duplicate policy,
precision, index justification) lives in `DATA_MODEL.md`.

- `Exchange`, `Instrument`: `Instrument` is unique per `(exchange, symbol)`;
  exchange codes, symbols, and currency codes are uppercased on save. Unknown
  exchange timezones are rejected in `clean()`.
- `PriceRecord`: one canonical row per `(instrument, trading_date)`; conflicting
  imports must be rejected by the ingestion layer, never overwritten. Prices
  are `DecimalField(18, 6)`; all prices must be positive and the OHLC shape
  (`low <= open|close <= high`) is enforced by MySQL check constraints. Volume
  is an unsigned big integer.
- `IngestionJob`: UUID key, user-owned (`created_by`), lifecycle
  `queued -> running -> completed|failed|cancelled` with no legal exit from a
  terminal state (`clean()` + `assert_transition()`). Counters cannot be
  negative or exceed `rows_received` (DB checks). `idempotency_key` is
  optional but unique per owner. Job state lives in MySQL only.
- `IngestionFile`: storage-relative `storage_key` (kept out of the admin and
  the API), lowercase SHA-256 `checksum`, size and content type. Files live in
  configured storage, never in a database column.
- `ValidationIssue`: multiple issues per row allowed; indexed by job+row and
  job+error code. `raw_row` stays out of the admin change form.
- `AuditEvent`: nullable user, stable action names, `GenericIPAddressField`,
  bounded JSON metadata; indexed by `(user, created_at)` and
  `(object_type, object_id)`.
- `MetricSnapshot`: persisted analytics only; values are fractions with
  `DecimalField(20, 8)`. `lookback_days = 0` marks non-windowed metrics so the
  unique natural key
  `(instrument, as_of_date, metric_type, lookback_days, calculation_version)`
  never relies on NULL duplicates.

All eight models are registered in `apps/marketdata/admin.py` with list
displays, search/filter support, raw-id inputs for large foreign keys, and
pagination; storage keys and raw row payloads are hidden there.

### Synthetic data

Deterministic offline generator for development and testing (no database,
Django config, or market API):

```powershell
.\venv\Scripts\python.exe scripts\generate_market_data.py --symbols 5 --days 100 --seed 42 --output-dir sample_data
```

Options: `--symbols`, `--days`, `--seed`, `--output-dir`, `--start-date`;
`--help` lists all. Same seed produces byte-identical CSV output. It writes:

| File | Content |
| --- | --- |
| `synthetic_market_clean.csv/json/xlsx` | valid daily OHLCV rows, shared trading dates |
| `synthetic_market_dirty.csv/json/xlsx` | same shape with missing fields, bad OHLC shape, negative volume/price, malformed.timestamp strings, bad types, null closes, empty strings, duplicate dates |
| `synthetic_market_empty.csv/json/xlsx` | empty datasets (0-byte CSV/JSON, header-only XLSX) |
| `synthetic_market_unexpected_columns.csv` | otherwise-valid rows plus an extra `unexpected_extra` column |

Rows always contain the documented columns `exchange, symbol, name,
asset_class, currency, timestamp, open, high, low, close, adjusted_close,
volume`; `timestamp` is an ISO date string. Dirty files stay distinguishable
by name, and the clean files are asserted corruption-free at generation time.

### Ownership

`apps/accounts/permissions.py:IsOwner` allows an object only when
`obj.owner_id == request.user.pk`. The job detail endpoint filters
`IngestionJob` by `created_by=request.user` and returns `404` for another user's
job, so existence is not disclosed. The cross-user cases are covered by
`apps/accounts/tests/test_permissions.py` and
`apps/marketdata/tests/test_uploads.py`.

## Layout

- `config/` - settings, URLs, WSGI/ASGI, startup migration hook, Celery bootstrap
- `config/settings_test.py` - test settings; keeps the configured database and
  forces LocMemCache so tests need no Redis
- `apps/accounts/` - custom user, manager, email-or-username backend, admin,
  serializers, JWT views, throttling scopes, owner permission, tests
- `apps/accounts/tests/integration/` - auth flow, migration, and startup probes
- `apps/marketdata/` - schema (models, migration, admin), upload validation,
  storage adapter, views, serializers, URLs, processing task, tests
- `apps/marketdata/ingestion/` - ORM-free CSV/JSON/XLSX readers, header and
  value validation, canonical normalization, metric hook
- `apps/marketdata/jobs/ingestion/` - queue dispatch, persistence, bounded
  dataset pipeline, worker claim/completion/cleanup
- `apps/marketdata/tests/integration/` - real Celery broker connectivity probe
- `scripts/generate_market_data.py` - deterministic synthetic OHLCV fixtures
- `sample_data/` - optional local example output of the generator (not committed)
- `manage.py` - command entrypoint
- `pytest.ini` - `DJANGO_SETTINGS_MODULE=config.settings_test`, tests under `apps/`

## Tests

```powershell
# Full suite, using the database configured by .env (MySQL by default)
.\venv\Scripts\python.exe -m pytest -q

# Integration marker only
.\venv\Scripts\python.exe -m pytest -q -m integration
```

Integration tests live in `apps/accounts/tests/integration/` and
`apps/marketdata/tests/integration/`:

- `test_auth_flow.py` drives register, login by username and email, `/me/`,
  refresh rotation, logout blacklisting, and CSRF-free API requests.
- `test_schema.py` starts fresh Django processes for two probes: migration
  `0002` against legacy `0001` rows and WSGI startup table creation. The probes
  use `probe_settings.py` and isolated SQLite files, so they do not touch the
  configured MySQL database.
- `test_broker.py` checks a real Celery broker connection and skips when Redis
  is unavailable.

The rest of the suite covers registration and email normalization, duplicate
username and email at the serializer and database layers, password validators,
login with username and with email, JWT refresh and blacklist logout, the
unauthenticated boundary, CSRF-free API POSTs, cross-user ownership denial,
superuser/admin creation, throttling for the credential endpoints, market-data
constraints, upload behavior (multipart/raw formats, size and content probes,
idempotent replay, owner scoping, post-commit dispatch, and failure cleanup),
and ingestion processing (chunking, malformed rows, header validation, numeric
and OHLC rules, timezone normalization, cross-chunk and cross-job duplicates,
all-invalid inputs, failed-job rollback, file cleanup, and generated CSV/JSON/
XLSX fixtures).

Run database-backed suites sequentially. Two pytest processes share the
configured MySQL test database; starting them in parallel fails with an existing
`django_migrations` table.

## Verification (milestone 02, run 2026-10-20, Python 3.12.0)

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

## Verification (milestone 03, run 2026-10-20, Python 3.12.0, Django 5.2.18)

| Command | Result |
| --- | --- |
| `python manage.py check` | `System check identified no issues (0 silenced)` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `python manage.py migrate` against the configured MySQL database | `Applying marketdata.0001_initial... OK` |
| `python manage.py showmigrations marketdata` | `[X] 0001_initial` |
| Generated migration review | Creation-only operations (create model/index/constraint), no `Delete`/`Alter` destructive ops |
| `pytest -q` with the MySQL test database | `75 passed` (46 new marketdata tests + 29 previous accounts tests) |
| `pytest -q -m integration` | `3 passed` |
| `ruff check apps config scripts manage.py` | `All checks passed!` |
| `python scripts/generate_market_data.py --symbols 5 --days 100 --seed 42 --output-dir sample_data` | 10 files, 3128 fixture rows; clean CSV: 500 rows, dirty CSV: 501 rows |
| Schema probes on MySQL | duplicate `(instrument, trading_date)` -> IntegrityError; duplicate idempotency key per owner -> IntegrityError; check-constraint violations -> IntegrityError; negative counters/overflow -> DataError; all named indexes present via `connection.introspection.get_constraints` |

Constraints verified only against MySQL 8 (`InnoDB`): unsigned counters and
check constraints; other backends are not wired in this project.

## Verification (milestone 04, run 2026-10-20, Python 3.12.0, Django 5.2.18)

| Command | Result |
| --- | --- |
| `python manage.py check` | `System check identified no issues (0 silenced)` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `python manage.py migrate --noinput` | `Applying marketdata.0002_ingestionfile_size_bigint... OK` |
| `pytest -q` with the MySQL test database | `101 passed` (24 upload tests, 1 size test, 1 broker integration test) |
| `pytest -q -m "not integration"` | `97 passed, 4 deselected` |
| `pytest -q -m integration` | `4 passed, 97 deselected` |
| `ruff check apps config scripts manage.py` | `All checks passed!` |
| `python manage.py spectacular --file <temp>` | exit 0, schema generated |
| Real Celery broker connection through `CELERY_BROKER_URL` | Redis was available; integration test passed |

The upload tests mock Celery dispatch, use a temporary local storage backend,
and cover missing files, wrong extension, mismatched MIME, oversized
and empty files, malformed headers/JSON/XLSX, non-UTF-8 CSV, unauthenticated
access, cross-user job access, post-commit dispatch, storage and enqueue
failures, duplicate idempotency keys, owner-scoped keys, and path-traversal
filenames. No live market API is used.

## Verification (milestone 05, run 2026-10-20, Python 3.12.0, Django 5.2.18)

| Command | Result |
| --- | --- |
| `python manage.py check` | `System check identified no issues (0 silenced)` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `pytest -q` with the MySQL test database | `126 passed` (25 new ingestion tests) |
| `pytest -q -m "not integration"` | `122 passed, 4 deselected` |
| `pytest -q -m integration` | `4 passed, 122 deselected` |
| `ruff check apps config scripts manage.py` | `All checks passed!` |
| `python manage.py spectacular --file <temp>` | exit 0, 9,116-byte schema |

The new tests use fixed inline CSV/JSON/XLSX fixtures plus files generated by
`scripts/generate_market_data.py`; no live market API is used. They cover empty
inputs, missing/reordered headers, malformed CSV rows and JSON, non-UTF-8
bytes, blank symbols, unknown exchanges, timezone-aware and naive timestamps,
NaN/infinities/string numerics, OHLC bounds, negative/fractional volume,
in-chunk and cross-chunk duplicates, conflicts with earlier jobs, all-invalid
files, the final partial batch, metric-hook fan-out, failed-job rollback,
stored-file cleanup, duplicate task delivery, and log fields. Ingestion ran
against MySQL 8; no SQLite ingestion run was made.

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
- Type checkers are not clean on the current tree: `basedpyright apps` reports 73
  errors and 2,101 warnings, and `pyrefly check apps` reports 22 errors. Most are
  pre-existing Django/DRF typing issues; the new ingestion code adds pandas
  typing complaints. No type-check gate is enforced.
- When broker dispatch fails, the job row, `IngestionFile`, and stored object
  are deleted and the request returns `503`. No failed job row is retained
  because `queued -> failed` is not a legal transition in DATA_MODEL.md;
  retry with a new idempotency key. Dispatch runs inside the request and uses
  Celery's broker connection defaults, so a slow broker can delay the response.
- The storage abstraction uses Django's storage API and was verified only with
  local `FileSystemStorage`. No S3 backend is installed or tested; an S3
  backend may buffer writes differently.
- Upload content checks inspect only the first 64 KiB (CSV header, JSON start,
  XLSX ZIP signature). Full row, encoding, and value validation happens in the
  worker after `202`; errors there mark the job failed.
- `MAX_UPLOAD_SIZE_BYTES` is the practical upload ceiling (default 50 MiB).
  `IngestionJob.file_size_bytes` and `IngestionFile.file_size_bytes` are 64-bit,
  so raising the limit within storage and worker capacity needs no schema
  change. Truly unlimited uploads are not supported: unbounded bodies can
  exhaust temp disk, storage, and worker capacity.
- No cancel or retry endpoint exists. A failed job is terminal; upload the file
  again with a new idempotency key.
- Ingestion is not one long transaction. Each chunk is committed as it is
  processed; on failure the job's `PriceRecord` rows are deleted and the job is
  marked failed. If the worker process dies mid-run, the job can remain
  `running` with partial rows and there is no lease or reaper yet.
- A job with multiple `IngestionFile` rows processes only the first. The upload
  API creates exactly one; the worker does not merge multiple files.
- The instrument cache grows with distinct instruments in a job, not rows. A
  pathological file with millions of distinct symbols would grow that cache.
  Duplicate detection is a database query per chunk over the chunk's instrument
  set and min/max date span.
- CSV parsing uses the stdlib reader for accurate malformed-row line numbers
  instead of `pandas.read_csv(chunksize=...)`. pandas is still used per chunk for
  validation and normalization. A JSON root object is read fully; only JSON
  arrays are streamed. XLSX parsing depends on `openpyxl` read-only mode.
- `rows_rejected` counts rows, not issues; a single row can create several
  `ValidationIssue` rows. `raw_row` previews are capped at 12 fields and 64
  characters per value.
- Cross-job write races are handled by the unique constraint plus
  `INSERT IGNORE`; a row lost only to that race is counted rejected without an
  individual issue. Metrics are not calculated or persisted yet; only the
  `MetricCalculator` hook exists.
- `sample_data/` was generated as an example; the repository does not track it.
- No Docker, deployment, or AWS configuration; `gunicorn` is listed for Linux
  container serving but was not run on Windows.
- Changing `AUTH_USER_MODEL` after a database has production data requires a
  migration plan; this milestone assumes no production data.
