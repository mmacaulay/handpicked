import fcntl
import json
import threading
from pathlib import Path

import httpx
import pytest
import respx

from handpicked import cli, db, suggestions, users
from handpicked.cli import main
from handpicked.picks import Candidate, Pick, Picks
from handpicked.pipeline import UserPicks
from handpicked.seeds import History, Seed
from handpicked.tmdb import Title

EXAMPLE = Path(__file__).parent.parent / "src" / "handpicked" / "config.example.yml"
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
    for path in ("media", "request", "blocklist"):
        respx.get(f"{SEERR}/{path}").respond(
            json={"pageInfo": {"results": 0}, "results": []}
        )


def seerr_user(id, name, permissions=32, email=None):
    return {"id": id, "displayName": name, "permissions": permissions, "email": email}


@pytest.fixture
def example_config(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(EXAMPLE.read_text() + "dry_run: true\n")
    monkeypatch.setenv("HANDPICKED_CONFIG", str(config))
    return config


@respx.mock
def test_run_ingests_history_and_reports_users(example_config, capsys):
    mock_services(tautulli_users=[(111, "matt"), (333, "emma")])
    assert main(["users"]) == 0
    assert main(["users", "set", "matt", "--include"]) == 0
    assert main(["users", "set", "emma", "--include"]) == 0

    assert main(["run", "--dry-run"]) == 0
    assert (example_config.parent / "handpicked.db").exists()
    out = capsys.readouterr().out
    assert "dry_run=True" in out
    assert "0 new events" in out
    assert "2 included (emma, matt)" in out
    assert "Warning" not in out


@respx.mock
def test_run_reports_empty_groups_without_names(example_config, capsys):
    mock_services(tautulli_users=[(111, "matt")])

    assert main(["run", "--dry-run"]) == 0
    assert "Users: 0 included, 1 not included (matt)" in capsys.readouterr().out


@respx.mock
def test_run_prints_picks_and_reasons(example_config, capsys, monkeypatch):
    finished = [Seed("movie", i, f"Seed {i}", 1.0 - i / 10) for i in range(1, 6)]
    candidate = Candidate(
        "movie", 10, 8.0, [*finished, Seed("movie", 9, "Dropped", -0.5)]
    )
    title = Title(10, "movie", "Pick", 2020, 1000, 8.0, "PG", 8)
    picked = UserPicks(
        users.User(111, "matt", True, None, None),
        History(seeds=finished, unresolved=2),
        Picks([Pick(candidate, title)], candidates=4, checked=2, rated=1),
        None,
    )
    skipped = UserPicks(
        users.User(333, "emma", True, 10, None),
        History(),
        None,
        "0 seeds, needs 3",
    )
    monkeypatch.setattr(cli, "recommend", lambda *args: [picked, skipped])
    mock_services()

    assert main(["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "matt: 5 seeds, 0 abandoned, 2 recent titles skipped (no TMDB id)" in out
    assert "1. Pick (2020, movie, PG) score 3.20" in out
    assert (
        "because you finished Seed 1, Seed 2, Seed 3 and 2 more,"
        " despite abandoning Dropped"
    ) in out
    assert "emma: 0 seeds, 0 abandoned\n  Skipped: 0 seeds, needs 3" in out
    assert "Age ratings: found for 1 of 2 titles checked (50%)" in out


@pytest.fixture
def live_config(example_config):
    text = example_config.read_text()
    example_config.write_text(text.replace("dry_run: true", "dry_run: false"))
    return example_config


HANDPICKED = seerr_user(5, "Handpicked", email=suggestions.REQUEST_USER_EMAIL)
APPROVING = seerr_user(5, "Handpicked", permissions=2, email=HANDPICKED["email"])


def user_picks(user_id, name, *tmdb_ids):
    picks = [
        Pick(
            Candidate("movie", i, 8.0, [Seed("movie", 1, "Seed", 1.0)]),
            Title(i, "movie", f"Pick {i}", 2020, 1000, 8.0, "PG", 8),
        )
        for i in tmdb_ids
    ]
    return UserPicks(
        users.User(user_id, name, True, None, None),
        History(seeds=[Seed("movie", 1, "Seed", 1.0)]),
        Picks(picks, candidates=4, checked=2, rated=2),
        None,
    )


@respx.mock
def test_run_requests_picks_and_syncs(live_config, capsys, monkeypatch):
    mock_services(
        tautulli_users=[(111, "matt"), (333, "emma")], seerr_users=[HANDPICKED]
    )
    monkeypatch.setattr(
        cli,
        "recommend",
        lambda *args: [
            user_picks(111, "matt", 10, 11),
            user_picks(333, "emma", 12),
        ],
    )
    post = respx.post(f"{SEERR}/request").mock(
        side_effect=[
            httpx.Response(201, json={"id": 41}),
            httpx.Response(403, json={"message": "Movie Quota exceeded."}),
            httpx.Response(201, json={"id": 42}),
        ]
    )
    other = respx.route(method__in=["PUT", "DELETE"])

    assert main(["run"]) == 0
    out = capsys.readouterr().out
    assert "Requests since last run: 0 approved, 0 declined, 0 deleted" in out
    assert "1. Pick 10 (2020, movie, PG) score 1.20" in out
    assert "requested in Seerr (#41)" in out
    assert "not requested: /request: Movie Quota exceeded." in out
    assert "requested in Seerr (#42)" in out
    assert "Digest not configured, not sent" in out
    assert [c.request.headers["X-API-User"] for c in post.calls] == ["5"] * 3
    assert not other.called

    respx.get(f"{SEERR}/request/41").respond(json={"status": 3})
    respx.get(f"{SEERR}/request/42").respond(json={"status": 1})
    monkeypatch.setattr(cli, "recommend", lambda *args: [])

    assert main(["run"]) == 0
    assert "0 approved, 1 declined, 0 deleted" in capsys.readouterr().out
    assert post.call_count == 3


@respx.mock
def test_an_approving_request_user_still_requests(live_config, capsys, monkeypatch):
    mock_services(tautulli_users=[(111, "matt")], seerr_users=[APPROVING])
    monkeypatch.setattr(cli, "recommend", lambda *args: [user_picks(111, "matt", 10)])
    respx.post(f"{SEERR}/request").respond(201, json={"id": 41, "status": 2})

    assert main(["run"]) == 0
    assert "requested in Seerr (#41)" in capsys.readouterr().out

    respx.get(f"{SEERR}/request/41").respond(json={"status": 2})
    monkeypatch.setattr(cli, "recommend", lambda *args: [])

    assert main(["run"]) == 0
    assert "1 approved, 0 declined, 0 deleted" in capsys.readouterr().out


@respx.mock
def test_live_run_creates_the_request_user(live_config, capsys, monkeypatch):
    mock_services(tautulli_users=[(111, "matt")])
    monkeypatch.setattr(cli, "recommend", lambda *args: [user_picks(111, "matt", 10)])
    # A new user gets the server's default permissions, here auto-approve.
    create = respx.post(f"{SEERR}/user").respond(
        201, json={"id": 9, "displayName": "Handpicked", "permissions": 32 | 128}
    )
    permissions = respx.post(f"{SEERR}/user/9/settings/permissions").respond(
        json={"permissions": 32}
    )
    quota = respx.post(f"{SEERR}/user/9/settings/main").respond(json={})
    post = respx.post(f"{SEERR}/request").respond(201, json={"id": 41})

    assert main(["run"]) == 0
    assert "Created Seerr user Handpicked (id 9)" in capsys.readouterr().out
    body = json.loads(create.calls.last.request.content)
    assert body["email"] == suggestions.REQUEST_USER_EMAIL
    assert len(body["password"]) >= 32
    assert json.loads(permissions.calls.last.request.content) == {"permissions": 32}
    assert json.loads(quota.calls.last.request.content) == {
        "username": "Handpicked",
        "email": suggestions.REQUEST_USER_EMAIL,
        "movieQuotaLimit": 0,
        "tvQuotaLimit": 0,
    }
    assert post.calls.last.request.headers["X-API-User"] == "9"


@respx.mock
def test_dry_run_warns_and_requests_nothing(example_config, capsys, monkeypatch):
    mock_services(tautulli_users=[(111, "matt")])
    monkeypatch.setattr(cli, "recommend", lambda *args: [user_picks(111, "matt", 10)])
    post = respx.post(f"{SEERR}/request")
    create = respx.post(f"{SEERR}/user")

    assert main(["run"]) == 0
    out = capsys.readouterr().out
    assert "Seerr user Handpicked not found; the first live run creates it" in out
    assert not create.called
    assert "Requests since last run" not in out
    assert "requested in Seerr" not in out
    assert not post.called
    conn = db.connect(example_config.parent / "handpicked.db")
    assert conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 0


DIGEST = """
digest:
  host: smtp.example.com
  sender: handpicked@example.com
  recipients: [admin@example.com]
"""


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(cli.digest, "send", lambda _, message: messages.append(message))
    return messages


def add_digest(config):
    config.write_text(config.read_text() + DIGEST)


def text_part(message):
    return next(message.iter_parts()).get_content()


@respx.mock
def test_dry_run_sends_the_digest(example_config, sent, capsys, monkeypatch):
    add_digest(example_config)
    mock_services(tautulli_users=[(111, "matt")])
    monkeypatch.setattr(cli, "recommend", lambda *args: [user_picks(111, "matt", 10)])

    assert main(["run"]) == 0
    (message,) = sent
    assert message["Subject"] == "[DRY RUN] Handpicked: 1 pick for 1 person"
    assert "1. Pick 10 (2020, movie, PG, TMDB 8.0)" in text_part(message)
    assert "Digest sent to admin@example.com" in capsys.readouterr().out


@respx.mock
def test_real_run_digest_shows_what_seerr_did(live_config, sent, monkeypatch):
    add_digest(live_config)
    mock_services(
        tautulli_users=[(111, "matt"), (333, "emma")], seerr_users=[HANDPICKED]
    )
    monkeypatch.setattr(
        cli,
        "recommend",
        lambda *args: [
            user_picks(111, "matt", 10),
            user_picks(333, "emma", 12),
        ],
    )
    respx.post(f"{SEERR}/request").mock(
        side_effect=[
            httpx.Response(201, json={"id": 41}),
            httpx.Response(201, json={"id": 42}),
        ]
    )

    assert main(["run"]) == 0
    text = text_part(sent[0])
    assert "DRY RUN" not in text
    assert "Since the last run: 0 approved, 0 declined, 0 deleted" in text
    assert "Requested in Seerr (#41)" in text
    assert "Requested in Seerr (#42)" in text


@respx.mock
def test_stopped_run_still_sends_the_digest(live_config, sent):
    add_digest(live_config)
    mock_services()
    respx.post(f"{SEERR}/user").respond(500, json={"message": "boom"})

    assert main(["run"]) == 1
    (message,) = sent
    assert message["Subject"] == "Handpicked: the run stopped"
    assert "Seerr error: /user: boom" in text_part(message)


@respx.mock
def test_service_error_still_sends_the_digest(example_config, sent, capsys):
    add_digest(example_config)
    respx.get(f"{SEERR}/user").respond(500, json={"message": "boom"})

    assert main(["run"]) == 1
    assert "Seerr error: /user: boom" in capsys.readouterr().err
    (message,) = sent
    assert message["Subject"] == "[DRY RUN] Handpicked: the run stopped"
    assert "Seerr error: /user: boom" in text_part(message)


@respx.mock
def test_unexpected_error_sends_the_digest_then_raises(
    example_config, sent, monkeypatch
):
    add_digest(example_config)
    mock_services()

    def broken(*args):
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "recommend", broken)

    with pytest.raises(RuntimeError, match="bug"):
        main(["run"])
    (message,) = sent
    assert "Unexpected error (RuntimeError), see the logs" in text_part(message)
    assert "bug" not in text_part(message)


@respx.mock
def test_digest_render_failure_keeps_the_original_error(example_config, monkeypatch):
    add_digest(example_config)
    mock_services()

    def broken(*args):
        raise RuntimeError("bug")

    def render(*args):
        raise TypeError("template")

    monkeypatch.setattr(cli, "recommend", broken)
    monkeypatch.setattr(cli.digest, "render", render)

    with pytest.raises(RuntimeError, match="bug") as error:
        main(["run"])
    assert isinstance(error.value.__context__, TypeError)


@respx.mock
def test_digest_failure_fails_the_run(example_config, capsys, monkeypatch):
    add_digest(example_config)
    mock_services()

    def refuse(settings, message):
        raise cli.digest.DigestError("smtp.example.com:587: refused")

    monkeypatch.setattr(cli.digest, "send", refuse)

    assert main(["run"]) == 1
    assert "Digest not sent: smtp.example.com:587: refused" in capsys.readouterr().err


@respx.mock
def test_users_lists_discovered_users(example_config, capsys):
    mock_services(tautulli_users=[(111, "matt"), (333, "Emma")])

    assert main(["users"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["User", "Included", "Max", "age", "Picks"]
    assert lines[1].split() == ["Emma", "no", "-", "3", "(default)"]
    assert lines[2].split() == ["matt", "no", "-", "3", "(default)"]
    assert not any(str(c.request.url).startswith(SEERR) for c in respx.calls)


@respx.mock
def test_users_set(example_config, capsys):
    mock_services(tautulli_users=[(333, "emma")])
    assert main(["users"]) == 0

    assert (
        main(["users", "set", "emma", "--include", "--max-age", "10", "--picks", "5"])
        == 0
    )
    assert main(["users"]) == 0

    out = capsys.readouterr().out
    assert "Updated emma." in out
    assert out.splitlines()[-1].split() == ["emma", "yes", "10", "5"]


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
def test_tautulli_error_exits_with_error(example_config, capsys):
    respx.get(f"{SEERR}/user").respond(json={"pageInfo": {"results": 0}, "results": []})
    respx.get(TAUTULLI).mock(side_effect=httpx.ConnectError("refused"))

    assert main(["run"]) == 1
    assert "Tautulli error" in capsys.readouterr().err


def test_invalid_config_exits_with_error(example_config, capsys):
    text = example_config.read_text().replace("picks_per_run: 3", "picks_per_run: 0")
    example_config.write_text(text)

    assert main(["run"]) == 2
    assert "Invalid config" in capsys.readouterr().err


@respx.mock
def test_a_run_in_progress_blocks_another(example_config, capsys):
    mock_services()
    with open(example_config.parent / "handpicked.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert main(["run"]) == 1
    assert "A run is already in progress" in capsys.readouterr().err
    assert main(["run"]) == 0


@pytest.mark.parametrize("command", ["run", "serve"])
def test_missing_settings_exit_without_creating_a_config(
    tmp_path, monkeypatch, capsys, command
):
    path = tmp_path / "config.yml"
    monkeypatch.setenv("HANDPICKED_CONFIG", str(path))

    assert main([command]) == 2
    assert capsys.readouterr().err.startswith("Missing settings: Tautulli URL")
    assert not path.exists()


@respx.mock
def test_run_from_the_environment_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HANDPICKED_CONFIG", str(tmp_path / "config.yml"))
    for var, value in {
        "HANDPICKED_TAUTULLI_URL": "http://tautulli:8181",
        "HANDPICKED_TAUTULLI_API_KEY": "t",
        "HANDPICKED_SEERR_URL": "http://seerr:5055",
        "HANDPICKED_SEERR_API_KEY": "s",
        "HANDPICKED_TMDB_TOKEN": "m",
    }.items():
        monkeypatch.setenv(var, value)
    mock_services(tautulli_users=[(111, "matt")])

    assert main(["run", "--dry-run"]) == 0
    assert "Users: 0 included, 1 not included (matt)" in capsys.readouterr().out
    assert (tmp_path / "handpicked.db").exists()


@respx.mock
def test_scheduled_run_works_off_the_main_thread(example_config, caplog):
    mock_services(tautulli_users=[(111, "matt")])
    errors = []

    def job():
        try:
            cli._scheduled_run()
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=job)
    thread.start()
    thread.join(10)

    assert errors == []
    conn = db.connect(example_config.parent / "handpicked.db")
    assert conn.execute("SELECT username FROM users").fetchall()[0][0] == "matt"
    assert "Run exited" not in caplog.text


def test_scheduled_run_logs_a_failed_run(example_config, caplog, monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv: 1)

    cli._scheduled_run()

    assert "Run exited with status 1" in caplog.text
