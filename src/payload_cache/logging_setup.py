"""Structured logging: one JSON object per line, each carrying the id of the request it belongs to.

Log lines are read by machines (a shipper, a search index) far more often than by people, so
the format is JSON throughout, uvicorn's own lines included. Every record emitted while a
request is being handled carries that request's id, so one search ties a slow query or a
traceback to the access-log line of the request that caused it.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Set by RequestContextMiddleware for the duration of a request and read by the formatter, so
# any log call made while handling the request is tagged without being told the id.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has. Anything else on a record arrived through ``extra=`` and is
# emitted as a field of its own.
_STANDARD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRIBUTES:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # default=str: a value that is not JSON (a path, a datetime) still gets logged.
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    """Send every logger, uvicorn's included, through one JSON handler on stdout.

    Idempotent, because ``create_app`` runs once per application and the tests build many
    applications in one process: a second call changes the level, not the handler count.
    """
    root = logging.getLogger()
    if not any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs text handlers on its loggers before it imports the application. Hand
    # those loggers to the root handler so start-up and error lines are JSON like the rest,
    # and retire its access log: the middleware writes one line per request that also
    # carries the request id, the route template and the duration.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
