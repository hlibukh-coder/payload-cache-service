# Payload Cache Service

A small FastAPI microservice that builds a payload from two lists of strings and caches
the results of an (simulated) external "transformer" so the same work is never done twice.
It ships with `cache-cli`, a command-line tool for exercising the service.

```
POST /payload  {"list_1": ["first string", ...], "list_2": ["other string", ...]}
  -> 201 {"id": "831db673...", "message": "Payload created."}

GET  /payload/831db673...
  -> 200 {"output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, ..."}
```

## How a request flows

1. **`main.py`** – `POST /payload` validates the body against `schemas.PayloadRequest`
   (two non-empty, bounded lists of strings of equal length, no unknown fields) and hands
   it to `PayloadService` with a request-scoped SQLAlchemy session.
2. **`service.py`** – derives the payload id from the canonical JSON of the request. If a
   payload with that id already exists the id is returned immediately (HTTP 200) without
   touching the transformer.
3. **`cache.py`** – otherwise every distinct string from both lists is looked up in
   `transformed_strings` in one query (chunked for very large inputs). Only strings that
   were never seen are sent to the transformer, and their results are written back with
   `INSERT ... ON CONFLICT DO NOTHING`.
4. **`service.py`** – interleaves the transformed strings (`a1, b1, a2, b2, ...`), stores
   the payload under its id (again `ON CONFLICT DO NOTHING`, with `RETURNING` to tell
   whether this request or a concurrent one created it) and commits.
5. `GET /payload/{id}` is a primary-key lookup on `payloads`.

## Caching design

Two tables, both **content-addressed** (`models.py`):

| table                 | key                                | value                          |
|-----------------------|------------------------------------|--------------------------------|
| `transformed_strings` | SHA-256 of the input string        | the transformer's output       |
| `payloads`            | SHA-256 of the canonical request   | the interleaved output string  |

Why this shape:

- **Deterministic payload ids.** Hashing the canonical request (sorted keys, compact
  separators) means an identical request always maps to the same id, which is how
  "reuse payload identifier for payloads already generated" is satisfied without any
  lookup table or coordination between workers.
- **Hash keys instead of raw text.** Primary keys stay a fixed 64 characters regardless of
  input length, which keeps indexes compact and sidesteps PostgreSQL's limit on the size of
  indexed values.
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
| `DATABASE_URL`                | `sqlite:///./data/payload_cache.db`  | any SQLAlchemy URL (SQLite or PostgreSQL) |
| `ECHO_SQL`                    | `false`                              | log every SQL statement                   |
| `TRANSFORMER_LATENCY_SECONDS` | `0`                                  | simulate a slow external service          |

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
  "id": "831db6735056b8b4eca24283fd7129380f4c8f94948fc77a2a28a61f191da401",
  "output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING",
  "consistent": true,
  "runs": [
    {"iteration": 1, "id": "831db673…", "status": 201, "post_ms": 347.26, "get_ms": 3.3},
    {"iteration": 2, "id": "831db673…", "status": 200, "post_ms": 2.32, "get_ms": 1.58},
    {"iteration": 3, "id": "831db673…", "status": 200, "post_ms": 1.59, "get_ms": 1.55}
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

- **Unit:** hashing, the transformer, output interleaving, id determinism, CLI parsing.
- **Integration:** the cache and service against a real SQLite file, the documented example
  through the HTTP API, concurrent identical requests, and `cache-cli` end to end against
  the application.

The suite runs against PostgreSQL as well, which exercises the PostgreSQL branch of the
one dialect-specific statement and the deadlock regression test. It drops and recreates the
tables in whatever database `TEST_DATABASE_URL` names, so point it only at a throwaway one
such as the compose container:

```bash
pip install -e ".[dev,postgres]"
docker compose --profile postgres up -d postgres
TEST_DATABASE_URL=postgresql+psycopg://payload:payload@localhost:5433/payload pytest
```

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
- **Schema via `create_all` on start-up, not migrations.** A deliberate shortcut: the data is
  a cache that can be rebuilt. A real deployment would add Alembic.
- **No cache invalidation.** Cached results are assumed valid forever because the
  transformer is deterministic. If the transformer changed behaviour, its version would have
  to become part of both cache keys.
- **The database connection is not held across transformer calls.** The cache ends its
  read-only lookup transaction before calling the transformer, so a slow external service
  pins neither a pooled connection nor, on PostgreSQL, an open transaction. The results are
  written in a fresh, short transaction afterwards.
- **Duplicate transformer calls remain possible across workers.** Two processes missing on
  the same string at the same instant both call the transformer; the second insert is
  simply ignored. Avoiding that would need locking around an external call, which costs more
  than the occasional duplicate.
- **Strict request validation.** Unknown fields and empty lists are rejected (HTTP 422):
  both almost certainly indicate a client bug, and an empty payload is not useful. Requests
  are bounded as well (`schemas.py`: 1 000 strings per list, 1 000 characters per string,
  16 MiB body, HTTP 413 beyond that), so one anonymous POST cannot queue tens of thousands
  of transformer calls or fill the cache with megabyte-sized rows. Strings that are legal
  JSON but can be neither hashed nor stored (NUL characters, lone surrogates) get a 422
  instead of failing with a 500 half-way through.
- **No authentication or rate limiting.** Out of scope for the exercise; both belong in the
  reverse proxy or gateway in front of a service like this.
- **Dependencies locked for the image, ranged for the package.** `pyproject.toml` declares
  compatibility floors, as a library would; `requirements.lock` pins what the image installs
  (frozen inside the same `python:3.13` image), so rebuilding a commit gives the same image
  and "the tests passed" says something about what ships. Regenerate it after changing the
  dependencies:

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
  service.py      payload id, interleaving, orchestration
  cache.py        batched lookup / fill of transformer results
  transformer.py  the simulated external service
  models.py       ORM tables
  db.py           engine factory, dialect-specific ON CONFLICT insert
  schemas.py      API contracts, shared with the CLI
  hashing.py      canonical JSON + SHA-256
  config.py       service settings
  cli.py          cache-cli
tests/            unit and integration tests (pytest)
requirements.lock pinned dependencies for the Docker image
Dockerfile, docker-compose.yml
```
