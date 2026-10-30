import io
import json
import os
import threading
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from payload_cache import cli
from payload_cache.api.schemas import PayloadRequest
from payload_cache.cli import CliSettings, load_request, main
from tests.conftest import SAMPLE_OUTPUT, SAMPLE_REQUEST

SAMPLE_JSON = json.dumps(SAMPLE_REQUEST)


def parse(*argv: str) -> CliSettings:
    return CliSettings(_cli_parse_args=list(argv))


# --- argument parsing -------------------------------------------------------------------


def test_short_and_long_flags_are_equivalent() -> None:
    short = parse("-H", "http://api:9000", "-r", "3", "-j", "{}", "-o", "out.json")
    long = parse(
        "--host", "http://api:9000", "--repeat", "3", "--json", "{}", "--output", "out.json"
    )

    assert short == long
    assert str(short.host) == "http://api:9000/"
    assert short.repeat == 3
    assert short.json_body == "{}"
    assert short.output == "out.json"


def test_defaults() -> None:
    settings = parse("-j", "{}")

    assert str(settings.host) == "http://localhost:8000/"
    assert settings.repeat == 1
    assert settings.output == "-"


def test_lower_case_h_is_help_not_host(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse("-h")

    assert exit_info.value.code == 0
    # argparse lays the option out as "-H URL, --host URL" before Python 3.13 and as
    # "-H, --host URL" from 3.13 on; assert on the parts both layouts share.
    out = capsys.readouterr().out
    assert "-H" in out
    assert "--host URL" in out


def test_help_reads_like_the_task_statement(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse("--help")

    out = capsys.readouterr().out
    assert "[-H URL] [-r N] [-i FILE] [-j JSON] [-o FILE]" in out
    assert "Exactly one of -i/--input or -j/--json" in out


def test_help_has_no_null_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse("--help")

    out = capsys.readouterr().out
    assert "null" not in out
    assert out.count("(default: not set)") == 2  # --input and --json


def test_a_file_named_null_is_an_ordinary_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("null").write_text(SAMPLE_JSON)

    assert parse("-i", "null").input == "null"


def test_repeat_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        parse("-j", "{}", "-r", "0")


def test_host_must_be_an_http_url() -> None:
    with pytest.raises(ValidationError):
        parse("-j", "{}", "-H", "not a url")


def test_input_and_json_are_mutually_exclusive(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text("{}")

    with pytest.raises(ValidationError, match="exactly one of"):
        parse("-i", str(request_file), "-j", "{}")


def test_one_request_source_is_required() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        parse()


def test_missing_input_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not found"):
        parse("-i", str(tmp_path / "missing.json"))


def test_empty_input_path_is_rejected() -> None:
    with pytest.raises(ValidationError, match="input file name is empty"):
        parse("-i", "")


def test_a_file_named_with_spaces_is_an_ordinary_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only "" is the current directory; " " is a legal file name.
    monkeypatch.chdir(tmp_path)
    Path(" ").write_text(SAMPLE_JSON)

    assert parse("-i", " ").input == " "


def test_directory_as_input_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="input path is a directory"):
        parse("-i", str(tmp_path))


def test_output_location_is_checked_up_front(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="directory not found"):
        parse("-j", "{}", "-o", str(tmp_path / "missing" / "report.json"))
    with pytest.raises(ValidationError, match="is a directory"):
        parse("-j", "{}", "-o", str(tmp_path))


def test_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Spelled exactly like the aliases, which is what the (disabled) env source would read.
    monkeypatch.setenv("host", "http://from-env:1")
    monkeypatch.setenv("repeat", "7")

    settings = parse("-j", "{}")

    assert str(settings.host) == "http://localhost:8000/"
    assert settings.repeat == 1


# --- request loading --------------------------------------------------------------------


def test_request_from_inline_json() -> None:
    request = load_request(parse("-j", SAMPLE_JSON), io.StringIO())

    assert request == PayloadRequest.model_validate(SAMPLE_REQUEST)


def test_request_from_file(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(SAMPLE_JSON)

    request = load_request(parse("-i", str(request_file)), io.StringIO())

    assert request.list_1 == SAMPLE_REQUEST["list_1"]


def test_request_file_may_start_with_a_bom(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(SAMPLE_JSON, encoding="utf-8-sig")

    request = load_request(parse("-i", str(request_file)), io.StringIO())

    assert request.list_1 == SAMPLE_REQUEST["list_1"]


def test_request_from_stdin() -> None:
    request = load_request(parse("-i", "-"), io.StringIO(SAMPLE_JSON))

    assert request.list_2 == SAMPLE_REQUEST["list_2"]


def test_request_from_stdin_may_start_with_a_bom() -> None:
    request = load_request(parse("-i", "-"), io.StringIO("\ufeff" + SAMPLE_JSON))

    assert request.list_1 == SAMPLE_REQUEST["list_1"]


def test_request_from_a_pipe(tmp_path: Path) -> None:
    # `cache-cli -i <(generate-request)` hands over a path that exists but is not a file.
    fifo = tmp_path / "request.fifo"
    os.mkfifo(fifo)
    writer = threading.Thread(target=fifo.write_text, args=(SAMPLE_JSON,))
    writer.start()

    request = load_request(parse("-i", str(fifo)), io.StringIO())

    writer.join()
    assert request.list_1 == SAMPLE_REQUEST["list_1"]


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValidationError):
        load_request(parse("-j", "{not json"), io.StringIO())


def test_request_is_validated_against_the_api_schema() -> None:
    with pytest.raises(ValidationError, match="same length"):
        load_request(parse("-j", '{"list_1": ["a"], "list_2": ["b", "c"]}'), io.StringIO())


def test_main_rejects_bad_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-j", "{}", "--repeat", "0"]) == 2

    err = capsys.readouterr().err
    assert "invalid arguments" in err
    assert "-r/--repeat: Input should be greater than 0" in err


def test_main_rejects_bad_request(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-j", '{"list_1": ["a"], "list_2": ["b", "c"]}']) == 2
    assert "same length" in capsys.readouterr().err


def test_main_reports_a_directory_given_as_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-i", str(tmp_path)]) == 2

    err = capsys.readouterr().err
    assert "invalid arguments" in err
    assert "-i/--input" in err
    assert "is a directory" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files regardless of permissions")
def test_main_reports_an_unreadable_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(SAMPLE_JSON)
    request_file.chmod(0)

    assert main(["-i", str(request_file)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_main_rejects_non_utf8_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_bytes(b"\xff\xfe{}")

    assert main(["-i", str(request_file)]) == 2
    assert "not UTF-8" in capsys.readouterr().err


def test_main_reports_unexpected_response_bodies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The wrong server (an SPA dev server, a proxy's error page) answering 200 with HTML.
    def html(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>Welcome</html>")

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda base_url: httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(html)),
    )

    assert main(["-j", SAMPLE_JSON]) == 1
    assert "unexpected response" in capsys.readouterr().err


def test_main_fails_when_the_service_disagrees_with_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A broken service that hands out a new id for every POST of the same request: exactly
    # the cache failure --repeat is there to catch.
    issued: list[str] = []

    def unstable(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok"})
        if request.method == "POST":
            issued.append(f"{len(issued):064x}")
            return httpx2.Response(201, json={"id": issued[-1], "message": "Payload created."})
        return httpx2.Response(200, json={"output": SAMPLE_OUTPUT})

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda base_url: httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(unstable)),
    )

    assert main(["-j", SAMPLE_JSON, "-r", "2"]) == 1

    captured = capsys.readouterr()
    assert "disagreed with itself" in captured.err
    # The report is still written, so the ids per run show where the service went wrong.
    report = json.loads(captured.out)
    assert report["consistent"] is False
    assert [item["id"] for item in report["runs"]] == issued


def test_main_reports_connection_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        # The URL proves --host reached the client and was joined with the endpoint path.
        assert str(request.url).startswith("http://nowhere:1/")
        raise httpx2.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda base_url: httpx2.Client(base_url=base_url, transport=httpx2.MockTransport(refuse)),
    )

    assert main(["-j", SAMPLE_JSON, "-H", "http://nowhere:1"]) == 1
    assert "connection refused" in capsys.readouterr().err
