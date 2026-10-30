"""HTTP layer: wires settings, database and transformer into a FastAPI application."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from payload_cache.api.middleware import RequestContextMiddleware
from payload_cache.api.schemas import PayloadCreated, PayloadRead, PayloadRequest
from payload_cache.config import Settings
from payload_cache.core.service import PayloadService
from payload_cache.core.transformer import UpperCaseTransformer
from payload_cache.db.engine import make_engine
from payload_cache.db.eviction import evict_expired, purge_other_versions, run_periodically
from payload_cache.db.migrate import ensure_at_head, upgrade_to_head
from payload_cache.db.models import DIGEST_LENGTH
from payload_cache.observability.logs import configure_logging
from payload_cache.observability.metrics import render as render_metrics


@dataclass(frozen=True)
class Resources:
    """What the lifespan builds and the handlers use; ``app.state`` itself is untyped."""

    session_factory: sessionmaker[Session]
    transformer: UpperCaseTransformer
    transformer_version: str


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_session(resources: ResourcesDep) -> Iterator[Session]:
    with resources.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_service(resources: ResourcesDep, session: SessionDep) -> PayloadService:
    return PayloadService(session, resources.transformer, resources.transformer_version)


ServiceDep = Annotated[PayloadService, Depends(get_service)]

# Coarse guard applied before the body is read into memory and parsed; the schema bounds in
# api/schemas.py are the real limit. 2 * MAX_ITEMS strings of MAX_ITEM_LENGTH characters take
# 2 MB as ASCII and 12 MB with every character \u-escaped. The one in-bounds shape that does
# not fit is text outside the BMP escaped as surrogate pairs (12 bytes per character, up to
# 24 MB); it gets 413 instead of 422, accepted rather than doubling the memory one request
# may claim (cache-cli and httpx2 send raw UTF-8, which never exceeds 8 MB here).
MAX_BODY_BYTES = 16 * 1024 * 1024

router = APIRouter()

# Endpoints are plain ``def``: FastAPI runs them in a thread pool, which is the right fit for
# the synchronous SQLAlchemy session used underneath.


_LOCATION_HEADER = {
    "Location": {"description": "Where the payload can be read.", "schema": {"type": "string"}}
}


@router.post(
    "/payload",
    response_model=PayloadCreated,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {"description": "Payload created.", "headers": _LOCATION_HEADER},
        status.HTTP_200_OK: {
            "model": PayloadCreated,
            "description": "An identical payload already existed; its identifier is returned.",
            "headers": _LOCATION_HEADER,
        },
    },
)
def create_payload(
    body: PayloadRequest, service: ServiceDep, request: Request, response: Response
) -> PayloadCreated:
    """Generate a payload from two lists of strings.

    Every string is passed through the transformer (cached) and the results are interleaved:
    ``list_1[0], list_2[0], list_1[1], list_2[1], ...``. The identifier is derived from the
    request, so an identical request answers 200 with the identifier it already has.
    """
    result = service.create(body)
    # Derived from the route so it follows a renamed path or a root_path set by a proxy;
    # kept relative so the client resolves it against the URL it actually used.
    response.headers["Location"] = request.url_for(
        "read_payload", payload_id=result.payload_id
    ).path
    if not result.created:
        response.status_code = status.HTTP_200_OK
        return PayloadCreated(
            id=result.payload_id,
            message="Payload already exists; returning the existing identifier.",
        )
    return PayloadCreated(id=result.payload_id, message="Payload created.")


@router.get(
    "/payload/{payload_id}",
    response_model=PayloadRead,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No payload with this identifier.",
            "content": {"application/json": {"example": {"detail": "Payload not found."}}},
        }
    },
)
def read_payload(
    payload_id: Annotated[str, Path(pattern=rf"^[0-9a-f]{{{DIGEST_LENGTH}}}$")],
    service: ServiceDep,
) -> PayloadRead:
    """Read the output of a previously generated payload."""
    output = service.get_output(payload_id)
    if output is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Payload not found.")
    return PayloadRead(output=output)


@router.get("/health", include_in_schema=False)
def health(session: SessionDep) -> dict[str, str]:
    """Liveness and readiness in one: the database is the only dependency that can fail."""
    try:
        session.execute(text("SELECT 1"))
    except OperationalError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable."
        ) from error
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus exposition; cache hit ratio, payload outcomes and request latency."""
    body, content_type = render_metrics()
    return Response(body, media_type=content_type)


# The one piece of client text that travels in an error location is the name of an unknown
# field (``extra="forbid"``). A real misspelling is short; anything longer is cut rather
# than reflected in full.
MAX_LOCATION_PART_LENGTH = 64


def _bounded(part: int | str) -> int | str:
    if isinstance(part, str) and len(part) > MAX_LOCATION_PART_LENGTH:
        return part[:MAX_LOCATION_PART_LENGTH] + "..."
    return part


async def validation_error_handler(_request: Request, error: Exception) -> JSONResponse:
    """422 without echoing the offending input.

    FastAPI's default handler copies the input into every error, which reflects a request
    of up to MAX_BODY_BYTES back at the client, and a lone surrogate in it (legal JSON that
    the string validator rightly rejects) cannot be UTF-8 encoded, turning the 422 into a
    500. Type, location and message tell the client everything it needs; the one
    client-controlled part of a location, an unknown field's name, is bounded.
    """
    assert isinstance(error, RequestValidationError)
    detail = [
        {
            "type": item["type"],
            "loc": [_bounded(part) for part in item["loc"]],
            "msg": item["msg"],
        }
        for item in error.errors()
    ]
    return JSONResponse({"detail": detail}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    uvicorn calls this through ``--factory`` and tests pass their own settings, so importing
    the module reads no configuration and has no side effects.
    """
    settings = settings or Settings()
    # Process-wide, and done here rather than at import time for the same reason the engine
    # is: the factory is the one place that knows the settings.
    configure_logging(settings.log_level, echo_sql=settings.echo_sql)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        eviction: asyncio.Task[None] | None = None
        try:
            if settings.run_migrations:
                upgrade_to_head(engine)
            else:
                ensure_at_head(engine)
            session_factory = sessionmaker(engine, expire_on_commit=False)
            # Rows another transformer version produced can never be served again (the
            # version is in their keys). The version changes with the configuration, that is
            # at a start-up, so that is where they are removed (rolling deploys: db/eviction.py).
            purge_other_versions(session_factory, settings.transformer_version)
            app.state.resources = Resources(
                session_factory=session_factory,
                transformer=UpperCaseTransformer(settings.transformer_latency_seconds),
                transformer_version=settings.transformer_version,
            )
            if settings.cache_ttl_seconds is not None:
                sweep = partial(
                    evict_expired, session_factory, timedelta(seconds=settings.cache_ttl_seconds)
                )
                # Once now, so a service that was down for longer than the TTL starts clean,
                # then in the background for as long as the application runs.
                sweep()
                eviction = asyncio.create_task(
                    run_periodically(sweep, settings.eviction_interval_seconds),
                    name="cache-eviction",
                )
            yield
        finally:
            # Also on a failed start-up, so the pool's connections are closed, not leaked.
            if eviction is not None:
                eviction.cancel()
                with suppress(asyncio.CancelledError):
                    await eviction
            engine.dispose()

    app = FastAPI(
        title="Payload Cache Service",
        description=(
            "Generates payloads from two lists of strings and caches the results of the "
            "underlying transformer so repeated work is never redone."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)
    # Added last, so it is outermost: a 413 from the body limit is logged like any response.
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    return app
