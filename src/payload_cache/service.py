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


def payload_id_for(request: PayloadRequest, transformer_version: str) -> str:
    """Deterministic identifier: equal lists in equal order, under one transformer version,
    always yield the same id.

    The version is part of it because the output depends on it. After a bump, the same lists
    get a new id and a freshly generated payload; the old payload stays intact under the old
    id until it is purged, rather than being overwritten with output a different transformer
    produced.
    """
    return sha256_hex(
        canonical_json(
            {"request": request.model_dump(), "transformer_version": transformer_version}
        )
    )


def interleave(first: Sequence[str], second: Sequence[str]) -> str:
    """``"a1, b1, a2, b2, ..."``: the output format the task mandates."""
    return ", ".join(item for pair in zip(first, second, strict=True) for item in pair)


class PayloadService:
    def __init__(
        self, session: Session, transformer: Transformer, transformer_version: str
    ) -> None:
        self._session = session
        self._version = transformer_version
        self._cache = TransformCache(session, transformer, transformer_version)

    def create(self, request: PayloadRequest) -> CreateResult:
        payload_id = payload_id_for(request, self._version)
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
            .values(id=payload_id, output=output, transformer_version=self._version)
            .returning(Payload.id)
        ).scalar_one_or_none()
        self._session.commit()
        created = inserted_id is not None
        PAYLOADS.labels("created" if created else "reused").inc()
        return CreateResult(payload_id, created=created)

    def get_output(self, payload_id: str) -> str | None:
        payload = self._session.get(Payload, payload_id)
        return None if payload is None else payload.output
