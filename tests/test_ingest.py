from datetime import UTC, datetime

import pytest

from handpicked import db
from handpicked.ingest import ingest, resolve_tmdb_ids, sync_history

MON = int(datetime(2026, 10, 5, 20, tzinfo=UTC).timestamp())


def movie(id, rating_key=100, user_id=1, percent=90, started=MON, **extra):
    return {
        "id": id,
        "reference_id": id,
        "user_id": user_id,
        "media_type": "movie",
        "rating_key": rating_key,
        "grandparent_rating_key": "",
        "title": "Dune",
        "grandparent_title": "",
        "year": 2021,
        "percent_complete": percent,
        "started": started,
        "stopped": started + 3600,
        "live": 0,
        **extra,
    }


def episode(id, rating_key, show_key=500, **extra):
    return movie(
        id,
        rating_key=rating_key,
        media_type="episode",
        grandparent_rating_key=show_key,
        title="Pilot",
        grandparent_title="Severance",
        **extra,
    )


class FakeTautulli:
    def __init__(self, rows=(), guids=None):
        self.rows = list(rows)
        self.guids = guids or {}
        self.history_calls = []
        self.metadata_calls = []

    def history(self, after=None):
        self.history_calls.append(after)
        return iter(self.rows)

    def metadata(self, rating_key):
        self.metadata_calls.append(rating_key)
        if rating_key not in self.guids:
            return None
        return {"guids": self.guids[rating_key]}


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def events(conn):
    return conn.execute("SELECT * FROM watch_events ORDER BY id").fetchall()


def test_first_sync_fetches_full_history(conn):
    tautulli = FakeTautulli([movie(1), episode(2, rating_key=501)])

    assert sync_history(conn, tautulli) == 2
    assert tautulli.history_calls == [None]
    movie_row, episode_row = events(conn)
    assert movie_row["title"] == "Dune"
    assert movie_row["grandparent_rating_key"] is None
    assert episode_row["title"] == "Severance"
    assert episode_row["grandparent_rating_key"] == 500


def test_incremental_sync_overlaps_and_skips_duplicates(conn):
    sync_history(conn, FakeTautulli([movie(1)]))
    tautulli = FakeTautulli([movie(1), movie(2, started=MON + 86400)])

    assert sync_history(conn, tautulli) == 1
    assert tautulli.history_calls == ["2026-10-03"]
    assert [row["id"] for row in events(conn)] == [1, 2]


def test_skips_live_tv_and_other_media(conn):
    rows = [movie(1), episode(2, rating_key=501, live=1), movie(3, media_type="track")]

    assert sync_history(conn, FakeTautulli(rows)) == 1
    assert [row["id"] for row in events(conn)] == [1]


def test_resolves_movies_and_shows(conn):
    rows = [movie(1), episode(2, rating_key=501), episode(3, rating_key=502)]
    tautulli = FakeTautulli(
        rows, guids={100: ["imdb://tt1160419", "tmdb://438631"], 500: ["tmdb://95396"]}
    )
    sync_history(conn, tautulli)

    assert resolve_tmdb_ids(conn, tautulli) == (2, 0)
    assert sorted(tautulli.metadata_calls) == [100, 500]
    stored = dict(conn.execute("SELECT rating_key, tmdb_id FROM tmdb_ids"))
    assert stored == {100: 438631, 500: 95396}


def test_resolved_ids_are_not_requested_again(conn):
    tautulli = FakeTautulli([movie(1)], guids={100: ["tmdb://438631"]})
    ingest(conn, tautulli)
    tautulli.metadata_calls.clear()

    assert resolve_tmdb_ids(conn, tautulli) == (0, 0)
    assert tautulli.metadata_calls == []


def test_unresolved_items_are_counted_and_retried(conn):
    rows = [movie(1, rating_key=100), movie(2, rating_key=200)]
    tautulli = FakeTautulli(rows, guids={200: ["imdb://tt0000001"]})

    stats = ingest(conn, tautulli)
    assert (stats.new_events, stats.resolved, stats.unresolved) == (2, 0, 2)

    tautulli.guids[100] = ["tmdb://438631"]
    assert resolve_tmdb_ids(conn, tautulli) == (1, 1)
