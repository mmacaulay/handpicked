import json

import httpx
import pytest
import respx

from handpicked.seerr import Seerr, SeerrError, SeerrUser

URL = "http://seerr:5055/api/v1"


def page(results, total=None):
    total = len(results) if total is None else total
    return {"pageInfo": {"results": total}, "results": results}


def media(tmdb_id, status=1, status4k=1, media_type="movie"):
    return {
        "tmdbId": tmdb_id,
        "mediaType": media_type,
        "status": status,
        "status4k": status4k,
    }


@pytest.fixture
def seerr():
    return Seerr("http://seerr:5055/", "key")


@respx.mock
def test_pages_until_total_reached(seerr, monkeypatch):
    monkeypatch.setattr(Seerr, "PAGE_SIZE", 2)
    route = respx.get(f"{URL}/blocklist").mock(
        side_effect=[
            httpx.Response(200, json=page([media(1), media(2)], total=3)),
            httpx.Response(200, json=page([media(3)], total=3)),
        ]
    )

    assert seerr.blocklisted() == {("movie", 1), ("movie", 2), ("movie", 3)}
    first, second = (call.request for call in route.calls)
    assert first.headers["X-Api-Key"] == "key"
    assert "X-API-User" not in first.headers
    assert first.url.params["take"] == "2"
    assert first.url.params["skip"] == "0"
    assert second.url.params["skip"] == "2"


@respx.mock
def test_stops_on_empty_page(seerr):
    # The total can shrink mid-run, e.g. a request deleted while paging.
    route = respx.get(f"{URL}/blocklist").respond(json=page([], total=5))

    assert seerr.blocklisted() == set()
    assert route.call_count == 1


@respx.mock
def test_users(seerr):
    respx.get(f"{URL}/user").respond(
        json=page(
            [
                {"id": 1, "plexId": 111, "displayName": "matt", "permissions": 2},
                {"id": 7, "plexId": None, "displayName": "Kid", "permissions": 32},
            ]
        )
    )

    assert seerr.users() == [
        SeerrUser(id=1, plex_id=111, display_name="matt", permissions=2),
        SeerrUser(id=7, plex_id=None, display_name="Kid", permissions=32),
    ]


@pytest.mark.parametrize(
    ("permissions", "auto_approve"),
    [
        (2, True),  # ADMIN
        (16, True),  # MANAGE_REQUESTS
        (128, True),  # AUTO_APPROVE
        (256 | 32, True),  # AUTO_APPROVE_MOVIE
        (512 | 32, True),  # AUTO_APPROVE_TV
        (32, False),  # REQUEST
        (32768 | 32, False),  # AUTO_APPROVE_4K only affects 4K requests
        (0, False),
    ],
)
def test_auto_approve(permissions, auto_approve):
    user = SeerrUser(id=1, plex_id=None, display_name="x", permissions=permissions)
    assert user.auto_approve is auto_approve


@respx.mock
def test_owned_status_mapping(seerr):
    respx.get(f"{URL}/media").respond(
        json=page(
            [
                media(1, status=1),  # unknown
                media(2, status=2),  # pending: covered by requests
                media(3, status=3),  # processing
                media(4, status=4),  # partially available
                media(5, status=5),  # available
                media(6, status=6),  # blocklisted: covered by the blocklist
                media(7, status=7),  # deleted
                media(8, status=1, status4k=5),  # 4K copy only
                media(5, status=5, media_type="tv"),  # same id, other type
            ]
        )
    )

    assert seerr.owned() == {
        ("movie", 3),
        ("movie", 4),
        ("movie", 5),
        ("movie", 8),
        ("tv", 5),
    }


@respx.mock
def test_requested_includes_every_status(seerr):
    route = respx.get(f"{URL}/request").respond(
        json=page(
            [
                {"status": status, "media": media(tmdb_id, media_type="tv")}
                for tmdb_id, status in [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]
            ]
        )
    )

    assert seerr.requested() == {("tv", n) for n in range(1, 6)}
    assert route.calls[0].request.url.params["filter"] == "all"


@respx.mock
def test_request_ids_include_every_status(seerr):
    route = respx.get(f"{URL}/request").respond(
        json=page([{"id": 40 + status, "status": status} for status in range(1, 6)])
    )

    assert seerr.request_ids() == {41, 42, 43, 44, 45}
    assert route.calls[0].request.url.params["filter"] == "all"


@respx.mock
def test_error_response_raises(seerr):
    respx.get(f"{URL}/user").respond(403, json={"message": "Unauthorized"})

    with pytest.raises(SeerrError, match="Unauthorized") as error:
        seerr.users()
    assert error.value.status_code == 403


@respx.mock
def test_non_json_response_raises(seerr):
    respx.get(f"{URL}/user").respond(502, text="Bad Gateway")

    with pytest.raises(SeerrError, match="HTTP 502"):
        seerr.users()


@respx.mock
def test_connection_error_raises(seerr):
    respx.get(f"{URL}/user").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(SeerrError, match="refused"):
        seerr.users()


@respx.mock
def test_create_request_then_reassign_it(seerr):
    post = respx.post(f"{URL}/request").respond(201, json={"id": 42})
    put = respx.put(f"{URL}/request/42").respond(json={"id": 42})

    assert seerr.create_request(5, 3, "movie", 603) == 42
    created, reassigned = post.calls[0].request, put.calls[0].request
    assert created.headers["X-Api-Key"] == "key"
    assert created.headers["X-API-User"] == "5"
    assert json.loads(created.content) == {"mediaType": "movie", "mediaId": 603}
    assert reassigned.headers["X-Api-Key"] == "key"
    assert "X-API-User" not in reassigned.headers
    assert json.loads(reassigned.content) == {"mediaType": "movie", "userId": 3}


@respx.mock
def test_tv_request_asks_for_all_seasons_and_keeps_them(seerr):
    post = respx.post(f"{URL}/request").respond(
        201, json={"id": 42, "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}]}
    )
    put = respx.put(f"{URL}/request/42").respond(json={"id": 42})

    seerr.create_request(5, 3, "tv", 1399)
    assert json.loads(post.calls[0].request.content)["seasons"] == "all"
    assert json.loads(put.calls[0].request.content)["seasons"] == [1, 2]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (202, "No seasons available to request"),
        (403, "You do not have permission to make movie requests."),
        (409, "Request for this media already exists."),
    ],
)
@respx.mock
def test_refused_request_raises_with_seerr_message(seerr, status, message):
    respx.post(f"{URL}/request").respond(status, json={"message": message})
    put = respx.put(url__regex=r".*")

    with pytest.raises(SeerrError, match=message) as error:
        seerr.create_request(5, 3, "tv", 1399)
    assert error.value.status_code == status
    assert not put.called


@respx.mock
def test_refused_reassign_deletes_the_request(seerr):
    respx.post(f"{URL}/request").respond(201, json={"id": 42})
    respx.put(f"{URL}/request/42").respond(
        403, json={"message": "Movie Quota exceeded."}
    )
    delete = respx.delete(f"{URL}/request/42").respond(204)

    with pytest.raises(SeerrError, match="Movie Quota exceeded.") as error:
        seerr.create_request(5, 3, "movie", 603)
    assert error.value.status_code == 403
    assert delete.called


@respx.mock
def test_failed_cleanup_says_the_request_is_left(seerr):
    respx.post(f"{URL}/request").respond(201, json={"id": 42})
    respx.put(f"{URL}/request/42").respond(
        403, json={"message": "Movie Quota exceeded."}
    )
    respx.delete(f"{URL}/request/42").respond(500, json={"message": "boom"})

    with pytest.raises(
        SeerrError,
        match="Movie Quota exceeded.; /request/42 is still in Seerr as user 5's",
    ):
        seerr.create_request(5, 3, "movie", 603)


@respx.mock
def test_request_status(seerr):
    route = respx.get(f"{URL}/request/42").respond(json={"id": 42, "status": 3})

    assert seerr.request_status(42) == 3
    assert "X-API-User" not in route.calls[0].request.headers


@respx.mock
def test_deleted_request_has_no_status(seerr):
    respx.get(f"{URL}/request/42").respond(404, json={"message": "Request not found."})

    assert seerr.request_status(42) is None


@respx.mock
def test_request_status_raises_other_errors(seerr):
    respx.get(f"{URL}/request/42").respond(500, json={"message": "boom"})

    with pytest.raises(SeerrError, match="boom"):
        seerr.request_status(42)
