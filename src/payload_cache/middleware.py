"""Request context: an id on every request, response and log line, plus one access-log line.

Written as raw ASGI rather than ``BaseHTTPMiddleware``: no response buffering, no extra task
per request, and the context variable set here is seen by the handler even though FastAPI
runs the synchronous handlers in a worker thread.
"""

import logging
import re
import time
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from payload_cache.logging_setup import request_id_var
from payload_cache.metrics import HTTP_REQUEST_DURATION, UNMATCHED_ROUTE

logger = logging.getLogger("payload_cache.http")

REQUEST_ID_HEADER = "X-Request-ID"
# A client-supplied id is kept, so that one id can follow a request across services, only
# when it plainly is an identifier: bounded, with no whitespace or control characters that
# could forge extra log lines. Anything else is replaced rather than rejected; the id exists
# for operators, not for the client.
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# Probes and scrapes arrive every few seconds and would drown the access log; when they
# succeed they are logged at DEBUG, so the log stays about real requests. A failing probe is
# logged like any failure: a 503 from /health is how a database outage first shows.
QUIET_ROUTES = frozenset({"/health", "/metrics"})
# The method is client-chosen and becomes a metric label; anything outside the standard set
# shares one value so it cannot multiply the series.
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
OTHER_METHOD = "other"

INTERNAL_ERROR_BODY = b'{"detail":"Internal Server Error"}'


def _level(route: str | None, status_code: int) -> int:
    if status_code >= 500:
        return logging.ERROR
    if route in QUIET_ROUTES and status_code < 400:
        return logging.DEBUG
    return logging.INFO


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = (
            incoming if incoming and _REQUEST_ID_PATTERN.fullmatch(incoming) else uuid4().hex
        )
        token = request_id_var.set(request_id)
        status_code = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # Logged here, while the request id is still set, so the traceback carries it;
            # Starlette's outer error handler would log it after this middleware has returned.
            logger.exception("unhandled error", extra={"request_id": request_id})
            if response_started:
                raise  # too late to answer; let the server close the connection
            # Answered here rather than re-raised, so the 500 carries the request id the
            # traceback was logged under, and the traceback is logged once, not twice.
            await send_with_request_id(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(INTERNAL_ERROR_BODY)).encode()),
                    ],
                }
            )
            await send_with_request_id({"type": "http.response.body", "body": INTERNAL_ERROR_BODY})
        finally:
            # The route template ("/payload/{payload_id}") rather than the concrete path: it
            # names the handler, and it stays low-cardinality when it becomes a metric label.
            route = getattr(scope.get("route"), "path", None)
            elapsed = time.perf_counter() - started
            method = scope["method"] if scope["method"] in KNOWN_METHODS else OTHER_METHOD
            HTTP_REQUEST_DURATION.labels(
                method, route or UNMATCHED_ROUTE, str(status_code)
            ).observe(elapsed)
            logger.log(
                _level(route, status_code),
                "%s %s -> %d",
                scope["method"],
                scope["path"],
                status_code,
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "path": scope["path"],
                    "route": route,
                    "status": status_code,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            request_id_var.reset(token)
