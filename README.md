# Payload Cache Service

[![CI](https://github.com/hlibukh-coder/payload-cache-service/actions/workflows/ci.yml/badge.svg)](https://github.com/hlibukh-coder/payload-cache-service/actions/workflows/ci.yml)

A small FastAPI microservice that builds a payload from two lists of strings and caches
the results of a (simulated) external "transformer" so the same work is never done twice.
It ships with `cache-cli`, a command-line tool for exercising the service.

```
POST /payload  {"list_1": ["first string", ...], "list_2": ["other string", ...]}
  -> 201 {"id": "512df758...", "message": "Payload created."}

GET  /payload/512df758...
  -> 200 {"output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, ..."}
```

## How a request flows

1. **`main.py`** – `POST /payload` validates the body against `schemas.PayloadRequest`
   (two non-empty, bounded lists of strings of equal length, no unknown fields) and hands
   it to `PayloadService` with a request-scoped SQLAlchemy session.
2. **`service.py`** – derives the payload id from the canonical JSON of the request and the
   transformer version. If a payload with that id already exists the id is returned
   immediately (HTTP 200) without touching the transformer.
3. **`cache.py`** – otherwise every distinct string from both lists is looked up in
   `transformed_strings` in one query (chunked for very large inputs), under keys that also
   include the transformer version. Only strings that were never seen are sent to the
   transformer, and their results are written back with `INSERT ... ON CONFLICT DO NOTHING`.
4. **`service.py`** – interleaves the transformed strings (`a1, b1, a2, b2, ...`), stores
   the payload under its id (again `ON CONFLICT DO NOTHING`, with `RETURNING` to tell
   whether this request or a concurrent one created it) and commits.
5. `GET /payload/{id}` is a primary-key lookup on `payloads`.

## Caching design

Two tables, both **content-addressed** (`models.py`):

| table                 | key                                                   | value                         |
|-----------------------|-------------------------------------------------------|-------------------------------|
| `transformed_strings` | SHA-256 of `[transformer version, input string]`      | the transformer's output      |
| `payloads`            | SHA-256 of the canonical request + transformer version | the interleaved output string |

Why this shape:

- **Deterministic payload ids.** Hashing the canonical request (sorted keys, compact
  separators) means an identical request always maps to the same id, which is how
  "reuse payload identifier for payloads already generated" is satisfied without any
  lookup table or coordination between workers.
- **Hash keys instead of raw text.** Primary keys stay a fixed 64 characters regardless of
  input length, which keeps indexes compact and sidesteps PostgreSQL's limit on the size of
  indexed values.
- **The transformer version is part of every key.** The transformer stands in for an
  external service, and external services change. `TRANSFORMER_VERSION` is hashed into
  string keys and payload ids, so bumping it turns every cached result into a miss and gives
  new payloads new ids, instead of serving under an old id an output a different transformer
  produced. Each row also records its version, which is what lets rows of other versions be
  found and purged at start-up (`eviction.py`).
- **Bounded by age, when asked.** With `CACHE_TTL_SECONDS` set, a background sweep
  (`eviction.py`) deletes rows older than that from both tables: once at start-up, so a
  service that was down for longer than the TTL starts clean, then every
  `EVICTION_INTERVAL_SECONDS` for as long as it runs; `created_at` is indexed for it
  (revision 0003). An evicted payload answers 404 until the same request is posted again,
  which gives it the same id back. Unset, the cache keeps everything: the right default
  while the transformer is deterministic and storage is not the constraint.
- **Minimal transformer calls.** Within one request, repeated strings are de-duplicated
  before the lookup; across requests, every string ever transformed is served from the
  database. The test suite asserts the exact call counts.
- **Races are harmless by construction.** Two requests racing on the same string or the
  same payload write identical values, so `ON CONFLICT DO NOTHING` is the correct
  resolution and no request ever fails because another one was faster. Cached strings are
  inserted in key order, so two requests racing on overlapping strings queue behind each
  other instead of deadlocking on PostgreSQL, where an `ON CONFLICT` insert waits for an
  uncommitted row with the same key. The two `test_concurrent_*` tests in
  `tests/test_service.py` pin both properties down with threads.

## Running it

### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn --factory payload_cache.main:create_app --reload
```

Interactive docs: <http://localhost:8000/docs>. Configuration is read from the environment
(or a `.env` file), see `config.py`:

| variable                      | default                              | purpose                                   |
|-------------------------------|--------------------------------------|-------------------------------------------|
| `DATABASE_URL`                | `sqlite:///./data/payload_cache.db`  | SQLAlchemy URL, SQLite (3.35+) or PostgreSQL |
| `RUN_MIGRATIONS`              | `true`                               | migrate the schema on start-up (see below) |
| `ECHO_SQL`                    | `false`                              | log every SQL statement                   |
| `LOG_LEVEL`                   | `INFO`                               | threshold of the JSON log on stdout       |
| `TRANSFORMER_LATENCY_SECONDS` | `0`                                  | simulate a slow external service (0 to 60 s) |
| `TRANSFORMER_VERSION`         | `1`                                  | hashed into every cache key; bump it when the transformer changes |
| `CACHE_TTL_SECONDS`           | unset                                | delete cache rows older than this (unset: keep everything) |
| `EVICTION_INTERVAL_SECONDS`   | `300`                                | how often the TTL sweep runs              |

### Docker

```bash
docker compose up --build
```

The SQLite file lives on the `payload-data` volume, so the cache survives restarts. The
image runs as a non-root user; its `HEALTHCHECK` polls `GET /health`, which runs `SELECT 1`
and answers 503 when the database is unreachable.

`cache-cli` is installed in the image too, so the service can be exercised from inside the
container without installing anything on the host:

```bash
docker compose exec api cache-cli --repeat 3 --json '{"list_1": ["a"], "list_2": ["b"]}'
```

The same image works against PostgreSQL; `DATABASE_URL` picks the backend. Naming the
service keeps the SQLite `api` from starting alongside:

```bash
docker compose --profile postgres up --build api-postgres   # API on :8001, PostgreSQL on :5433
```

## Schema and migrations

The schema is managed with Alembic. The revision scripts live inside the package
(`src/payload_cache/migrations/`), so an installed application, the Docker image included,
migrates its own database: on start-up by default (`schema.py` applies pending revisions in
one transaction, DDL included on SQLite too, so a failure leaves the database as it was;
nothing pending is a no-op), or as a release step:

```bash
payload-cache-migrate        # same DATABASE_URL as the service; then RUN_MIGRATIONS=false
```

Several replicas starting at once would race on Alembic's version table, which is why a
deployment with more than one migrates once up front and tells the replicas not to. A
replica that is told not to still checks the revision at start-up and refuses to start
against a database that is behind, naming the command to run, rather than failing on its
first request with a missing-column error.

A database created before migrations existed (tables, no version table) is recognised and
recorded as revision 0001, so a service upgraded on an old volume starts normally. Revision
0002 empties both tables: their rows were keyed without a transformer version and can never
be looked up again; the cache refills on demand.

To change the schema, edit `models.py` and generate the revision from a checkout:

```bash
alembic revision --autogenerate -m "describe the change"   # alembic.ini points at the package
alembic upgrade head
```

`test_migrations.py` compares the migrated schema with the models on every run, so a model
change without a revision fails the suite instead of surfacing at deployment.

## Observability

Every log line is one JSON object on stdout, uvicorn's own lines included, so a log shipper
needs no parsing rules. The middleware in `middleware.py` gives each request an id, taken
from an incoming `X-Request-ID` header when it looks like an identifier and generated
otherwise, returns it in the response and tags every log line written while the request is
being handled (`logging_setup.py` reads it from a context variable, so handlers and the
cache do not pass it around). One access-log line per request names the method, the route
template, the status and the duration:

```json
{"time": "2026-10-29T10:12:03.412+00:00", "level": "INFO", "logger": "payload_cache.http",
 "message": "POST /payload -> 201", "request_id": "6f1c…", "method": "POST", "path": "/payload",
 "route": "/payload", "status": 201, "duration_ms": 3.81}
```

Successful health probes and metric scrapes are logged at `DEBUG` so the access log stays
about real traffic; a failing one, like every 5xx, is logged at `ERROR`. An unhandled
exception is logged with its traceback and the request id, and answered with a 500 that
carries the same `X-Request-ID`, so a client's bug report leads straight to the traceback.
uvicorn's own access log is turned off because this line supersedes it.

`GET /metrics` serves Prometheus metrics (`metrics.py`):

| metric                                         | labels                      | meaning                                                                 |
|------------------------------------------------|-----------------------------|-------------------------------------------------------------------------|
| `payload_cache_transform_strings_total`        | `result` = `hit` \| `miss`  | distinct strings per request served from the cache or sent to the transformer; misses are the transformer calls made |
| `payload_cache_payloads_total`                 | `result` = `created` \| `reused` | `POST /payload` outcomes                                           |
| `payload_cache_evicted_rows_total`             | `table`, `reason` = `version` \| `ttl` | rows removed by the start-up purge of other transformer versions and by the TTL sweep |
| `payload_cache_http_request_duration_seconds`  | `method`, `route`, `status` | latency histogram by route template; unrouted requests share the label `unmatched` and non-standard methods the label `other`, so client-chosen values cannot multiply the series |

The image runs one uvicorn worker, so the process-wide registry describes the whole
container; scale with replicas, each scraped on its own, rather than with `--workers`,
which would need the client library's multiprocess mode. Neither `/health` nor `/metrics`
is authenticated: expose them to the probe and the scraper, not through the public gateway.

## `cache-cli`

Installed with the package (`pip install -e .`). It sends a request `--repeat` times,
reads every payload back and reports the status and timing of each call, which is how the
caching can be observed from the outside: the first POST answers `201` and pays for every
transformer call, the following ones answer `200` from cache.

```
cache-cli [-H|--host URL] [-r|--repeat N] [-i|--input FILE|-] [-j|--json JSON] [-o|--output FILE|-] [-h|--help]
```

Exactly one of `--input`/`--json` is required; `--help` prints the full description.

```bash
cache-cli --repeat 3 --json '{"list_1": ["first string", "second string", "third string"],
                             "list_2": ["other string", "another string", "last string"]}'
cat request.json | cache-cli --input - --output report.json
```

```json
{
  "id": "512df758f001b543a8272d884fd97045f67ce8ac446bbf0dc2a6c64ae79d8a4a",
  "output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING",
  "consistent": true,
  "runs": [
    {"iteration": 1, "id": "512df758…", "status": 201, "post_ms": 347.26, "get_ms": 3.3},
    {"iteration": 2, "id": "512df758…", "status": 200, "post_ms": 2.32, "get_ms": 1.58},
    {"iteration": 3, "id": "512df758…", "status": 200, "post_ms": 1.59, "get_ms": 1.55}
  ]
}
```

(ids shortened; the run was made with `TRANSFORMER_LATENCY_SECONDS=0.05`, so the six
transformer calls of the first POST are visible.) A throwaway `GET /health` warms the
connection first, so iteration 1 measures the service rather than the TCP handshake.

Argument parsing is done with Pydantic Settings (`cli.py`): flags are validated and
normalised (`--host` must be an HTTP URL, `--repeat` a positive integer, `--input` an
existing file or `-`, exactly one of `--input`/`--json`), and the request body is validated
against the API schema before it is sent. Exit codes: `0` success, `1` the service could not
be reached, answered with an error or with a body that is not the API's (or disagreed with
itself across repeats), `2` invalid invocation, including an unreadable request file or an
unwritable report location.

## Tests

```bash
pytest
ruff check . && ruff format --check . && mypy
```

- **Unit:** hashing, the transformer, output interleaving, id determinism, cache keys, CLI
  parsing, the JSON log formatter, the eviction sweep and its retry loop.
- **Integration:** the cache and service against a real database, the documented example
  through the HTTP API, concurrent identical and overlapping requests, request ids and
  access-log lines, metric deltas through `/metrics`, migrations up and down (and the migrated
  schema diffed against the models), a transformer-version bump across two application
  start-ups, the TTL sweep running at start-up and on its timer, and `cache-cli` end to end
  against the application.

The suite runs against PostgreSQL as well, which exercises the PostgreSQL branch of the
one dialect-specific statement and the deadlock regression test. It drops and recreates the
tables in whatever database `TEST_DATABASE_URL` names, so point it only at a throwaway one
such as the compose container:

```bash
pip install -e ".[dev,postgres]"
docker compose --profile postgres up -d postgres
TEST_DATABASE_URL=postgresql+psycopg://payload:payload@localhost:5433/payload pytest
```

The same checks run on GitHub Actions for every push and pull request
(`.github/workflows/ci.yml`): lint and types, the suite on SQLite under Python 3.12 and
3.13 with the newest dependencies the declared ranges admit, the suite on PostgreSQL 17 with
exactly the versions in `requirements.lock` (what the image ships), and a smoke test that
builds the Docker image, waits for `/health` and runs `cache-cli` inside the container.

## Decisions and trade-offs

- **SQLAlchemy 2.0 rather than SQLModel.** Keeps API schemas (Pydantic) and persistence
  models (ORM) separate: the request schema has validation rules that make no sense on a
  table, and the tables have columns the API never exposes.
- **`httpx2` rather than `httpx`.** It is httpx's successor by the same author, and
  Starlette's `TestClient` is built on it, so `cache-cli` and the tests share one HTTP client
  and one set of exception types: the `HTTPStatusError` the CLI catches is the one the
  `TestClient` raises.
- **Synchronous endpoints.** FastAPI runs `def` endpoints in a thread pool, which fits a
  synchronous SQLAlchemy session and keeps the code straightforward. Going async would only
  pay off once the transformer is a real network call.
- **Migrations run by the application.** The scripts ship inside the package and the
  application applies them, on start-up or through `payload-cache-migrate`, so there is no
  separate deploy artefact to keep in step with the image. The first version of this service
  created the tables with `create_all`; a cache can be rebuilt, but a column added later (an
  eviction marker, a transformer version) could not have been, so the schema is versioned
  from the start rather than retrofitted under pressure.
- **Invalidation by version, not per key.** A cached result stays valid for as long as the
  transformer behaves the same, so the one invalidation handle is the transformer version:
  bump it and every key changes. There is deliberately no endpoint to drop individual
  entries, which would invite callers to work around a bad transformer result instead of
  fixing it. Age is the other, orthogonal limit: `CACHE_TTL_SECONDS` bounds how long any
  row lives, for deployments where storage rather than freshness is the constraint.
- **The database connection is not held across transformer calls.** The cache ends its
  read-only lookup transaction before calling the transformer, so a slow external service
  pins neither a pooled connection nor, on PostgreSQL, an open transaction. The results are
  written in a fresh, short transaction afterwards.
- **Duplicate transformer calls remain possible under concurrency.** Two requests missing on
  the same string at the same instant, in two threads of one process (FastAPI's thread pool)
  just as in two processes, both call the transformer; the second insert is simply ignored.
  `test_concurrent_identical_requests_converge_on_one_payload` therefore checks that the
  database converges, not the call count. Closing the gap would mean holding every other
  caller back until the first one's external call returns (an in-flight map per process, a
  lock across processes), which costs more than the occasional duplicate.
- **Strict request validation.** Unknown fields and empty lists are rejected (HTTP 422):
  both almost certainly indicate a client bug, and an empty payload is not useful. Requests
  are bounded as well (`schemas.py`: 1 000 strings per list, 1 000 characters per string;
  `main.py`: 16 MiB body, HTTP 413 beyond that), so one anonymous POST cannot queue tens of thousands
  of transformer calls or fill the cache with megabyte-sized rows. Strings that are legal
  JSON but can be neither hashed nor stored (NUL characters, lone surrogates) get a 422
  instead of failing with a 500 half-way through.
- **Logging configured by the application factory, not by the server.** `create_app`
  installs the JSON handler and redirects uvicorn's loggers through it. That is a process-wide
  side effect inside a factory, accepted because the factory is the one place that knows the
  settings, and because the alternative, a uvicorn `--log-config` file, would have to be kept
  in step with the code by hand.
- **No authentication or rate limiting.** Out of scope for the exercise; both belong in the
  reverse proxy or gateway in front of a service like this.
- **Dependencies locked for the image, ranged for the package.** `pyproject.toml` declares
  compatibility floors, as a library would (the floors were installed once, `fastapi==0.134`
  with `starlette==1.6`, and the suite run against them); `requirements.lock` pins what the
  image installs (frozen inside the same `python:3.13` image), so rebuilding a commit gives
  the same image and "the tests passed" says something about what ships. Regenerate it after
  changing the dependencies:

  ```bash
  docker run --rm -v "$PWD:/src:ro" python:3.13.18-slim sh -c \
    "pip install -q '/src[postgres]' && pip freeze --exclude payload-cache-service" > requirements.lock
  ```

- **Single-stage Docker image.** All dependencies ship as wheels, so a build stage would add
  complexity without shrinking the image. The dependency layer is installed before the
  source is copied so code changes rebuild quickly.

## Deviations from the task statement

- The task lists `-h` for both `--host` and `--help`. `-h` is universally help, so `--host`
  takes `-H` and `-h` keeps its conventional meaning.
- "Payload files" are stored as rows in the database rather than on disk: the database is
  already the persistence layer the task asks for, and a file store would add a second one.

## Project layout

```
src/payload_cache/
  main.py         FastAPI app factory, routes, dependencies
  middleware.py   request id + access-log line (raw ASGI)
  logging_setup.py JSON log formatter, uvicorn loggers redirected
  metrics.py      Prometheus collectors and the exposition for /metrics
  service.py      payload id, interleaving, orchestration
  cache.py        batched lookup / fill of transformer results
  transformer.py  the simulated external service
  models.py       ORM tables
  db.py           engine factory, dialect-specific ON CONFLICT insert
  schema.py       Alembic upgrade at start-up, payload-cache-migrate
  migrations/     Alembic environment and revision scripts (inside the package)
  eviction.py     start-up purge of other transformer versions, TTL sweep
  schemas.py      API contracts, shared with the CLI
  hashing.py      canonical JSON + SHA-256
  config.py       service settings
  cli.py          cache-cli
tests/            unit and integration tests (pytest)
alembic.ini       points the alembic command line at the package's migrations
requirements.lock pinned dependencies for the Docker image
Dockerfile, docker-compose.yml
.github/workflows/ci.yml
```
