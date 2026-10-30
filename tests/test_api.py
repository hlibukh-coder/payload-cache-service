from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from payload_cache.db import make_engine
from payload_cache.main import MAX_BODY_BYTES, MAX_LOCATION_PART_LENGTH
from payload_cache.schemas import MAX_ITEM_LENGTH, MAX_ITEMS
from payload_cache.transformer import UpperCaseTransformer
from tests.conftest import SAMPLE_ID, SAMPLE_OUTPUT, SAMPLE_REQUEST, FailingTransformer

JSON = {"content-type": "application/json"}


def test_create_then_read_returns_the_documented_output(client: TestClient) -> None:
    created = client.post("/payload", json=SAMPLE_REQUEST)

    assert created.status_code == 201
    assert created.json() == {"id": SAMPLE_ID, "message": "Payload created."}
    assert created.headers["Location"] == f"/payload/{SAMPLE_ID}"

    read = client.get(f"/payload/{SAMPLE_ID}")

    assert read.status_code == 200
    assert read.json() == {"output": SAMPLE_OUTPUT}


def test_reposting_the_same_request_reuses_the_id(client: TestClient, app: FastAPI) -> None:
    first = client.post("/payload", json=SAMPLE_REQUEST)
    calls_after_first = app.state.resources.transformer.calls

    second = client.post("/payload", json=SAMPLE_REQUEST)

    assert second.status_code == 200
    assert second.json() == {
        "id": first.json()["id"],
        "message": "Payload already exists; returning the existing identifier.",
    }
    assert second.headers["Location"] == first.headers["Location"]
    assert app.state.resources.transformer.calls == calls_after_first


def test_location_follows_the_root_path_of_a_proxy(app: FastAPI) -> None:
    with TestClient(app, root_path="/api") as client:
        response = client.post("/payload", json=SAMPLE_REQUEST)

    assert response.headers["Location"] == f"/api/payload/{response.json()['id']}"


def test_openapi_documents_the_location_header_and_the_404_body(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    post_responses = paths["/payload"]["post"]["responses"]
    assert "Location" in post_responses["201"]["headers"]
    assert "Location" in post_responses["200"]["headers"]
    get_404 = paths["/payload/{payload_id}"]["get"]["responses"]["404"]
    assert get_404["content"]["application/json"]["example"] == {"detail": "Payload not found."}


def test_only_new_strings_reach_the_transformer(client: TestClient, app: FastAPI) -> None:
    client.post("/payload", json=SAMPLE_REQUEST)
    overlapping = {"list_1": ["first string", "new one"], "list_2": ["last string", "new two"]}

    response = client.post("/payload", json=overlapping)

    assert response.status_code == 201
    assert app.state.resources.transformer.calls == 6 + 2
    assert client.get(response.headers["Location"]).json() == {
        "output": "FIRST STRING, LAST STRING, NEW ONE, NEW TWO"
    }


def test_a_transformer_failure_is_a_500_and_leaves_no_payload_behind(app: FastAPI) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.resources = replace(
            app.state.resources, transformer=FailingTransformer(fail_on_call=3)
        )

        failed = client.post("/payload", json=SAMPLE_REQUEST)

        assert failed.status_code == 500
        assert failed.json() == {"detail": "Internal Server Error"}
        assert "X-Request-ID" in failed.headers
        assert client.get(f"/payload/{SAMPLE_ID}").status_code == 404

        # The service recovers once the transformer does, with the same id as always.
        app.state.resources = replace(app.state.resources, transformer=UpperCaseTransformer())

        retried = client.post("/payload", json=SAMPLE_REQUEST)

        assert retried.status_code == 201
        assert retried.json()["id"] == SAMPLE_ID


def test_unknown_payload_is_404(client: TestClient) -> None:
    response = client.get(f"/payload/{'0' * 64}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Payload not found."}


def test_malformed_payload_id_is_rejected(client: TestClient) -> None:
    assert client.get("/payload/not-a-digest").status_code == 422


def test_lists_of_different_length_are_rejected(client: TestClient) -> None:
    response = client.post("/payload", json={"list_1": ["a"], "list_2": ["b", "c"]})

    assert response.status_code == 422
    assert "same length" in response.text


def test_empty_lists_are_rejected(client: TestClient) -> None:
    assert client.post("/payload", json={"list_1": [], "list_2": []}).status_code == 422


def test_non_string_items_are_rejected(client: TestClient) -> None:
    assert client.post("/payload", json={"list_1": [1], "list_2": ["b"]}).status_code == 422


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    assert client.post("/payload", json={**SAMPLE_REQUEST, "list_3": []}).status_code == 422


def test_too_many_items_are_rejected(client: TestClient) -> None:
    too_many = ["x"] * (MAX_ITEMS + 1)

    assert client.post("/payload", json={"list_1": too_many, "list_2": too_many}).status_code == 422


def test_too_long_strings_are_rejected(client: TestClient) -> None:
    too_long = "x" * (MAX_ITEM_LENGTH + 1)

    assert client.post("/payload", json={"list_1": [too_long], "list_2": ["y"]}).status_code == 422


def test_nul_characters_are_rejected(client: TestClient) -> None:
    # PostgreSQL cannot store NUL in text; refuse it up front on every backend.
    body = b'{"list_1": ["a\\u0000b"], "list_2": ["c"]}'

    response = client.post("/payload", content=body, headers=JSON)

    assert response.status_code == 422
    assert "NUL" in response.text


def test_lone_surrogates_are_rejected(client: TestClient) -> None:
    # Legal JSON that Python's parser accepts but that is not valid text: it can neither be
    # hashed nor echoed back in an error response.
    body = b'{"list_1": ["\\ud800"], "list_2": ["c"]}'

    response = client.post("/payload", content=body, headers=JSON)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "list_1", 0]


def test_validation_errors_do_not_echo_the_input(client: TestClient) -> None:
    response = client.post("/payload", json={"list_1": ["secret"], "list_2": []})

    (error,) = response.json()["detail"]
    assert set(error) == {"type", "loc", "msg"}
    assert "secret" not in response.text


def test_unknown_field_names_are_bounded_in_errors(client: TestClient) -> None:
    # The field name is the one place where client text reaches an error location.
    long_name = "k" * 5_000

    response = client.post("/payload", json={**SAMPLE_REQUEST, long_name: 1})

    (error,) = response.json()["detail"]
    assert error["type"] == "extra_forbidden"
    assert error["loc"] == ["body", "k" * MAX_LOCATION_PART_LENGTH + "..."]
    assert len(response.content) < 1_000


def test_oversized_bodies_are_refused(client: TestClient) -> None:
    body = b'{"list_1": ["' + b"x" * MAX_BODY_BYTES + b'"], "list_2": ["y"]}'

    assert client.post("/payload", content=body, headers=JSON).status_code == 413


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_health_is_503_when_the_database_is_unreachable(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    # A directory is not a database file, so every connection attempt fails.
    broken = sessionmaker(make_engine(f"sqlite:///{tmp_path}"))
    app.state.resources = replace(app.state.resources, session_factory=broken)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable."}
