from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import SAMPLE_OUTPUT, SAMPLE_REQUEST


def test_create_then_read_returns_the_documented_output(client: TestClient) -> None:
    created = client.post("/payload", json=SAMPLE_REQUEST)

    assert created.status_code == 201
    payload_id = created.json()["id"]
    assert created.headers["Location"] == f"/payload/{payload_id}"

    read = client.get(f"/payload/{payload_id}")

    assert read.status_code == 200
    assert read.json() == {"output": SAMPLE_OUTPUT}


def test_reposting_the_same_request_reuses_the_id(client: TestClient, app: FastAPI) -> None:
    first = client.post("/payload", json=SAMPLE_REQUEST)
    calls_after_first = app.state.transformer.calls

    second = client.post("/payload", json=SAMPLE_REQUEST)

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert app.state.transformer.calls == calls_after_first


def test_only_new_strings_reach_the_transformer(client: TestClient, app: FastAPI) -> None:
    client.post("/payload", json=SAMPLE_REQUEST)
    overlapping = {"list_1": ["first string", "new one"], "list_2": ["last string", "new two"]}

    response = client.post("/payload", json=overlapping)

    assert response.status_code == 201
    assert app.state.transformer.calls == 6 + 2
    assert client.get(response.headers["Location"]).json() == {
        "output": "FIRST STRING, LAST STRING, NEW ONE, NEW TWO"
    }


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


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
