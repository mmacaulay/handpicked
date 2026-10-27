import shutil
from pathlib import Path

import httpx
import pytest
import respx

from handpicked.cli import main

EXAMPLE = Path(__file__).parent.parent / "config.example.yml"
TAUTULLI = "http://tautulli:8181/api/v2"


@pytest.fixture
def example_config(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    shutil.copy(EXAMPLE, config)
    monkeypatch.setenv("HANDPICKED_CONFIG", str(config))
    return config


@respx.mock
def test_run_ingests_history(example_config, capsys):
    respx.get(TAUTULLI).respond(
        json={"response": {"result": "success", "data": {"data": []}}}
    )

    assert main(["run", "--dry-run"]) == 0
    assert (example_config.parent / "handpicked.db").exists()
    out = capsys.readouterr().out
    assert "dry_run=True" in out
    assert "0 new events" in out


@respx.mock
def test_tautulli_error_exits_with_error(example_config, capsys):
    respx.get(TAUTULLI).mock(side_effect=httpx.ConnectError("refused"))

    assert main(["run"]) == 1
    assert "Tautulli error" in capsys.readouterr().err


def test_invalid_config_exits_with_error(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yml"
    config.write_text("services: {}\n")
    monkeypatch.setenv("HANDPICKED_CONFIG", str(config))

    assert main(["run"]) == 2
    assert "Invalid config" in capsys.readouterr().err
