"""``cache-cli``: exercises the service the way a client would, for scripted testing.

Pydantic Settings does the argument parsing, so flags are validated and normalised (URL,
positive repeat count, existing input file) by the same machinery the service uses for its
own configuration, and the request body is checked against the API schema before it is
sent.
"""

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, TypedDict

import httpx2
from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from payload_cache.schemas import PayloadCreated, PayloadRead, PayloadRequest

# Conventional spelling for "use stdin / stdout instead of a file".
STDIO = "-"

# Pydantic Settings prints a type alias by its name as the argparse metavar, so the help
# reads "-H URL" rather than "-H AnyHttpUrl".
type URL = AnyHttpUrl
type N = PositiveInt
type FILE = str
type JSON = str


class CliSettings(BaseSettings):
    """Send a payload request to the service, read the result back and report timings.

    Exactly one of -i/--input or -j/--json must be given.
    """

    model_config = SettingsConfigDict(
        cli_prog_name="cache-cli",
        # argparse would otherwise fold -H (host) into -h (help).
        case_sensitive=True,
        # Show "-i str" rather than "-i {str,null}" for optional flags.
        cli_hide_none_type=True,
    )

    # The task lists -h for both --host and --help; -h is universally help, so host gets -H.
    host: URL = Field(
        default=AnyHttpUrl("http://localhost:8000"),
        validation_alias=AliasChoices("H", "host"),
        description="Base URL of the service.",
    )
    repeat: N = Field(
        default=1,
        validation_alias=AliasChoices("r", "repeat"),
        description="How many times to send the request; repeats should be served from cache.",
    )
    input: FILE | None = Field(
        default=None,
        validation_alias=AliasChoices("i", "input"),
        description="File holding the JSON request, or '-' for stdin (use either this or --json).",
    )
    json_body: JSON | None = Field(
        default=None,
        validation_alias=AliasChoices("j", "json"),
        description="The JSON request given inline (use either this or --input).",
    )
    output: FILE = Field(
        default=STDIO,
        validation_alias=AliasChoices("o", "output"),
        description="File to write the report to, or '-' for stdout.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Command line only: with the aliases above, the environment source would also pick
        # up ambient variables such as HOST, which is not what a test tool should do.
        return (init_settings,)

    @field_validator("input")
    @classmethod
    def _input_file_must_exist(cls, value: str | None) -> str | None:
        if value is not None and value != STDIO and not Path(value).is_file():
            raise ValueError(f"input file not found: {value}")
        return value

    @field_validator("output")
    @classmethod
    def _output_location_must_be_writable(cls, value: str) -> str:
        # Checked before any request is sent: finding out after the run that the report
        # cannot be written would waste the run and hide the result.
        if value != STDIO:
            path = Path(value)
            if path.is_dir():
                raise ValueError(f"output path is a directory: {value}")
            if not path.parent.is_dir():
                raise ValueError(f"output directory not found: {path.parent}")
        return value

    @model_validator(mode="after")
    def _exactly_one_request_source(self) -> "CliSettings":
        if (self.input is None) == (self.json_body is None):
            raise ValueError("exactly one of --input/-i or --json/-j is required")
        return self


def load_request(settings: CliSettings, stdin: IO[str]) -> PayloadRequest:
    if settings.json_body is not None:
        raw = settings.json_body
    elif settings.input == STDIO:
        raw = stdin.read()
    else:
        assert settings.input is not None  # the model validator guarantees one source
        # utf-8-sig also accepts the BOM some editors put in front of JSON files.
        raw = Path(settings.input).read_text(encoding="utf-8-sig")
    # Validating here means a malformed request fails fast with a precise message rather
    # than as a 422 from the server.
    return PayloadRequest.model_validate_json(raw)


class RunRecord(TypedDict):
    iteration: int
    id: str
    status: int
    post_ms: float
    get_ms: float


class Report(TypedDict):
    id: str
    output: str
    consistent: bool
    runs: list[RunRecord]


def run(client: httpx2.Client, request: PayloadRequest, repeat: int) -> Report:
    """POST the request ``repeat`` times, read each payload back and report what happened.

    The report records the status of every POST (201 the first time, 200 once the payload
    is known) and the time each call took, which is how the caching is observed from the
    outside.
    """
    # One throwaway call so that iteration 1 measures the service, not the TCP handshake.
    # Its status is irrelevant; a connection failure propagates like any other.
    client.get("/health")
    runs: list[RunRecord] = []
    outputs: list[str] = []
    for iteration in range(1, repeat + 1):
        started = time.perf_counter()
        created = client.post("/payload", json=request.model_dump())
        created.raise_for_status()
        post_ms = (time.perf_counter() - started) * 1000
        # Parsed through the API schemas: a 2xx with some other body (a proxy or the wrong
        # server answering) is then a reported error rather than a crash.
        payload_id = PayloadCreated.model_validate_json(created.content).id

        started = time.perf_counter()
        read = client.get(f"/payload/{payload_id}")
        read.raise_for_status()
        get_ms = (time.perf_counter() - started) * 1000

        outputs.append(PayloadRead.model_validate_json(read.content).output)
        runs.append(
            {
                "iteration": iteration,
                "id": payload_id,
                "status": created.status_code,
                "post_ms": round(post_ms, 2),
                "get_ms": round(get_ms, 2),
            }
        )
    return {
        "id": runs[0]["id"],
        "output": outputs[0],
        # A test tool should notice if the service ever disagrees with itself; the per-run
        # ids above show where.
        "consistent": len({item["id"] for item in runs}) == 1 and len(set(outputs)) == 1,
        "runs": runs,
    }


def write_report(report: Report, destination: str, stdout: IO[str]) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if destination == STDIO:
        stdout.write(text)
    else:
        Path(destination).write_text(text, encoding="utf-8")


def make_client(base_url: str) -> httpx2.Client:
    # Generous timeout: a first request over a large input pays for every transformer call.
    return httpx2.Client(base_url=base_url, timeout=30.0)


def _flag_labels() -> dict[str, str]:
    """``{"r": "-r/--repeat", "repeat": "-r/--repeat", ...}`` from the field aliases.

    Pydantic Settings reports an argument error under the field's first alias whichever
    spelling was typed, so the message names both, as argparse's own errors do.
    """
    labels: dict[str, str] = {}
    for field in CliSettings.model_fields.values():
        if isinstance(field.validation_alias, AliasChoices):
            names = [str(choice) for choice in field.validation_alias.choices]
            label = "/".join(("-" if len(name) == 1 else "--") + name for name in names)
            labels.update(dict.fromkeys(names, label))
    return labels


def _describe(error: ValidationError, *, as_flags: bool) -> str:
    """One line per problem, located by flag (arguments) or by JSON path (request body)."""
    labels = _flag_labels() if as_flags else {}
    lines = []
    for item in error.errors():
        parts = [str(part) for part in item["loc"]]
        where = labels.get(parts[0], parts[0]) if as_flags and parts else ".".join(parts)
        lines.append(f"  {where}: {item['msg']}" if where else f"  {item['msg']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit code (0 ok, 1 service problem, 2 bad invocation)."""
    try:
        settings = CliSettings(_cli_parse_args=list(argv) if argv is not None else sys.argv[1:])
    except ValidationError as error:
        print(f"cache-cli: invalid arguments\n{_describe(error, as_flags=True)}", file=sys.stderr)
        return 2
    try:
        request = load_request(settings, sys.stdin)
    except ValidationError as error:
        print(f"cache-cli: invalid request\n{_describe(error, as_flags=False)}", file=sys.stderr)
        return 2
    except UnicodeDecodeError:
        print(f"cache-cli: invalid request\n  {settings.input}: not UTF-8 encoded", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"cache-cli: cannot read {settings.input}: {error.strerror}", file=sys.stderr)
        return 2

    try:
        with make_client(str(settings.host)) as client:
            report = run(client, request, settings.repeat)
    except httpx2.HTTPStatusError as error:
        print(
            f"cache-cli: {error.request.method} {error.request.url} failed with "
            f"{error.response.status_code}: {error.response.text}",
            file=sys.stderr,
        )
        return 1
    except httpx2.HTTPError as error:
        print(f"cache-cli: {error}", file=sys.stderr)
        return 1
    except ValidationError as error:
        print(
            f"cache-cli: unexpected response from the service\n{_describe(error, as_flags=False)}",
            file=sys.stderr,
        )
        return 1

    try:
        write_report(report, settings.output, sys.stdout)
    except OSError as error:
        print(f"cache-cli: cannot write {settings.output}: {error.strerror}", file=sys.stderr)
        return 2
    if not report["consistent"]:
        print(
            "cache-cli: the service disagreed with itself across repeats (see the ids per run)",
            file=sys.stderr,
        )
        return 1
    return 0
