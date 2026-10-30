"""Prometheus metrics: what the service is for (transformer calls avoided) and how it behaves.

Module-level collectors, as the client library intends: they register themselves once per
process, so every application built in this process (there are many in the tests) reports
into the same registry and the numbers below are process-wide, never per request.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

TRANSFORM_STRINGS = Counter(
    "payload_cache_transform_strings_total",
    "Distinct strings per request answered from the cache (hit) or sent to the transformer "
    "(miss). Misses are the transformer calls made; the hit ratio is what the cache buys.",
    ["result"],
)
PAYLOADS = Counter(
    "payload_cache_payloads_total",
    "POST /payload outcomes: created, or reused because an identical request had been "
    "generated before.",
    ["result"],
)
EVICTED_ROWS = Counter(
    "payload_cache_evicted_rows_total",
    "Cache rows removed by the clean-up in db/eviction.py, by table and reason.",
    ["table", "reason"],
)
HTTP_REQUEST_DURATION = Histogram(
    "payload_cache_http_request_duration_seconds",
    "Time to answer a request, by route template and status.",
    ["method", "route", "status"],
    # Cache hits answer in about a millisecond and a cold request over a slow transformer
    # takes seconds; the default buckets start too high to tell hits apart.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Label values are materialised up front so the series exist (at zero) from the first scrape;
# a rate() over a series that appears later misses its first increment.
for _result in ("hit", "miss"):
    TRANSFORM_STRINGS.labels(_result)
for _result in ("created", "reused"):
    PAYLOADS.labels(_result)

# What a request with no matching route is labelled as, instead of its raw path: paths are
# client-controlled and unbounded, and a label with unbounded values is a cardinality leak.
UNMATCHED_ROUTE = "unmatched"


def render() -> tuple[bytes, str]:
    """The exposition text and its content type, for the ``/metrics`` endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
