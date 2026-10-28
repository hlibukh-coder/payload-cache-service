"""HTTP layer: wires settings, database and transformer into a FastAPI application."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from payload_cache.config import Settings
from payload_cache.db import make_engine
from payload_cache.models import DIGEST_LENGTH, Base
from payload_cache.schemas import PayloadCreated, PayloadRead, PayloadRequest
from payload_cache.service import PayloadService
from payload_cache.transformer import UpperCaseTransformer


@dataclass(frozen=True)
class Resources:
    """What the lifespan builds and the handlers use; ``app.state`` itself is untyped."""

    session_factory: sessionmaker[Session]
    transformer: UpperCaseTransformer


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_session(resources: ResourcesDep) -> Iterator[Session]:
    with resources.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_service(resources: ResourcesDep, session: SessionDep) -> PayloadService:
    return PayloadService(session, resources.transformer)


ServiceDep = Annotated[PayloadService, Depends(get_service)]

# A request within the schema bounds (2 * MAX_ITEMS strings of MAX_ITEM_LENGTH characters,
# every one of them JSON-escaped) stays well below this; anything larger is refused with
# 413 instead of being read into memory and parsed first.
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


async def validation_error_handler(_request: Request, error: Exception) -> JSONResponse:
    """422 without echoing the offending input.

    FastAPI's default handler copies the input into every error, which reflects a request
    of up to MAX_BODY_BYTES back at the client, and a lone surrogate in it (legal JSON that
    the string validator rightly rejects) cannot be UTF-8 encoded, turning the 422 into a
    500. Type, location and message tell the client everything it needs.
    """
    assert isinstance(error, RequestValidationError)
    detail = [{key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()]
    return JSONResponse({"detail": detail}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; tests call this with their own settings."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url, echo=settings.echo_sql)
        # Shortcut: tables are created on start-up instead of through migrations. Fine
        # for a cache whose schema can be rebuilt, documented in the README.
        Base.metadata.create_all(engine)
        app.state.resources = Resources(
            session_factory=sessionmaker(engine, expire_on_commit=False),
            transformer=UpperCaseTransformer(settings.transformer_latency_seconds),
        )
        yield
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
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    return app


app = create_app()
