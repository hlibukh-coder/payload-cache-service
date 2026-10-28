"""Caching layer in front of the transformer.

``TransformCache`` answers each string from the database when it can and calls the
transformer only for strings nobody has asked for before, using one query per batch
rather than one per string.
"""

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from payload_cache.db import insert_or_ignore
from payload_cache.hashing import sha256_hex
from payload_cache.models import TransformedString
from payload_cache.transformer import Transformer

# SQLite builds used to cap a statement at 999 bound parameters; chunking the lookups keeps
# every backend comfortably below any such limit.
LOOKUP_CHUNK_SIZE = 500


class TransformCache:
    def __init__(self, session: Session, transformer: Transformer) -> None:
        self._session = session
        self._transformer = transformer

    def transform_many(self, values: Iterable[str]) -> dict[str, str]:
        """Return ``{original: transformed}`` for every distinct value, filling misses."""
        # dict.fromkeys de-duplicates while preserving first-seen order, so a string that
        # appears several times in one request costs at most one transformer call.
        distinct = list(dict.fromkeys(values))
        results = self._lookup(distinct)
        misses = [value for value in distinct if value not in results]
        if misses:
            results.update(self._fill(misses))
        return results

    def _lookup(self, values: Sequence[str]) -> dict[str, str]:
        key_to_value = {sha256_hex(value): value for value in values}
        keys = list(key_to_value)
        found: dict[str, str] = {}
        for start in range(0, len(keys), LOOKUP_CHUNK_SIZE):
            chunk = keys[start : start + LOOKUP_CHUNK_SIZE]
            rows = self._session.execute(
                select(TransformedString.key, TransformedString.transformed).where(
                    TransformedString.key.in_(chunk)
                )
            )
            for key, transformed in rows:
                found[key_to_value[key]] = transformed
        return found

    def _fill(self, misses: Sequence[str]) -> dict[str, str]:
        transformed = {value: self._transformer.transform(value) for value in misses}
        rows = [
            {"key": sha256_hex(value), "original": value, "transformed": result}
            for value, result in transformed.items()
        ]
        # A concurrent request may have stored some of these strings since our lookup. The
        # transformer is deterministic, so keeping the existing rows loses nothing.
        self._session.execute(insert_or_ignore(self._session, TransformedString), rows)
        return transformed
