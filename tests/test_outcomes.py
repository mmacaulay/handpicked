from collections import Counter

import pytest
from test_pipeline import CONFIG, NOW

from handpicked import db
from handpicked.outcomes import WEEK, landed, record
from handpicked.suggestions import Change


def counts(changes):
    return Counter(c.status for c in changes)


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    with conn:
        conn.execute("INSERT INTO users (user_id, username) VALUES (7, 'user7')")
        conn.execute("INSERT INTO users (user_id, username) VALUES (8, 'user8')")
    yield conn
    conn.close()


def pick(conn, tmdb_id, media_type="movie", status="available", available=0, user_id=7):
    """A pick requested at 0, available `available` weeks before NOW."""
    with conn:
        conn.execute(
            """
            INSERT INTO suggestions (
                user_id, media_type, tmdb_id, title, score, seeds, status,
                proposed_at, requested_at, available_at, ignored_at
            ) VALUES (?, ?, ?, 'Pick', 1, '[]', ?, 0, 0, ?, ?)
            """,
            (
                user_id,
                media_type,
                tmdb_id,
                status,
                NOW - available * WEEK,
                NOW - WEEK if status == "ignored" else None,
            ),
        )


def view(conn, tmdb_id, weeks_ago, percent=100, episode=None, user_id=7):
    """One viewing of a movie, or of a show's episode, `weeks_ago` before NOW."""
    rating_key = tmdb_id if episode is None else tmdb_id * 100 + episode
    with conn:
        conn.execute(
            """
            INSERT INTO watch_events (
                reference_id, user_id, media_type, rating_key,
                grandparent_rating_key, title, percent_complete, started, stopped
            ) VALUES (
                (SELECT COUNT(*) FROM watch_events), ?, ?, ?, ?, 'title', ?, 0, ?
            )
            """,
            (
                user_id,
                "movie" if episode is None else "episode",
                rating_key,
                None if episode is None else tmdb_id,
                percent,
                NOW - weeks_ago * WEEK,
            ),
        )
        conn.execute("INSERT OR IGNORE INTO tmdb_ids VALUES (?, ?, 0)", (tmdb_id,) * 2)


def outcomes(conn):
    return {
        (r["media_type"], r["tmdb_id"]): r["status"]
        for r in conn.execute("SELECT media_type, tmdb_id, status FROM suggestions")
    }


def test_a_finished_movie_is_watched(conn):
    pick(conn, 1, available=1)
    view(conn, 1, 0.5, percent=85)

    assert counts(record(conn, CONFIG, NOW)) == {"watched": 1}
    (row,) = conn.execute("SELECT * FROM suggestions").fetchall()
    assert (row["status"], row["watched_at"]) == ("watched", NOW)


def test_a_show_is_watched_at_the_minimum_finished_episodes(conn):
    pick(conn, 1, "tv", available=3)
    pick(conn, 2, "tv", available=3)
    for episode in (1, 2, 3):
        view(conn, 1, 2, episode=episode)
    view(conn, 2, 2, episode=1)
    view(conn, 2, 2, episode=1)  # a rewatch is one finished episode
    view(conn, 2, 2, episode=2, percent=50)

    assert counts(record(conn, CONFIG, NOW)) == {"watched": 1}
    assert outcomes(conn) == {("tv", 1): "watched", ("tv", 2): "available"}


@pytest.mark.parametrize(
    ("media_type", "weeks", "expected"),
    [("movie", 2, "abandoned"), ("movie", 1.9, "available")]
    + [("tv", 4, "abandoned"), ("tv", 3.9, "available")],
)
def test_a_started_pick_is_abandoned_after_the_window(
    conn, media_type, weeks, expected
):
    pick(conn, 1, media_type, available=10)
    view(conn, 1, 9, percent=40, episode=None if media_type == "movie" else 1)
    view(conn, 1, weeks, percent=40, episode=None if media_type == "movie" else 2)

    record(conn, CONFIG, NOW)

    assert outcomes(conn) == {(media_type, 1): expected}


def test_the_abandon_window_starts_once_the_pick_is_available(conn):
    # Started while only part of season 1 was in the library.
    pick(conn, 1, "tv", available=0)
    view(conn, 1, 10, percent=50, episode=1)

    assert counts(record(conn, CONFIG, NOW)) == {}
    assert counts(record(conn, CONFIG, NOW + 4 * WEEK)) == {"abandoned": 1}


@pytest.mark.parametrize(
    ("media_type", "weeks", "expected"),
    [("movie", 4, "ignored"), ("movie", 3.9, "available")]
    + [("tv", 8, "ignored"), ("tv", 7.9, "available")],
)
def test_an_unstarted_pick_is_ignored_after_the_window(
    conn, media_type, weeks, expected
):
    pick(conn, 1, media_type, available=weeks)

    record(conn, CONFIG, NOW)

    assert outcomes(conn) == {(media_type, 1): expected}


def test_an_ignored_pick_started_later_is_settled(conn):
    for tmdb_id in (1, 2, 3):
        pick(conn, tmdb_id, status="ignored", available=10)
    view(conn, 1, 0)  # finished
    view(conn, 2, 3, percent=10)  # started, then left
    view(conn, 3, 0, percent=10)  # still watching

    assert counts(record(conn, CONFIG, NOW)) == {"watched": 1, "abandoned": 1}
    assert outcomes(conn) == {
        ("movie", 1): "watched",
        ("movie", 2): "abandoned",
        ("movie", 3): "ignored",
    }
    ignored_at = conn.execute("SELECT ignored_at FROM suggestions").fetchall()
    assert [r["ignored_at"] for r in ignored_at] == [NOW - WEEK] * 3


def test_watching_before_the_run_noticed_it_counts(conn):
    # Requested at 0, so a viewing before available_at but after the request
    # is a viewing of the pick.
    pick(conn, 1, available=0)
    view(conn, 1, 0.5)

    assert counts(record(conn, CONFIG, NOW)) == {"watched": 1}


def test_viewings_before_the_request_do_not_count(conn):
    pick(conn, 1, available=5)
    with conn:
        conn.execute("UPDATE suggestions SET requested_at = ?", (NOW - WEEK,))
    view(conn, 1, 2)

    assert counts(record(conn, CONFIG, NOW)) == {"ignored": 1}


def test_only_the_members_own_viewings_of_that_title_count(conn):
    pick(conn, 1, available=5)
    pick(conn, 2, "tv", available=10)
    view(conn, 1, 1, user_id=8)
    for episode in (1, 2, 3):
        view(conn, 3, 1, episode=episode)
    view(conn, 2, 1)  # a movie with the show's TMDB id

    assert counts(record(conn, CONFIG, NOW)) == {"ignored": 2}


def test_settled_picks_are_not_checked_again(conn):
    for tmdb_id, status in enumerate(["approved", "watched", "abandoned"]):
        pick(conn, tmdb_id, status=status, available=10)

    assert counts(record(conn, CONFIG, NOW)) == {}
    assert set(outcomes(conn).values()) == {"approved", "watched", "abandoned"}


def test_a_dry_run_counts_outcomes_without_recording_them(conn):
    pick(conn, 1, available=1)
    pick(conn, 2, available=10)
    view(conn, 1, 0)

    assert counts(record(conn, CONFIG, NOW, dry_run=True)) == {
        "watched": 1,
        "ignored": 1,
    }
    assert set(outcomes(conn).values()) == {"available"}


def change(row_id, user_id, status):
    return Change(row_id, user_id, "movie", row_id, "Pick", None, [], status)


def test_landed_groups_this_runs_changes_and_totals_by_member(conn):
    with conn:
        conn.execute("INSERT INTO users (user_id, username) VALUES (9, 'Abby')")
    for tmdb_id, status in enumerate(["watched", "watched", "abandoned", "ignored"]):
        pick(conn, tmdb_id, status=status)
    pick(conn, 10, status="watched", user_id=9)
    changes = [
        change(1, 7, "available"),
        change(2, 7, "approved"),  # not shown
        change(3, 8, "available"),
        change(3, 8, "watched"),  # available and watched in the same run
        change(4, 8, "ignored"),
    ]

    abby, user7, user8 = landed(conn, changes)

    assert (abby.username, abby.available, abby.outcomes) == ("Abby", [], [])
    assert abby.hit_rate == 1
    assert user7.username == "user7"
    assert user7.available == [changes[0]]
    assert user7.outcomes == []
    assert user7.totals == {"watched": 2, "abandoned": 1, "ignored": 1}
    assert user7.hit_rate == 0.5
    assert user8.available == []
    assert user8.outcomes == [changes[3], changes[4]]
    assert user8.hit_rate is None  # nothing recorded yet, e.g. a dry run


def test_landed_is_empty_with_nothing_to_report(conn):
    pick(conn, 1)

    assert landed(conn, []) == []
