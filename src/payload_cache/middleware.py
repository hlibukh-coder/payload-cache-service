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

logger = logging.getLogger("payload_cache.http")

REQUEST_ID_HEADER = "X-Request-ID"
# A client-supplied id is kept, so that one id can follow a request across services, only
# when it plainly is an identifier: bounded, with no whitespace or control characters that
# could forge extra log lines. Anything else is replaced rather than rejected; the id exists
# for operators, not for the client.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Probe traffic arrives every few seconds and would drown the access log; it is logged at
# DEBUG so the log stays about real requests but can still be switched on.
QUIET_ROUTES = frozenset({"/health"})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _REQUEST_ID_PATTERN.match(incoming) else uuid4().hex
        token = request_id_var.set(request_id)
        # What the client ends up with when the application raises and the outer error
        # handler answers: no response start passes through here in that case.
        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # The route template ("/payload/{payload_id}") rather than the concrete path: it
            # names the handler, and it stays low-cardinality when it becomes a metric label.
            route = getattr(scope.get("route"), "path", None)
            logger.log(
                logging.DEBUG if route in QUIET_ROUTES else logging.INFO,
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
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)
