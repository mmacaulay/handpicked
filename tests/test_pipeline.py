import pytest
from test_picks import FakeTmdb, rec

from handpicked import db
from handpicked.config import Config
from handpicked.pipeline import recommend
from handpicked.seeds import DAY
from handpicked.seerr import SeerrUser
from handpicked.tmdb import TmdbCache, TmdbError
from handpicked.users import User

NOW = 1_800_000_000
CONFIG = Config.model_validate(
    {
        "services": {
            "tautulli": {"url": "http://tautulli", "api_key": "k"},
            "seerr": {"url": "http://seerr", "api_key": "k"},
            "tmdb": {"token": "t"},
        },
        "thresholds": {"min_seeds": 2},
    }
)


class FakeSeerr:
    def __init__(self, owned=(), requested=(), blocklisted=()):
        self.sets = set(owned), set(requested), set(blocklisted)
        self.calls = 0

    def owned(self):
        self.calls += 1
        return self.sets[0]

    def requested(self):
        return self.sets[1]

    def blocklisted(self):
        return self.sets[2]


LINKED = SeerrUser(id=3, plex_id=None, display_name="kid", permissions=32)


def user(user_id=7, include=True, picks_per_run=None, seerr_user=LINKED):
    return User(
        user_id, f"user{user_id}", include, None, picks_per_run, None, seerr_user, None
    )


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    with conn:
        conn.execute("INSERT INTO users (user_id, username) VALUES (7, 'user7')")
    yield conn
    conn.close()


def watch(conn, user_id, *movies):
    """Record a finished, resolved movie per TMDB id, watched in that order."""
    with conn:
        for tmdb_id in movies:
            conn.execute(
                """
                INSERT INTO watch_events (
                    reference_id, user_id, media_type, rating_key, title,
                    percent_complete, started, stopped
                ) VALUES (?, ?, 'movie', ?, 'title', 100, 0, ?)
                """,
                (tmdb_id, user_id, tmdb_id, NOW - DAY + tmdb_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO tmdb_ids VALUES (?, ?, 0)", (tmdb_id, tmdb_id)
            )


def test_users_below_min_seeds_are_skipped(conn):
    watch(conn, 7, 1)

    (result,) = recommend(conn, CONFIG, FakeTmdb({}), FakeSeerr(), [user()], NOW)

    assert result.picks is None
    assert result.skipped == "1 seeds, needs 2"


def test_no_included_users_reads_nothing(conn):
    seerr = FakeSeerr()

    assert recommend(conn, CONFIG, FakeTmdb({}), seerr, [], NOW) == []
    assert seerr.calls == 0


def test_excludes_seerr_titles_past_suggestions_and_history(conn):
    watch(conn, 7, 1, 2)
    with conn:
        conn.execute(
            "INSERT INTO suggestions (user_id, media_type, tmdb_id, score, seeds,"
            " status, proposed_at) VALUES (7, 'movie', 13, 1, '[]', 'declined', 0)"
        )
    recs = [rec(i) for i in (2, 10, 11, 12, 13, 14)]
    seerr = FakeSeerr(
        owned={("movie", 10)}, requested={("movie", 11)}, blocklisted={("movie", 12)}
    )

    (result,) = recommend(
        conn, CONFIG, FakeTmdb({("movie", 1): recs}), seerr, [user()], NOW
    )

    assert [p.candidate.tmdb_id for p in result.picks.picks] == [14]


def test_picks_per_run_falls_back_to_config(conn):
    watch(conn, 7, 1, 2)
    tmdb = FakeTmdb({("movie", 1): [rec(i) for i in range(10, 20)]})

    (default,) = recommend(conn, CONFIG, tmdb, FakeSeerr(), [user()], NOW)
    (custom,) = recommend(conn, CONFIG, tmdb, FakeSeerr(), [user(picks_per_run=5)], NOW)

    assert len(default.picks.picks) == CONFIG.picks_per_run
    assert len(custom.picks.picks) == 5


def test_a_title_goes_to_the_first_user_it_is_picked_for(conn):
    watch(conn, 7, 1, 2)
    watch(conn, 8, 1, 2)
    tmdb = FakeTmdb({("movie", 1): [rec(10), rec(11)]})
    included = [user(7, picks_per_run=1), user(8, picks_per_run=1)]

    first, second = recommend(conn, CONFIG, tmdb, FakeSeerr(), included, NOW)

    assert [p.candidate.tmdb_id for p in first.picks.picks] == [10]
    assert [p.candidate.tmdb_id for p in second.picks.picks] == [11]


def test_picks_for_a_user_without_seerr_are_not_reserved(conn):
    watch(conn, 7, 1, 2)
    watch(conn, 8, 1, 2)
    tmdb = FakeTmdb({("movie", 1): [rec(10), rec(11)]})
    included = [
        user(7, picks_per_run=1, seerr_user=None),
        user(8, picks_per_run=1),
    ]

    first, second = recommend(conn, CONFIG, tmdb, FakeSeerr(), included, NOW)

    assert [p.candidate.tmdb_id for p in first.picks.picks] == [10]
    assert [p.candidate.tmdb_id for p in second.picks.picks] == [10]


def test_tmdb_error_skips_the_user_and_keeps_the_cache(conn, tmp_path):
    watch(conn, 7, 1, 2)
    watch(conn, 8, 3, 4)
    cache = TmdbCache(conn)

    class FailingTmdb(FakeTmdb):
        def recommendations(self, media_type, tmdb_id):
            if tmdb_id == 2:
                raise TmdbError("rate limited", 429)
            cache.put(f"/{media_type}/{tmdb_id}/recommendations", {"results": []})
            return super().recommendations(media_type, tmdb_id)

    tmdb = FailingTmdb({("movie", 3): [rec(10)]})
    failed, picked = recommend(conn, CONFIG, tmdb, FakeSeerr(), [user(7), user(8)], NOW)

    assert failed.picks is None
    assert failed.skipped == "TMDB error: rate limited"
    assert [p.candidate.tmdb_id for p in picked.picks.picks] == [10]
    other = db.connect(tmp_path / "test.db")
    assert other.execute("SELECT key FROM tmdb_cache").fetchall()[0][0] == (
        "/movie/1/recommendations"
    )
