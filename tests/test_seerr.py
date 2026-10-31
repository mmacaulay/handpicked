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
                {
                    "id": 7,
                    "plexId": None,
                    "displayName": "Kid",
                    "permissions": 32,
                    "email": "kid@example.com",
                },
            ]
        )
    )

    assert seerr.users() == [
        SeerrUser(id=1, display_name="matt"),
        SeerrUser(7, "Kid", "kid@example.com"),
    ]


@respx.mock
def test_create_set_up_and_delete_a_user(seerr):
    create = respx.post(f"{URL}/user").respond(201, json={"id": 9})
    permissions = respx.post(f"{URL}/user/9/settings/permissions").respond(
        json={"permissions": 32}
    )
    quota = respx.post(f"{URL}/user/9/settings/main").respond(json={})
    delete = respx.delete(f"{URL}/user/9").respond(json={"id": 9})

    assert seerr.create_user("bot@example.com", "Bot", "secret") == 9
    seerr.set_permissions(9, 32)
    seerr.remove_quota(9, "Bot", "bot@example.com")
    seerr.delete_user(9)

    assert json.loads(create.calls.last.request.content) == {
        "email": "bot@example.com",
        "username": "Bot",
        "password": "secret",
    }
    assert json.loads(permissions.calls.last.request.content) == {"permissions": 32}
    assert json.loads(quota.calls.last.request.content) == {
        "username": "Bot",
        "email": "bot@example.com",
        "movieQuotaLimit": 0,
        "tvQuotaLimit": 0,
    }
    assert delete.called


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
def test_request_statuses_include_every_status(seerr):
    route = respx.get(f"{URL}/request").respond(
        json=page([{"id": 40 + status, "status": status} for status in range(1, 6)])
    )

    assert seerr.request_statuses() == {41: 1, 42: 2, 43: 3, 44: 4, 45: 5}
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
def test_create_request_as_a_user(seerr):
    post = respx.post(f"{URL}/request").respond(201, json={"id": 42})
    other = respx.route(method__in=["PUT", "DELETE"])

    assert seerr.create_request(5, "movie", 603) == 42
    created = post.calls[0].request
    assert created.headers["X-Api-Key"] == "key"
    assert created.headers["X-API-User"] == "5"
    assert json.loads(created.content) == {"mediaType": "movie", "mediaId": 603}
    assert not other.called


@respx.mock
def test_tv_request_asks_for_all_seasons(seerr):
    post = respx.post(f"{URL}/request").respond(201, json={"id": 42})

    seerr.create_request(5, "tv", 1399)
    assert json.loads(post.calls[0].request.content)["seasons"] == "all"


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

    with pytest.raises(SeerrError, match=message) as error:
        seerr.create_request(5, "tv", 1399)
    assert error.value.status_code == status


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
