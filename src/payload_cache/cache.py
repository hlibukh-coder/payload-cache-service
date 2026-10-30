"""Caching layer in front of the transformer.

``TransformCache`` answers each string from the database when it can and calls the
transformer only for strings nobody has asked for before, using one query per batch
rather than one per string.
"""

import operator
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from payload_cache.db import insert_or_ignore
from payload_cache.hashing import canonical_json, sha256_hex
from payload_cache.metrics import TRANSFORM_STRINGS
from payload_cache.models import TransformedString
from payload_cache.transformer import Transformer

# SQLite builds used to cap a statement at 999 bound parameters; chunking the lookups keeps
# every backend comfortably below any such limit.
LOOKUP_CHUNK_SIZE = 500


def cache_key(value: str, transformer_version: str) -> str:
    """Key of a cached result: the input string and the transformer version that produced it.

    A different version may answer the same string differently, so its results live under
    different keys and a bump makes every old row a miss instead of a wrong answer.
    """
    return sha256_hex(canonical_json([transformer_version, value]))


class TransformCache:
    def __init__(
        self, session: Session, transformer: Transformer, transformer_version: str
    ) -> None:
        self._session = session
        self._transformer = transformer
        self._version = transformer_version

    def transform_many(self, values: Iterable[str]) -> dict[str, str]:
        """Return ``{original: transformed}`` for every distinct value, filling misses."""
        # dict.fromkeys de-duplicates while preserving first-seen order, so a string that
        # appears several times in one request costs at most one transformer call.
        distinct = list(dict.fromkeys(values))
        results = self._lookup(distinct)
        misses = [value for value in distinct if value not in results]
        TRANSFORM_STRINGS.labels("hit").inc(len(results))
        TRANSFORM_STRINGS.labels("miss").inc(len(misses))
        if misses:
            # Nothing has been written yet, so ending the read-only lookup transaction loses
            # nothing and hands the pooled connection back while we wait on the (external)
            # transformer, instead of pinning a connection, and on PostgreSQL an open
            # transaction, for the whole call. Callers must therefore have no unflushed
            # writes at this point; _fill starts a fresh, short write transaction.
            self._session.rollback()
            results.update(self._fill(misses))
        return results

    def _lookup(self, values: Sequence[str]) -> dict[str, str]:
        key_to_value = {cache_key(value, self._version): value for value in values}
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
        # All or nothing: if the transformer fails part-way, the exception propagates before
        # anything is written, the client gets a 500 and a retry pays again for the strings
        # that did succeed. Keeping them would need a write per call or a partial insert on
        # the error path. A transient failure costs one request's strings again at most, and
        # during an outage the retry fails anyway, so the simpler path was chosen.
        transformed = {value: self._transformer.transform(value) for value in misses}
        rows = [
            {
                "key": cache_key(value, self._version),
                "original": value,
                "transformed": result,
                "transformer_version": self._version,
            }
            for value, result in transformed.items()
        ]
        # A concurrent request may have stored some of these strings since our lookup. The
        # transformer is deterministic, so keeping the existing rows loses nothing. The rows
        # go in key order on purpose: PostgreSQL makes an ON CONFLICT insert wait for an
        # uncommitted row with the same key, so two requests inserting overlapping strings
        # in one global order can only queue behind each other, never deadlock.
        rows.sort(key=operator.itemgetter("key"))
        self._session.execute(insert_or_ignore(self._session, TransformedString), rows)
        return transformed
