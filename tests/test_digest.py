import smtplib
from collections import Counter

import pytest

from handpicked import digest
from handpicked.config import Digest
from handpicked.digest import DigestError, Entry, Report, render, send
from handpicked.ingest import IngestStats
from handpicked.picks import Candidate, Pick, Picks
from handpicked.pipeline import UserPicks
from handpicked.seeds import History, Seed
from handpicked.suggestions import Requested
from handpicked.tmdb import Title
from handpicked.users import User

SETTINGS = Digest(
    host="smtp.example.com",
    username="me@example.com",
    password="secret",
    sender="handpicked@example.com",
    recipients=["admin@example.com", "other@example.com"],
)
SEEDS = [Seed("movie", 1, "Seed One", 0.9), Seed("tv", 2, "Seed Two", 0.6)]


def user(user_id, name, problem=None):
    return User(user_id, name, True, None, None, None, None, problem)


def pick(tmdb_id, name, media_type="movie", certification="PG"):
    candidate = Candidate(media_type, tmdb_id, 7.84, list(SEEDS))
    title = Title(tmdb_id, media_type, name, 2020, 1000, 7.84, certification, 8)
    return Pick(candidate, title)


def entry(name, picks, requested=None):
    result = UserPicks(
        user(1, name),
        History(seeds=list(SEEDS), abandoned=[Seed("movie", 3, "Dropped", -0.5)]),
        Picks(picks, candidates=10, checked=4, rated=3),
        None,
    )
    return Entry(result, requested)


def report(**kwargs):
    first, second = pick(10, "Tom & Jerry <Uncut>"), pick(11, "A Show", "tv", None)
    defaults = dict(
        dry_run=False,
        changes=Counter(approved=2, deleted=1),
        ingest=IngestStats(new_events=5, resolved=2, unresolved=1),
        included=[user(1, "kid"), user(2, "adult", problem="no matching Seerr user")],
        excluded=[user(3, "guest")],
        entries=[
            entry(
                "kid",
                [first, second],
                [
                    Requested(first, 41, None),
                    Requested(second, None, "/request: Series Quota exceeded."),
                ],
            ),
            Entry(
                UserPicks(user(2, "adult"), History(), None, "0 seeds, needs 3"), None
            ),
        ],
    )
    return Report(**{**defaults, **kwargs})


def parts(message):
    text, html = (p.get_content() for p in message.iter_parts())
    return text, html


def test_digest_is_multipart_with_both_bodies():
    message = render(report(), SETTINGS)

    assert message.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert message["Subject"] == "Handpicked: 2 picks for 2 people"
    assert message["From"] == "handpicked@example.com"
    assert message["To"] == "admin@example.com, other@example.com"
    assert message["Message-ID"].endswith("@example.com>")


def test_text_shows_each_pick_with_its_seeds_and_outcome():
    text, _ = parts(render(report(), SETTINGS))

    assert "DRY RUN" not in text
    assert "Since the last run: 2 approved, 0 declined, 1 deleted" in text
    assert "Warning: adult: no matching Seerr user" in text
    assert "kid (2 seeds, 1 abandoned)" in text
    assert "  1. Tom & Jerry <Uncut> (2020, movie, PG, TMDB 7.8)" in text
    assert "     because you finished Seed One and Seed Two" in text
    assert "     Requested in Seerr (#41)" in text
    assert "  2. A Show (2020, TV, unrated, TMDB 7.8)" in text
    assert "     Not requested: /request: Series Quota exceeded." in text
    assert "adult (0 seeds, 0 abandoned)\n  Skipped: 0 seeds, needs 3" in text
    assert "Not included: guest" in text
    assert "Watch history: 5 new events, 2 titles resolved, 1 unresolved" in text
    assert "Age ratings: found for 3 of 4 titles checked (75%)" in text


def test_html_escapes_external_text():
    _, html = parts(render(report(), SETTINGS))

    assert "Tom &amp; Jerry &lt;Uncut&gt;" in html
    assert "<Uncut>" not in html
    assert "because you finished Seed One and Seed Two" in html
    assert "Requested in Seerr (#41)" in html
    assert "Not requested: /request: Series Quota exceeded." in html


def test_dry_run_is_marked_everywhere():
    entries = [entry("kid", [pick(10, "Pick")])]
    message = render(
        report(dry_run=True, changes=None, entries=entries, request_problem="unset"),
        SETTINGS,
    )
    text, html = parts(message)

    assert message["Subject"] == "[DRY RUN] Handpicked: 1 pick for 1 person"
    assert text.startswith("DRY RUN: nothing was requested in Seerr or recorded.")
    assert "DRY RUN: nothing was requested" in html
    assert "Warning: requests cannot be created yet: unset" in text
    assert "Since the last run" not in text
    assert "Requested in Seerr" not in text


def test_stopped_run_says_why():
    message = render(
        Report(dry_run=False, stopped="Cannot create requests: no user"), SETTINGS
    )
    text, html = parts(message)

    assert message["Subject"] == "Handpicked: the run stopped"
    assert "The run stopped before making any picks:\nCannot create requests" in text
    assert "Cannot create requests: no user" in html
    assert "Watch history" not in text


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.args = (host, port, timeout)
        self.context = context
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        assert context is not None
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.calls.append(("send", message["Subject"]))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.instances


def test_starttls_logs_in_and_sends(smtp):
    send(SETTINGS, render(report(), SETTINGS))

    (server,) = smtp
    assert server.args == ("smtp.example.com", 587, digest.TIMEOUT)
    assert server.context is None  # plain SMTP, upgraded by starttls
    assert server.calls == [
        "starttls",
        ("login", "me@example.com", "secret"),
        ("send", "Handpicked: 2 picks for 2 people"),
        "quit",
    ]


@pytest.mark.parametrize(
    ("security", "port", "expected"),
    [("ssl", None, 465), ("none", None, 25), ("ssl", 2465, 2465)],
)
def test_ports_default_by_security(smtp, security, port, expected):
    settings = SETTINGS.model_copy(update={"security": security, "port": port})

    send(settings, render(report(), settings))

    (server,) = smtp
    assert server.args[1] == expected
    assert "starttls" not in server.calls
    if security == "ssl":
        assert server.context is not None


def test_no_username_skips_login(smtp):
    settings = SETTINGS.model_copy(update={"username": None, "password": None})

    send(settings, render(report(), settings))

    assert [c for c in smtp[0].calls if c[0] == "login"] == []


def test_smtp_failure_raises_digest_error(monkeypatch):
    class Refusing(FakeSMTP):
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    monkeypatch.setattr(digest.smtplib, "SMTP", Refusing)

    with pytest.raises(DigestError, match="smtp.example.com:587: .*authentication"):
        send(SETTINGS, render(report(), SETTINGS))


def test_connection_failure_raises_digest_error(monkeypatch):
    def unreachable(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(digest.smtplib, "SMTP", unreachable)

    with pytest.raises(DigestError, match="timed out"):
        send(SETTINGS, render(report(), SETTINGS))
