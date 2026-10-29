import time

import httpx
import pytest
import respx

from handpicked import db
from handpicked.tmdb import CACHE_TTL, Title, Tmdb, TmdbCache, TmdbError

URL = "https://api.themoviedb.org/3"

MOVIE = {
    "id": 603,
    "title": "The Matrix",
    "release_date": "1999-03-31",
    "vote_count": 26000,
    "vote_average": 8.2,
    "poster_path": "/matrix.jpg",
    "release_dates": {
        "results": [
            {"iso_3166_1": "CA", "release_dates": [{"type": 3, "certification": "14A"}]}
        ]
    },
}

SHOW = {
    "id": 95396,
    "name": "Severance",
    "first_air_date": "2022-02-17",
    "vote_count": 2000,
    "vote_average": 8.4,
    "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
}


@pytest.fixture
def tmdb():
    return Tmdb("token")


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


@respx.mock
def test_movie_details(tmdb):
    route = respx.get(f"{URL}/movie/603").respond(json=MOVIE)

    assert tmdb.details("movie", 603) == Title(
        tmdb_id=603,
        media_type="movie",
        title="The Matrix",
        year=1999,
        vote_count=26000,
        vote_average=8.2,
        certification="14A",
        age=14,
        poster_path="/matrix.jpg",
    )
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer token"
    assert request.url.params["append_to_response"] == "release_dates"


@respx.mock
def test_tv_details(tmdb):
    route = respx.get(f"{URL}/tv/95396").respond(json=SHOW)

    title = tmdb.details("tv", 95396)

    assert (title.title, title.year) == ("Severance", 2022)
    assert (title.certification, title.age) == ("TV-MA", 17)
    assert route.calls[0].request.url.params["append_to_response"] == "content_ratings"


@respx.mock
def test_poster_url(tmdb):
    respx.get(f"{URL}/movie/603").respond(json=MOVIE)
    respx.get(f"{URL}/tv/95396").respond(json=SHOW | {"poster_path": None})

    movie = tmdb.details("movie", 603)
    assert movie.poster_url == "https://image.tmdb.org/t/p/w185/matrix.jpg"
    assert tmdb.details("tv", 95396).poster_url is None


@respx.mock
def test_details_without_date(tmdb):
    respx.get(f"{URL}/movie/603").respond(json=MOVIE | {"release_date": ""})

    assert tmdb.details("movie", 603).year is None


@respx.mock
def test_details_missing_title_is_none(tmdb):
    respx.get(f"{URL}/movie/603").respond(404, json={"status_code": 34})

    assert tmdb.details("movie", 603) is None


@respx.mock
def test_recommendations(tmdb):
    respx.get(f"{URL}/tv/95396/recommendations").respond(
        json={"page": 1, "results": [{"id": 1}, {"id": 2}]}
    )

    assert tmdb.recommendations("tv", 95396) == [{"id": 1}, {"id": 2}]


@respx.mock
def test_recommendations_missing_title_is_empty(tmdb):
    respx.get(f"{URL}/movie/603/recommendations").respond(404, json={})

    assert tmdb.recommendations("movie", 603) == []


@respx.mock
def test_rate_limit_honours_retry_after(tmdb, sleeps):
    respx.get(f"{URL}/movie/603").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, json={}),
            httpx.Response(200, json=MOVIE),
        ]
    )

    assert tmdb.details("movie", 603).title == "The Matrix"
    assert sleeps == [3.0]


@respx.mock
def test_rate_limit_backs_off_then_gives_up(tmdb, sleeps):
    route = respx.get(f"{URL}/movie/603").respond(
        429, json={"status_message": "Too many requests"}
    )

    with pytest.raises(TmdbError, match="Too many requests") as error:
        tmdb.details("movie", 603)
    assert error.value.status_code == 429
    assert route.call_count == Tmdb.MAX_ATTEMPTS
    assert sleeps == [1, 2, 4]


@respx.mock
def test_error_response_raises(tmdb):
    respx.get(f"{URL}/movie/603").respond(
        401, json={"status_code": 7, "status_message": "Invalid API key"}
    )

    with pytest.raises(TmdbError, match="Invalid API key"):
        tmdb.details("movie", 603)


@respx.mock
def test_non_json_response_raises(tmdb):
    respx.get(f"{URL}/movie/603").respond(502, text="Bad Gateway")

    with pytest.raises(TmdbError, match="HTTP 502"):
        tmdb.details("movie", 603)


@respx.mock
def test_connection_error_raises(tmdb):
    respx.get(f"{URL}/movie/603").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TmdbError, match="refused"):
        tmdb.details("movie", 603)


@respx.mock
def test_cache_hit_skips_request(conn):
    route = respx.get(f"{URL}/movie/603").respond(json=MOVIE)
    tmdb = Tmdb("token", cache=TmdbCache(conn))

    first = tmdb.details("movie", 603)
    second = tmdb.details("movie", 603)

    assert first == second
    assert route.call_count == 1


@respx.mock
def test_expired_cache_refetches(conn):
    route = respx.get(f"{URL}/movie/603").respond(json=MOVIE)
    tmdb = Tmdb("token", cache=TmdbCache(conn))

    tmdb.details("movie", 603)
    conn.execute("UPDATE tmdb_cache SET fetched_at = fetched_at - ?", (CACHE_TTL,))
    tmdb.details("movie", 603)

    assert route.call_count == 2
    assert conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0] == 1


@respx.mock
def test_missing_title_is_not_cached(conn):
    route = respx.get(f"{URL}/movie/603").respond(404, json={})
    tmdb = Tmdb("token", cache=TmdbCache(conn))

    tmdb.details("movie", 603)
    tmdb.details("movie", 603)

    assert route.call_count == 2


@respx.mock
def test_without_cache_always_fetches(tmdb):
    route = respx.get(f"{URL}/movie/603").respond(json=MOVIE)

    tmdb.details("movie", 603)
    tmdb.details("movie", 603)

    assert route.call_count == 2
