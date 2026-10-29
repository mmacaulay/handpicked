import shutil
from pathlib import Path

import httpx
import pytest
import respx

from handpicked.cli import main

EXAMPLE = Path(__file__).parent.parent / "config.example.yml"
TAUTULLI = "http://tautulli:8181/api/v2"
SEERR = "http://seerr:5055/api/v1"


def tautulli_data(data):
    return {"response": {"result": "success", "data": data}}


def mock_services(tautulli_users=(), seerr_users=()):
    respx.get(TAUTULLI, params={"cmd": "get_history"}).respond(
        json=tautulli_data({"data": []})
    )
    respx.get(TAUTULLI, params={"cmd": "get_users"}).respond(
        json=tautulli_data(
            [{"user_id": i, "username": n, "is_active": 1} for i, n in tautulli_users]
        )
    )
    respx.get(f"{SEERR}/user").respond(
        json={"pageInfo": {"results": len(seerr_users)}, "results": list(seerr_users)}
    )


def seerr_user(id, name, plex_id=None, permissions=32):
    return {
        "id": id,
        "plexId": plex_id,
        "displayName": name,
        "permissions": permissions,
    }


@pytest.fixture
def example_config(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    shutil.copy(EXAMPLE, config)
    monkeypatch.setenv("HANDPICKED_CONFIG", str(config))
    return config


@respx.mock
def test_run_ingests_history_and_reports_users(example_config, capsys):
    mock_services(
        tautulli_users=[(111, "matt"), (333, "emma")],
        seerr_users=[seerr_user(1, "matt", plex_id=111, permissions=2)],
    )
    assert main(["users"]) == 0
    assert main(["users", "set", "matt", "--include"]) == 0
    assert main(["users", "set", "emma", "--include"]) == 0

    assert main(["run", "--dry-run"]) == 0
    assert (example_config.parent / "handpicked.db").exists()
    out = capsys.readouterr().out
    assert "dry_run=True" in out
    assert "0 new events" in out
    assert "2 included (emma, matt)" in out
    assert "Warning: emma: no matching Seerr user" in out
    assert "Warning: matt" not in out


@respx.mock
def test_run_reports_empty_groups_without_names(example_config, capsys):
    mock_services(tautulli_users=[(111, "matt")])

    assert main(["run", "--dry-run"]) == 0
    assert "Users: 0 included, 1 not included (matt)" in capsys.readouterr().out


@respx.mock
def test_users_lists_discovered_users(example_config, capsys):
    mock_services(
        tautulli_users=[(111, "matt"), (333, "Emma")],
        seerr_users=[
            seerr_user(1, "matt", plex_id=111, permissions=2),
            seerr_user(7, "emma"),
        ],
    )

    assert main(["users"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "User",
        "Included",
        "Max",
        "age",
        "Picks",
        "Seerr",
        "user",
        "Notes",
    ]
    assert lines[1].split()[:6] == ["Emma", "no", "-", "3", "(default)", "emma"]
    assert "(id 7, by name)" in lines[1]
    assert lines[2].endswith("matt (id 1, by plex)")


@respx.mock
def test_users_set_and_link(example_config, capsys):
    mock_services(
        tautulli_users=[(333, "emma")],
        seerr_users=[seerr_user(7, "kiddo")],
    )
    assert main(["users"]) == 0

    assert (
        main(["users", "set", "emma", "--include", "--max-age", "10", "--picks", "5"])
        == 0
    )
    assert main(["users", "link", "emma", "7"]) == 0
    assert main(["users"]) == 0

    out = capsys.readouterr().out
    assert "Updated emma." in out
    assert "Linked emma to Seerr user 7." in out
    assert out.splitlines()[-1].split()[:6] == [
        "emma",
        "yes",
        "10",
        "5",
        "kiddo",
        "(id",
    ]


def test_users_set_unknown_user(example_config, capsys):
    assert main(["users", "set", "nobody", "--include"]) == 1
    assert "Run `handpicked users`" in capsys.readouterr().err


def test_users_set_needs_a_change(example_config, capsys):
    assert main(["users", "set", "emma"]) == 2
    assert "Nothing to change" in capsys.readouterr().err


def test_users_set_rejects_bad_values(example_config):
    with pytest.raises(SystemExit):
        main(["users", "set", "emma", "--picks", "0"])
    with pytest.raises(SystemExit):
        main(["users", "set", "emma", "--max-age", "10", "--no-max-age"])


@respx.mock
def test_seerr_error_exits_with_error(example_config, capsys):
    respx.get(TAUTULLI).respond(json=tautulli_data([]))
    respx.get(f"{SEERR}/user").mock(side_effect=httpx.ConnectError("refused"))

    assert main(["users"]) == 1
    assert "Seerr error" in capsys.readouterr().err


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
