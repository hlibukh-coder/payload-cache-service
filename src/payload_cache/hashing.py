"""Content hashing helpers shared by both caches."""

import hashlib
import json
from typing import Any


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(data: Any) -> str:
    """Serialise ``data`` so that equal values always produce byte-identical JSON.

    Sorted keys and compact separators remove formatting freedom; ``ensure_ascii=False``
    keeps non-ASCII text as-is instead of depending on escape choices.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
