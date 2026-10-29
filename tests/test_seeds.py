import pytest

from handpicked import db
from handpicked.config import Thresholds
from handpicked.seeds import (
    ABANDONED_WEIGHT,
    DAY,
    REWATCH_BOOST,
    Viewing,
    analyze,
    load_viewings,
)

NOW = 1_800_000_000
UNRESOLVED = object()


def recency(days_ago):
    return 1 - 0.5 * days_ago / 90


def movie(key, pct, days_ago, *, hours=0, tmdb_id=None):
    return viewing("movie", key, key, pct, days_ago, hours, tmdb_id)


def episode(key, show, pct, days_ago, *, hours=0, tmdb_id=None):
    return viewing("episode", key, show, pct, days_ago, hours, tmdb_id)


def viewing(media_type, key, item, pct, days_ago, hours, tmdb_id):
    return Viewing(
        media_type=media_type,
        rating_key=key,
        item_key=item,
        tmdb_id=None if tmdb_id is UNRESOLVED else tmdb_id or item + 1000,
        title=f"title {item}",
        percent_complete=pct,
        stopped=NOW - days_ago * DAY + hours * 3600,
    )


def run(*viewings, **thresholds):
    ordered = sorted(viewings, key=lambda v: v.stopped)
    return analyze(ordered, Thresholds(**thresholds), NOW)


def weights(seeds):
    return {(s.media_type, s.tmdb_id): s.weight for s in seeds}


def test_finished_movies_in_window_are_seeds_weighted_by_recency():
    history = run(movie(1, 100, 0), movie(2, 80, 45), movie(3, 100, 91))

    assert weights(history.seeds) == {
        ("movie", 1001): pytest.approx(1.0),
        ("movie", 1002): pytest.approx(0.75),
    }
    assert history.abandoned == []


def test_unfinished_movie_is_neither_seed_nor_abandoned():
    history = run(movie(1, 79, 1), movie(2, 25, 1))

    assert history.seeds == []
    assert history.abandoned == []


def test_rewatched_movie_is_boosted():
    history = run(movie(1, 100, 200), movie(1, 100, 10))

    assert weights(history.seeds) == {
        ("movie", 1001): pytest.approx(recency(10) * REWATCH_BOOST)
    }


def test_second_finish_within_a_day_is_not_a_rewatch():
    history = run(movie(1, 85, 10), movie(1, 100, 10, hours=3))

    assert weights(history.seeds) == {("movie", 1001): pytest.approx(recency(10))}


def test_copies_of_one_title_are_one_seed():
    # e.g. HD and 4K libraries: different Plex items, same TMDB id.
    history = run(
        movie(1, 10, 5, tmdb_id=1500),
        movie(2, 100, 3, tmdb_id=1500),
        movie(3, 100, 1, tmdb_id=1500),
    )

    assert weights(history.seeds) == {("movie", 1500): pytest.approx(recency(1))}
    assert history.abandoned == []


def test_abandoned_movie_is_a_mild_negative():
    history = run(movie(1, 24, 5))

    assert weights(history.abandoned) == {("movie", 1001): ABANDONED_WEIGHT}
    assert history.seeds == []


def test_movie_finished_before_is_never_abandoned():
    history = run(movie(1, 100, 200), movie(1, 10, 5))

    assert history.seeds == []
    assert history.abandoned == []


def test_movie_abandoned_before_the_window_is_ignored():
    assert run(movie(1, 10, 91)).abandoned == []


def test_show_with_enough_finished_episodes_is_a_seed():
    history = run(
        episode(11, 100, 100, 30),
        episode(12, 100, 90, 20),
        episode(13, 100, 100, 10),
        episode(14, 100, 50, 1),
    )

    assert weights(history.seeds) == {("tv", 1100): pytest.approx(recency(10))}


def test_show_needs_distinct_finished_episodes():
    history = run(
        episode(11, 100, 100, 30),
        episode(11, 100, 100, 20),
        episode(12, 100, 100, 10),
        episode(13, 100, 100, 100),  # finished before the window
    )

    assert history.seeds == []


def test_show_with_a_rewatched_episode_is_boosted():
    history = run(
        episode(11, 100, 100, 100),
        episode(11, 100, 100, 30),
        episode(12, 100, 100, 20),
        episode(13, 100, 100, 10),
    )

    assert weights(history.seeds) == {
        ("tv", 1100): pytest.approx(recency(10) * REWATCH_BOOST)
    }


def test_rewatch_before_the_window_does_not_boost():
    history = run(
        episode(11, 100, 100, 300),
        episode(11, 100, 100, 200),
        episode(12, 100, 100, 20),
        episode(13, 100, 100, 15),
        episode(14, 100, 100, 10),
    )

    assert weights(history.seeds) == {("tv", 1100): pytest.approx(recency(10))}


@pytest.mark.parametrize("episodes", [1, 2])
def test_show_dropped_after_one_or_two_episodes_is_abandoned(episodes):
    history = run(*(episode(10 + i, 100, 100, 40) for i in range(episodes)))

    assert weights(history.abandoned) == {("tv", 1100): ABANDONED_WEIGHT}


def test_show_is_not_abandoned_until_the_weeks_pass():
    history = run(episode(11, 100, 100, 40), episode(12, 100, 100, 27))

    assert history.abandoned == []


def test_show_with_three_episodes_watched_is_not_abandoned():
    history = run(*(episode(10 + i, 100, 50, 40) for i in range(3)))

    assert history.seeds == []
    assert history.abandoned == []


def test_unresolved_titles_are_counted_in_the_window_only():
    history = run(
        movie(1, 100, 1, tmdb_id=UNRESOLVED),
        episode(11, 100, 100, 1, tmdb_id=UNRESOLVED),
        episode(12, 100, 100, 2, tmdb_id=UNRESOLVED),
        movie(2, 100, 200, tmdb_id=UNRESOLVED),
    )

    assert history.unresolved == 2
    assert history.seeds == []


def test_watched_includes_every_resolved_title():
    history = run(movie(1, 5, 400), episode(11, 100, 50, 300))

    assert history.watched == {("movie", 1001), ("tv", 1100)}


def test_load_viewings_groups_sessions(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    events = [
        # id, reference_id, user_id, type, rating_key, show, pct, stopped
        (1, 1, 7, "movie", 5, None, 40, 100),
        (2, 1, 7, "movie", 5, None, 90, 200),
        (3, 3, 7, "episode", 51, 50, 5000, 300),  # no duration
        (4, 4, 7, "episode", 61, None, 100, 400),  # no show key
        (5, 5, 8, "movie", 5, None, 100, 500),  # another user
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO watch_events (
                id, reference_id, user_id, media_type, rating_key,
                grandparent_rating_key, title, percent_complete, started, stopped
            ) VALUES (?, ?, ?, ?, ?, ?, 'title', ?, 0, ?)
            """,
            events,
        )
        conn.execute("INSERT INTO tmdb_ids VALUES (5, 603, 0)")

    viewings = load_viewings(conn, 7)

    assert [(v.rating_key, v.item_key, v.tmdb_id) for v in viewings] == [
        (5, 5, 603),
        (51, 50, None),
    ]
    assert [(v.percent_complete, v.stopped) for v in viewings] == [
        (90, 200),
        (100, 300),
    ]
