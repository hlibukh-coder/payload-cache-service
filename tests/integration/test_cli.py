import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from payload_cache import cli
from payload_cache.api.schemas import PayloadRequest
from payload_cache.cli import main, run
from tests.conftest import SAMPLE_OUTPUT, SAMPLE_REQUEST

SAMPLE_JSON = json.dumps(SAMPLE_REQUEST)


def test_run_reports_id_reuse_across_repeats(client: TestClient) -> None:
    request = PayloadRequest.model_validate(SAMPLE_REQUEST)

    report = run(client, request, repeat=3)

    assert report["output"] == SAMPLE_OUTPUT
    assert report["consistent"] is True
    assert [item["status"] for item in report["runs"]] == [201, 200, 200]
    assert all(item["id"] == report["id"] for item in report["runs"])
    assert all(item["post_ms"] >= 0 and item["get_ms"] >= 0 for item in report["runs"])


def test_main_writes_report_to_file(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "make_client", lambda base_url: TestClient(app, base_url=base_url))
    report_file = tmp_path / "report.json"

    exit_code = main(["-j", SAMPLE_JSON, "-r", "2", "-o", str(report_file)])

    assert exit_code == 0
    report = json.loads(report_file.read_text())
    assert report["output"] == SAMPLE_OUTPUT
    assert len(report["runs"]) == 2


def test_main_reads_stdin_and_writes_stdout(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "make_client", lambda base_url: TestClient(app, base_url=base_url))
    monkeypatch.setattr("sys.stdin", io.StringIO(SAMPLE_JSON))

    exit_code = main(["-i", "-"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["output"] == SAMPLE_OUTPUT


def test_main_reports_http_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = FastAPI()

    @broken.post("/payload")
    def unavailable() -> None:
        raise HTTPException(503, detail="maintenance")

    monkeypatch.setattr(cli, "make_client", lambda base_url: TestClient(broken, base_url=base_url))

    assert main(["-j", SAMPLE_JSON]) == 1
    assert "503" in capsys.readouterr().err
