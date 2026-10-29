import pytest

from handpicked.picks import RATING_PRIOR_MAX, choose
from handpicked.seeds import History, Seed
from handpicked.tmdb import Title

SEED_A = Seed("movie", 1, "A", 1.0)
SEED_B = Seed("movie", 2, "B", 0.5)
ABANDONED = Seed("movie", 3, "C", -0.5)
SHOW = Seed("tv", 4, "D", 0.8)


def rec(tmdb_id, vote_average=8.0, vote_count=1000):
    return {"id": tmdb_id, "vote_average": vote_average, "vote_count": vote_count}


def title(tmdb_id, media_type="movie", certification="PG", age=8):
    return Title(
        tmdb_id, media_type, f"title {tmdb_id}", 2020, 1000, 8.0, certification, age
    )


class FakeTmdb:
    def __init__(self, recs, titles=None):
        self.recs = recs
        self.titles = titles or {}
        self.details_calls = []

    def recommendations(self, media_type, tmdb_id):
        return self.recs.get((media_type, tmdb_id), [])

    def details(self, media_type, tmdb_id):
        self.details_calls.append((media_type, tmdb_id))
        key = (media_type, tmdb_id)
        return self.titles[key] if key in self.titles else title(tmdb_id, media_type)


def pick(history, tmdb, exclude=frozenset(), count=3, max_age=None, min_vote_count=100):
    return choose(
        history,
        tmdb,
        set(exclude),
        count=count,
        max_age=max_age,
        min_vote_count=min_vote_count,
    )


def ids(result):
    return [(p.candidate.media_type, p.candidate.tmdb_id) for p in result.picks]


def test_score_sums_recommending_seeds_and_rating_prior():
    tmdb = FakeTmdb(
        {("movie", 1): [rec(10), rec(11, vote_average=10)], ("movie", 2): [rec(10)]}
    )

    result = pick(History(seeds=[SEED_A, SEED_B]), tmdb)

    assert ids(result) == [("movie", 10), ("movie", 11)]
    first, second = (p.candidate for p in result.picks)
    assert first.seeds == [SEED_A, SEED_B]
    assert first.score == pytest.approx(1.5 + 0.8 * RATING_PRIOR_MAX)
    assert second.score == pytest.approx(1.0 + RATING_PRIOR_MAX)


def test_abandoned_titles_only_penalize_existing_candidates():
    tmdb = FakeTmdb(
        {("movie", 1): [rec(10), rec(11)], ("movie", 3): [rec(10), rec(12)]}
    )

    result = pick(History(seeds=[SEED_A], abandoned=[ABANDONED]), tmdb)

    assert ids(result) == [("movie", 11), ("movie", 10)]
    penalized = result.picks[1].candidate
    assert penalized.seeds == [SEED_A, ABANDONED]
    assert penalized.score == pytest.approx(0.5 + 0.8 * RATING_PRIOR_MAX)


def test_candidates_cancelled_out_by_abandoned_titles_are_dropped():
    tmdb = FakeTmdb({("movie", 2): [rec(10)], ("movie", 3): [rec(10)]})

    result = pick(History(seeds=[SEED_B], abandoned=[ABANDONED]), tmdb)

    assert result.picks == []
    assert tmdb.details_calls == []


def test_excluded_titles_are_dropped():
    tmdb = FakeTmdb({("movie", 1): [rec(10), rec(11)]})

    result = pick(History(seeds=[SEED_A]), tmdb, exclude={("movie", 10)})

    assert ids(result) == [("movie", 11)]


def test_candidates_take_their_seeds_media_type():
    tmdb = FakeTmdb({("tv", 4): [rec(10)]})

    result = pick(History(seeds=[SHOW]), tmdb, exclude={("movie", 10)})

    assert ids(result) == [("tv", 10)]
    assert tmdb.details_calls == [("tv", 10)]


def test_titles_below_min_vote_count_are_dropped():
    tmdb = FakeTmdb({("movie", 1): [rec(10, vote_count=99), rec(11, vote_count=100)]})

    assert ids(pick(History(seeds=[SEED_A]), tmdb)) == [("movie", 11)]


def test_age_limit_drops_titles_rated_above_it_and_unrated_titles():
    tmdb = FakeTmdb(
        {("movie", 1): [rec(10), rec(11), rec(12)]},
        {
            ("movie", 10): title(10, certification="14A", age=14),
            ("movie", 12): title(12, certification=None, age=None),
        },
    )
    history = History(seeds=[SEED_A])

    assert ids(pick(history, tmdb, max_age=10)) == [("movie", 11)]
    assert ids(pick(history, tmdb)) == [("movie", 10), ("movie", 11), ("movie", 12)]


def test_details_are_fetched_only_until_enough_picks():
    tmdb = FakeTmdb(
        {("movie", 1): [rec(10), rec(11), rec(12), rec(13)]},
        {("movie", 10): None},  # gone from TMDB
    )

    result = pick(History(seeds=[SEED_A]), tmdb, count=2)

    assert ids(result) == [("movie", 11), ("movie", 12)]
    assert tmdb.details_calls == [("movie", 10), ("movie", 11), ("movie", 12)]
    assert (result.candidates, result.checked) == (4, 2)


def test_rating_coverage_counts_checked_titles():
    tmdb = FakeTmdb(
        {("movie", 1): [rec(10), rec(11)]},
        {("movie", 10): title(10, certification=None, age=None)},
    )

    result = pick(History(seeds=[SEED_A]), tmdb, max_age=10)

    assert (result.checked, result.rated) == (2, 1)
