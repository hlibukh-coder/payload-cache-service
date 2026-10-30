from payload_cache.core.cache import cache_key
from payload_cache.core.hashing import canonical_json, sha256_hex


def test_sha256_hex_matches_known_vector() -> None:
    assert sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_canonical_json_is_independent_of_key_order() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_preserves_list_order() -> None:
    assert canonical_json({"x": [1, 2]}) != canonical_json({"x": [2, 1]})


def test_canonical_json_keeps_unicode_intact() -> None:
    assert canonical_json({"s": "héllo"}) == '{"s":"héllo"}'


def test_cache_keys_differ_by_version_and_are_digests() -> None:
    assert cache_key("a", "1") != cache_key("a", "2")
    assert len(cache_key("a", "1")) == 64
