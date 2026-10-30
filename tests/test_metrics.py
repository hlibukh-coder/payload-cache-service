from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
from prometheus_client.parser import text_string_to_metric_families

from tests.conftest import SAMPLE_REQUEST

PAYLOAD_ID_PATTERN_ROUTE = "/payload/{payload_id}"


def counter(name: str, **labels: str) -> float:
    # The registry is process-wide and shared by every test, so tests compare deltas.
    value = REGISTRY.get_sample_value(name, labels)
    assert value is not None, f"{name}{labels} is not registered"
    return value


def test_metrics_endpoint_serves_the_prometheus_exposition_format(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    families = {f.name: f.type for f in text_string_to_metric_families(response.text)}
    assert families["payload_cache_transform_strings"] == "counter"
    assert families["payload_cache_payloads"] == "counter"
    assert families["payload_cache_http_request_duration_seconds"] == "histogram"


def test_metrics_endpoint_is_not_part_of_the_api_schema(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_cache_hits_and_misses_are_counted_per_distinct_string(client: TestClient) -> None:
    hits = counter("payload_cache_transform_strings_total", result="hit")
    misses = counter("payload_cache_transform_strings_total", result="miss")

    client.post("/payload", json=SAMPLE_REQUEST)  # six new strings
    # Two already cached, one new, and a duplicate inside the request counted once.
    client.post(
        "/payload",
        json={"list_1": ["first string", "brand new"], "list_2": ["other string", "brand new"]},
    )

    assert counter("payload_cache_transform_strings_total", result="hit") - hits == 2
    assert counter("payload_cache_transform_strings_total", result="miss") - misses == 6 + 1


def test_reposting_an_identical_request_touches_no_string_counter(client: TestClient) -> None:
    client.post("/payload", json=SAMPLE_REQUEST)
    hits = counter("payload_cache_transform_strings_total", result="hit")
    misses = counter("payload_cache_transform_strings_total", result="miss")

    client.post("/payload", json=SAMPLE_REQUEST)

    assert counter("payload_cache_transform_strings_total", result="hit") == hits
    assert counter("payload_cache_transform_strings_total", result="miss") == misses


def test_payload_outcomes_are_counted(client: TestClient) -> None:
    created = counter("payload_cache_payloads_total", result="created")
    reused = counter("payload_cache_payloads_total", result="reused")

    client.post("/payload", json=SAMPLE_REQUEST)
    client.post("/payload", json=SAMPLE_REQUEST)
    client.post("/payload", json=SAMPLE_REQUEST)

    assert counter("payload_cache_payloads_total", result="created") - created == 1
    assert counter("payload_cache_payloads_total", result="reused") - reused == 2


def test_request_durations_are_labelled_by_route_template_and_status(client: TestClient) -> None:
    name = "payload_cache_http_request_duration_seconds_count"
    labels = {"method": "GET", "route": PAYLOAD_ID_PATTERN_ROUTE, "status": "404"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0

    client.get(f"/payload/{'0' * 64}")

    assert REGISTRY.get_sample_value(name, labels) == before + 1


def test_unrouted_requests_share_one_label_instead_of_leaking_paths(client: TestClient) -> None:
    name = "payload_cache_http_request_duration_seconds_count"
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0

    client.get("/no/such/path-1")
    client.get("/no/such/path-2")

    assert REGISTRY.get_sample_value(name, labels) == before + 2
    assert "/no/such/path-1" not in client.get("/metrics").text


def test_non_standard_methods_share_one_label(client: TestClient) -> None:
    name = "payload_cache_http_request_duration_seconds_count"
    labels = {"method": "other", "route": "/payload", "status": "405"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    client.request("BREW", "/payload")
    assert REGISTRY.get_sample_value(name, labels) == before + 1
    assert 'method="BREW"' not in client.get("/metrics").text
