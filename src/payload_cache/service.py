"""Payload generation: resolves identifiers, drives the cache and assembles the output."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from payload_cache.cache import TransformCache
from payload_cache.db import insert_or_ignore
from payload_cache.hashing import canonical_json, sha256_hex
from payload_cache.metrics import PAYLOADS
from payload_cache.models import Payload
from payload_cache.schemas import PayloadRequest
from payload_cache.transformer import Transformer


@dataclass(frozen=True)
class CreateResult:
    payload_id: str
    # False when an identical request had already been generated and its id was reused.
    created: bool


def payload_id_for(request: PayloadRequest) -> str:
    """Deterministic identifier: equal lists in equal order always yield the same id."""
    return sha256_hex(canonical_json(request.model_dump()))


def interleave(first: Sequence[str], second: Sequence[str]) -> str:
    """``"a1, b1, a2, b2, ..."``: the output format the task mandates."""
    return ", ".join(item for pair in zip(first, second, strict=True) for item in pair)


class PayloadService:
    def __init__(self, session: Session, transformer: Transformer) -> None:
        self._session = session
        self._cache = TransformCache(session, transformer)

    def create(self, request: PayloadRequest) -> CreateResult:
        payload_id = payload_id_for(request)
        # Seen this exact request before: reuse the id without touching the transformer.
        # Only the key is read; the stored output (megabytes at the schema bounds) is not
        # needed to answer.
        if self._session.scalar(select(Payload.id).where(Payload.id == payload_id)) is not None:
            PAYLOADS.labels("reused").inc()
            return CreateResult(payload_id, created=False)

        transformed = self._cache.transform_many([*request.list_1, *request.list_2])
        output = interleave(
            [transformed[value] for value in request.list_1],
            [transformed[value] for value in request.list_2],
        )
        # RETURNING yields nothing when a concurrent identical request committed first; the
        # stored output is the same either way, so only the response status differs.
        inserted_id = self._session.execute(
            insert_or_ignore(self._session, Payload)
            .values(id=payload_id, output=output)
            .returning(Payload.id)
        ).scalar_one_or_none()
        self._session.commit()
        created = inserted_id is not None
        PAYLOADS.labels("created" if created else "reused").inc()
        return CreateResult(payload_id, created=created)

    def get_output(self, payload_id: str) -> str | None:
        payload = self._session.get(Payload, payload_id)
        return None if payload is None else payload.output
