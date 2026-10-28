"""HTTP layer: wires settings, database and transformer into a FastAPI application."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request, Response, status
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.config import Settings
from payload_cache.db import make_engine
from payload_cache.models import DIGEST_LENGTH, Base
from payload_cache.schemas import PayloadCreated, PayloadRead, PayloadRequest
from payload_cache.service import PayloadService
from payload_cache.transformer import UpperCaseTransformer


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_service(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> PayloadService:
    return PayloadService(session, request.app.state.transformer)


ServiceDep = Annotated[PayloadService, Depends(get_service)]

router = APIRouter()

# Endpoints are plain ``def``: FastAPI runs them in a thread pool, which is the right fit for
# the synchronous SQLAlchemy session used underneath.


@router.post(
    "/payload",
    response_model=PayloadCreated,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": PayloadCreated,
            "description": "An identical payload already existed; its identifier is returned.",
        }
    },
)
def create_payload(body: PayloadRequest, service: ServiceDep, response: Response) -> PayloadCreated:
    result = service.create(body)
    response.headers["Location"] = f"/payload/{result.payload_id}"
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
    responses={status.HTTP_404_NOT_FOUND: {"description": "No payload with this identifier."}},
)
def read_payload(
    payload_id: Annotated[str, Path(pattern=rf"^[0-9a-f]{{{DIGEST_LENGTH}}}$")],
    service: ServiceDep,
) -> PayloadRead:
    output = service.get_output(payload_id)
    if output is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Payload not found.")
    return PayloadRead(output=output)


@router.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; tests call this with their own settings."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url, echo=settings.echo_sql)
        # Shortcut: tables are created on start-up instead of through migrations. Fine
        # for a cache whose schema can be rebuilt, documented in the README.
        Base.metadata.create_all(engine)
        app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
        app.state.transformer = UpperCaseTransformer(settings.transformer_latency_seconds)
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
    app.include_router(router)
    return app


app = create_app()
