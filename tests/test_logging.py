import json
import logging
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from payload_cache.logging_setup import JsonFormatter, configure_logging, request_id_var
from payload_cache.middleware import REQUEST_ID_HEADER
from tests.conftest import SAMPLE_REQUEST

HEX_32 = 32


def test_every_response_carries_a_generated_request_id(client: TestClient) -> None:
    response = client.get("/health")
    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == HEX_32 and int(request_id, 16) >= 0


def test_a_well_formed_client_request_id_is_kept(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-1.2:abc"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-1.2:abc"


@pytest.mark.parametrize("bad", ["x" * 129, "has space", "tab\there", ""])
def test_a_malformed_client_request_id_is_replaced(client: TestClient, bad: str) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: bad})
    assert response.headers[REQUEST_ID_HEADER] != bad
    assert len(response.headers[REQUEST_ID_HEADER]) == HEX_32


def test_access_log_line_names_route_status_duration_and_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="payload_cache.http"):
        response = client.post("/payload", json=SAMPLE_REQUEST)
    [record] = [r for r in caplog.records if r.name == "payload_cache.http"]
    fields = vars(record)
    assert record.levelno == logging.INFO
    assert record.getMessage() == "POST /payload -> 201"
    assert fields["method"] == "POST"
    assert fields["route"] == "/payload"
    assert fields["status"] == 201
    assert fields["duration_ms"] >= 0
    assert fields["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_access_log_uses_the_route_template_not_the_concrete_path(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="payload_cache.http"):
        client.get(f"/payload/{'0' * 64}")
    [record] = [r for r in caplog.records if r.name == "payload_cache.http"]
    fields = vars(record)
    assert fields["route"] == "/payload/{payload_id}"
    assert fields["status"] == 404


def test_unrouted_requests_are_logged_without_a_route(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="payload_cache.http"):
        client.get("/nowhere")
    [record] = [r for r in caplog.records if r.name == "payload_cache.http"]
    fields = vars(record)
    assert fields["route"] is None
    assert fields["status"] == 404


@pytest.mark.parametrize("path", ["/health", "/metrics"])
def test_probes_and_scrapes_are_logged_at_debug(
    client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger="payload_cache.http"):
        client.get(path)
    [record] = [r for r in caplog.records if r.name == "payload_cache.http"]
    assert record.levelno == logging.DEBUG


def test_the_request_id_is_visible_inside_a_handler(app: FastAPI) -> None:
    # Handlers are plain ``def`` and run in a worker thread; the id must still reach them.
    def echo_request_id() -> dict[str, str | None]:
        return {"seen": request_id_var.get()}

    app.add_api_route("/echo-request-id", echo_request_id)
    with TestClient(app) as client:
        response = client.get("/echo-request-id", headers={REQUEST_ID_HEADER: "abc-123"})
    assert response.json() == {"seen": "abc-123"}


def test_the_request_id_does_not_leak_past_the_request(client: TestClient) -> None:
    client.get("/health", headers={REQUEST_ID_HEADER: "leaky"})
    assert request_id_var.get() is None


def test_json_formatter_emits_one_object_with_extras_and_the_request_id() -> None:
    record = logging.makeLogRecord(
        {
            "name": "payload_cache.test",
            "levelname": "INFO",
            "levelno": logging.INFO,
            "msg": "cache %s",
            "args": ("miss",),
            "status": 201,
            "duration_ms": 1.5,
        }
    )
    token = request_id_var.set("req-1")
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(token)
    entry = json.loads(line)
    assert entry["message"] == "cache miss"
    assert entry["request_id"] == "req-1"
    assert entry["status"] == 201 and entry["duration_ms"] == 1.5
    assert entry["level"] == "INFO" and entry["logger"] == "payload_cache.test"
    assert entry["time"].endswith("+00:00")
    assert "\n" not in line


def test_json_formatter_includes_the_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.makeLogRecord({"msg": "failed", "exc_info": sys.exc_info()})
    entry = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in entry["exception"]


def test_configure_logging_is_idempotent_and_sets_the_level() -> None:
    configure_logging("INFO")
    configure_logging("WARNING")
    root = logging.getLogger()
    json_handlers = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
    assert len(json_handlers) == 1
    assert root.level == logging.WARNING
    configure_logging("INFO")  # leave the process as the other tests expect it
