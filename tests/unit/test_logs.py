import json
import logging
import sys

from payload_cache.observability.logs import JsonFormatter, configure_logging, request_id_var

HEX_32 = 32


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


def test_configure_logging_lets_log_level_reach_uvicorn_and_drops_colour_codes() -> None:
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)  # what uvicorn's own config does
    configure_logging("WARNING")
    try:
        assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.WARNING
    finally:
        configure_logging("INFO")
    record = logging.makeLogRecord({"msg": "Started", "color_message": "\x1b[32mStarted\x1b[0m"})
    assert "color_message" not in json.loads(JsonFormatter().format(record))


def test_echo_sql_logs_statements_through_the_json_handler_only() -> None:
    configure_logging("INFO", echo_sql=True)
    try:
        engine_logger = logging.getLogger("sqlalchemy.engine")
        assert engine_logger.level == logging.INFO
        assert not engine_logger.handlers  # no plain-text handler of SQLAlchemy's own
    finally:
        configure_logging("INFO")
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
