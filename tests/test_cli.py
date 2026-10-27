import shutil
from pathlib import Path

from handpicked.cli import main

EXAMPLE = Path(__file__).parent.parent / "config.example.yml"


def test_run_creates_database(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yml"
    shutil.copy(EXAMPLE, config)
    monkeypatch.setenv("HANDPICKED_CONFIG", str(config))

    assert main(["run", "--dry-run"]) == 0
    assert (tmp_path / "handpicked.db").exists()
    assert "dry_run=True" in capsys.readouterr().out


def test_invalid_config_exits_with_error(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yml"
    config.write_text("services: {}\n")
    monkeypatch.setenv("HANDPICKED_CONFIG", str(config))

    assert main(["run"]) == 2
    assert "Invalid config" in capsys.readouterr().err
