import json

import httpx
import pytest
import respx
from test_picks import FakeTmdb, rec, title
from test_pipeline import CONFIG, NOW, FakeSeerr, watch

from handpicked import db
from handpicked.picks import Candidate, Pick
from handpicked.pipeline import recommend
from handpicked.seeds import Seed
from handpicked.seerr import Seerr, SeerrError, SeerrUser
from handpicked.suggestions import (
    REQUEST_USER_EMAIL,
    create_request_user,
    find_request_user,
    request,
    request_user_problem,
    sync,
)
from handpicked.users import User

URL = "http://seerr:5055/api/v1"
MEMBER = SeerrUser(id=3, plex_id=None, display_name="kid", permissions=32)


def member(user_id=7, seerr_user=MEMBER):
    return User(user_id, f"user{user_id}", True, None, None, "name", seerr_user, None)


def pick(tmdb_id, media_type="movie"):
    seeds = [Seed("movie", 1, "Seed 1", 0.9), Seed("tv", 2, "Seed 2", -0.5)]
    candidate = Candidate(media_type, tmdb_id, 8.0, seeds)
    return Pick(candidate, title(tmdb_id, media_type))


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    with conn:
        conn.execute("INSERT INTO users (user_id, username) VALUES (7, 'user7')")
    yield conn
    conn.close()


@pytest.fixture
def seerr():
    return Seerr("http://seerr:5055", "key")


def reassign():
    return respx.put(url__regex=rf"{URL}/request/\d+").respond(json={})


def rows(conn):
    return conn.execute(
        "SELECT * FROM suggestions ORDER BY media_type, tmdb_id"
    ).fetchall()


@pytest.mark.parametrize(
    ("permissions", "problem"),
    [
        (32, None),  # REQUEST
        (8 | 32, None),  # and MANAGE_USERS
        (262144 | 524288, None),  # REQUEST_MOVIE and REQUEST_TV
        (262144, "needs the Request permission"),  # REQUEST_MOVIE only
        (8, "needs the Request permission"),
        (32 | 128, "must not have Admin"),  # AUTO_APPROVE
        (2, "must not have Admin"),  # ADMIN
        (32 | 16, "must not have Admin"),  # MANAGE_REQUESTS
    ],
)
def test_request_user_permissions(permissions, problem):
    user = SeerrUser(5, None, "Handpicked", permissions, email=REQUEST_USER_EMAIL)

    found = request_user_problem(user)

    assert found is None if problem is None else problem in found


@respx.mock
def test_creating_the_request_user_sets_it_up(seerr):
    respx.post(f"{URL}/user").respond(201, json={"id": 9})
    permissions = respx.post(f"{URL}/user/9/settings/permissions").respond(json={})
    quota = respx.post(f"{URL}/user/9/settings/main").respond(json={})

    user = create_request_user(seerr)

    assert user == SeerrUser(9, None, "Handpicked", 32, email=REQUEST_USER_EMAIL)
    assert permissions.called and quota.called


@respx.mock
def test_a_half_created_request_user_is_deleted(seerr):
    respx.post(f"{URL}/user").respond(201, json={"id": 9})
    respx.post(f"{URL}/user/9/settings/permissions").respond(json={})
    respx.post(f"{URL}/user/9/settings/main").respond(500, json={"message": "boom"})
    delete = respx.delete(f"{URL}/user/9").respond(json={"id": 9})

    with pytest.raises(SeerrError, match="boom"):
        create_request_user(seerr)
    assert delete.called


@respx.mock
def test_a_failed_cleanup_says_the_user_is_left(seerr):
    respx.post(f"{URL}/user").respond(201, json={"id": 9})
    respx.post(f"{URL}/user/9/settings/permissions").respond(500, json={})
    respx.delete(f"{URL}/user/9").respond(500, json={"message": "nope"})

    with pytest.raises(SeerrError, match="Seerr user 9 is left half set up: .*nope"):
        create_request_user(seerr)


def test_request_user_is_found_by_email():
    handpicked = SeerrUser(5, None, "Bot", 32, email=REQUEST_USER_EMAIL.upper())
    named = SeerrUser(6, None, "Handpicked", 32, email="admin@example.com")

    assert find_request_user([MEMBER, named, handpicked]) is handpicked
    assert find_request_user([MEMBER, named]) is None


@respx.mock
def test_request_records_each_created_request(conn, seerr):
    respx.post(f"{URL}/request").mock(
        side_effect=[
            httpx.Response(201, json={"id": 41}),
            httpx.Response(201, json={"id": 42, "seasons": [{"seasonNumber": 1}]}),
        ]
    )
    route = reassign()

    results = request(conn, seerr, 5, member(), [pick(10), pick(11, "tv")], NOW)

    assert [r.request_id for r in results] == [41, 42]
    assert [json.loads(c.request.content)["userId"] for c in route.calls] == [3, 3]
    movie, tv = rows(conn)
    assert (movie["user_id"], movie["media_type"], movie["tmdb_id"]) == (7, "movie", 10)
    assert (tv["media_type"], tv["tmdb_id"], tv["seerr_request_id"]) == ("tv", 11, 42)
    assert movie["status"] == "requested"
    assert movie["seerr_request_id"] == 41
    assert (movie["proposed_at"], movie["requested_at"]) == (NOW, NOW)


@respx.mock
def test_stored_seeds_sum_to_the_score(conn, seerr):
    respx.post(f"{URL}/request").respond(201, json={"id": 41})
    reassign()
    picked = pick(10)

    request(conn, seerr, 5, member(), [picked], NOW)

    (row,) = rows(conn)
    parts = json.loads(row["seeds"])
    assert parts == [
        {"media_type": "movie", "tmdb_id": 1, "title": "Seed 1", "weight": 0.9},
        {"media_type": "tv", "tmdb_id": 2, "title": "Seed 2", "weight": -0.5},
        {"tmdb_rating": 8.0, "weight": picked.candidate.rating_prior},
    ]
    assert sum(p["weight"] for p in parts) == pytest.approx(row["score"])
    assert row["score"] == pytest.approx(picked.candidate.score)


@respx.mock
def test_refused_requests_are_reported_and_not_recorded(conn, seerr):
    respx.post(f"{URL}/request").mock(
        side_effect=[
            httpx.Response(403, json={"message": "Movie Quota exceeded."}),
            httpx.Response(201, json={"id": 42}),
        ]
    )
    reassign()

    refused, created = request(conn, seerr, 5, member(), [pick(10), pick(11)], NOW)

    assert refused.request_id is None
    assert "Movie Quota exceeded." in refused.error
    assert created.request_id == 42
    assert [row["tmdb_id"] for row in rows(conn)] == [11]


@respx.mock
def test_requested_titles_are_not_picked_again(conn, seerr):
    respx.post(f"{URL}/request").respond(201, json={"id": 41})
    reassign()
    watch(conn, 7, 1, 2)
    tmdb = FakeTmdb({("movie", 1): [rec(i) for i in range(10, 16)]})

    (first,) = recommend(conn, CONFIG, tmdb, FakeSeerr(), [member()], NOW)
    request(conn, seerr, 5, member(), first.picks.picks, NOW)
    (second,) = recommend(conn, CONFIG, tmdb, FakeSeerr(), [member()], NOW)

    first_ids = [p.candidate.tmdb_id for p in first.picks.picks]
    second_ids = [p.candidate.tmdb_id for p in second.picks.picks]
    assert first_ids == [10, 11, 12]
    assert second_ids == [13, 14, 15]


def suggest(conn, tmdb_id, request_id, status="requested"):
    with conn:
        conn.execute(
            """
            INSERT INTO suggestions (
                user_id, media_type, tmdb_id, score, seeds, status,
                seerr_request_id, proposed_at, requested_at
            ) VALUES (7, 'movie', ?, 1, '[]', ?, ?, 0, 0)
            """,
            (tmdb_id, status, request_id),
        )


def not_found(request_id):
    return respx.get(f"{URL}/request/{request_id}").respond(
        404, json={"message": "Request not found."}
    )


def request_list(statuses):
    return respx.get(f"{URL}/request").respond(
        json={
            "pageInfo": {"results": len(statuses)},
            "results": [{"id": i, "status": s} for i, s in statuses.items()],
        }
    )


@respx.mock
def test_sync_records_the_admins_review(conn, seerr):
    # Seerr status: pending, approved, declined, failed, completed, deleted
    request_list({1: 1, 2: 2, 3: 3, 4: 4, 5: 5})
    not_found(6)
    for request_id in range(1, 7):
        suggest(conn, 10 + request_id, request_id)

    changes = sync(conn, seerr, NOW)

    assert changes == {"approved": 3, "declined": 1, "deleted": 1}
    assert [
        (r["status"], r["approved_at"], r["declined_at"], r["deleted_at"])
        for r in rows(conn)
    ] == [
        ("requested", None, None, None),
        ("approved", NOW, None, None),
        ("declined", None, NOW, None),
        ("approved", NOW, None, None),
        ("approved", NOW, None, None),
        ("deleted", None, None, NOW),
    ]


@respx.mock
def test_listed_requests_are_not_looked_up(conn, seerr):
    request_list({1: 2})
    route = respx.get(f"{URL}/request/1")
    suggest(conn, 10, 1)

    assert sync(conn, seerr, NOW) == {"approved": 1}
    assert not route.called


@respx.mock
def test_request_missing_from_the_list_is_looked_up(conn, seerr):
    # e.g. a blocklisted title, or skipped by paging while the list changed
    request_list({})
    respx.get(f"{URL}/request/1").respond(json={"status": 1})
    respx.get(f"{URL}/request/2").respond(json={"status": 3})
    suggest(conn, 10, 1)
    suggest(conn, 11, 2)

    assert sync(conn, seerr, NOW) == {"declined": 1}
    assert [r["status"] for r in rows(conn)] == ["requested", "declined"]


@respx.mock
def test_failed_lookup_is_not_taken_for_deleted(conn, seerr):
    request_list({})
    respx.get(f"{URL}/request/1").respond(500, json={"message": "boom"})
    suggest(conn, 10, 1)

    with pytest.raises(SeerrError, match="boom"):
        sync(conn, seerr, NOW)
    assert rows(conn)[0]["status"] == "requested"


@respx.mock
def test_deleted_requests_are_not_checked_again(conn, seerr):
    request_list({})
    not_found(1)
    suggest(conn, 10, 1)
    sync(conn, seerr, NOW)
    respx.reset()

    assert sync(conn, seerr, NOW) == {}
    assert not respx.calls


@respx.mock
def test_sync_only_checks_open_requests(conn, seerr):
    route = respx.get(f"{URL}/request/1").respond(json={"status": 3})
    suggest(conn, 10, 1, status="approved")

    assert sync(conn, seerr, NOW) == {}
    assert not route.called
