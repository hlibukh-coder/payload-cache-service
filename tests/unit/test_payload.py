import pytest

from payload_cache.api.schemas import PayloadRequest
from payload_cache.core.service import interleave, payload_id_for
from tests.conftest import SAMPLE_ID


def test_interleave_alternates_between_lists() -> None:
    assert interleave(["A", "C"], ["B", "D"]) == "A, B, C, D"


def test_interleave_rejects_lists_of_different_length() -> None:
    with pytest.raises(ValueError):
        interleave(["A"], ["B", "C"])


def test_payload_id_is_deterministic(sample_request: PayloadRequest) -> None:
    again = PayloadRequest.model_validate(sample_request.model_dump())

    assert payload_id_for(sample_request, "1") == payload_id_for(again, "1")
    assert len(payload_id_for(sample_request, "1")) == 64


def test_sample_request_has_the_documented_id(sample_request: PayloadRequest) -> None:
    assert payload_id_for(sample_request, "1") == SAMPLE_ID


def test_payload_id_depends_on_order_and_on_which_list_holds_what() -> None:
    base = PayloadRequest(list_1=["a", "b"], list_2=["c", "d"])
    reordered = PayloadRequest(list_1=["b", "a"], list_2=["d", "c"])
    swapped = PayloadRequest(list_1=["c", "d"], list_2=["a", "b"])

    assert (
        len(
            {
                payload_id_for(base, "1"),
                payload_id_for(reordered, "1"),
                payload_id_for(swapped, "1"),
            }
        )
        == 3
    )


def test_the_transformer_version_is_part_of_the_payload_id(sample_request: PayloadRequest) -> None:
    assert payload_id_for(sample_request, "1") != payload_id_for(sample_request, "2")
