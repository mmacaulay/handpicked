from dataclasses import replace

import httpx
import pytest
import respx

from handpicked.plex import Item, Plex, PlexError, Server, Share, delivery_problem

URL = "http://plex:32400"
TOKEN = "admin-token"


@pytest.fixture
def plex():
    return Plex("http://plex:32400/", TOKEN)


def xml(body):
    return httpx.Response(200, text=f"<MediaContainer>{body}</MediaContainer>")


@respx.mock
def test_server_reads_version_and_plex_pass(plex):
    route = respx.get(f"{URL}/").respond(
        json={
            "MediaContainer": {
                "version": "1.43.5.11079-b3b13e506",
                "machineIdentifier": "abc",
                "myPlexSubscription": True,
            }
        }
    )

    assert plex.server() == Server((1, 43, 5, 11079), "abc", True)
    request = route.calls.last.request
    assert request.headers["X-Plex-Token"] == TOKEN
    assert request.headers["Accept"] == "application/json"
    assert TOKEN not in str(request.url)


@respx.mock
def test_server_without_a_subscription(plex):
    respx.get(f"{URL}/").respond(
        json={"MediaContainer": {"version": "1.43.5.1", "machineIdentifier": "abc"}}
    )

    assert plex.server().plex_pass is False


@respx.mock
def test_shares_read_each_members_filters(plex):
    route = respx.get("https://plex.tv/api/users").mock(
        return_value=xml(
            '<User id="1" filterMovies="" filterTelevision=""><Server/></User>'
            '<User id="2" filterMovies="contentRating!%3DR"'
            ' filterTelevision="label!%3Dx"/>'
        )
    )

    assert plex.shares() == [
        Share(1, "", ""),
        Share(2, "contentRating!%3DR", "label!%3Dx"),
    ]
    assert route.calls.last.request.headers["X-Plex-Token"] == TOKEN


@respx.mock
def test_age_presets_are_none_when_unset(plex):
    respx.get("https://plex.tv/api/home/users").mock(
        return_value=xml(
            '<User id="1" restrictionProfile=""/>'
            '<User id="2" restrictionProfile="little_kid"/>'
            '<User id="3"/>'
        )
    )

    assert plex.age_presets() == {1: None, 2: "little_kid", 3: None}


@respx.mock
def test_member_tokens_by_plex_id(plex):
    respx.get("https://plex.tv/api/servers/abc/shared_servers").mock(
        return_value=xml(
            '<SharedServer userID="2" accessToken="t2"><Section/></SharedServer>'
            '<SharedServer userID="3" accessToken="t3"/>'
            '<SharedServer userID="4" accessToken=""/>'  # invited, not accepted
        )
    )

    assert plex.member_tokens("abc") == {2: "t2", 3: "t3"}


@respx.mock
def test_as_member_reads_with_the_members_token(plex):
    route = respx.get(f"{URL}/").respond(
        json={"MediaContainer": {"version": "1.43.5.1", "machineIdentifier": "abc"}}
    )

    plex.as_member("member-token").server()

    assert route.calls.last.request.headers["X-Plex-Token"] == "member-token"


def metadata(rating_key, *guids):
    return {"ratingKey": rating_key, "Guid": [{"id": g} for g in guids]}


@respx.mock
def test_library_items_map_tmdb_ids_in_movie_and_tv_libraries(plex):
    respx.get(f"{URL}/library/sections").respond(
        json={
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "type": "movie"},
                    {"key": "2", "type": "show"},
                    {"key": "3", "type": "artist"},
                    {"key": "4", "type": "movie"},
                ]
            }
        }
    )
    movies = respx.get(f"{URL}/library/sections/1/all").respond(
        json={
            "MediaContainer": {
                "Metadata": [
                    metadata("10", "imdb://tt1", "tmdb://100", "tvdb://5"),
                    metadata("11", "imdb://tt2"),  # unmatched: no TMDB id
                ]
            }
        }
    )
    respx.get(f"{URL}/library/sections/2/all").respond(
        json={"MediaContainer": {"Metadata": [metadata("20", "tmdb://100")]}}
    )
    music = respx.get(f"{URL}/library/sections/3/all")
    respx.get(f"{URL}/library/sections/4/all").respond(
        json={"MediaContainer": {"Metadata": [metadata("40", "tmdb://100")]}}
    )

    # Movie and TV ids overlap, so the media type is part of the key.
    assert plex.library_items() == {
        ("movie", 100): [Item("1", "10"), Item("4", "40")],
        ("tv", 100): [Item("2", "20")],
    }
    assert movies.calls.last.request.url.params["includeGuids"] == "1"
    assert not music.called


@respx.mock
def test_an_empty_library_has_no_items(plex):
    respx.get(f"{URL}/library/sections").respond(
        json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
    )
    respx.get(f"{URL}/library/sections/1/all").respond(json={"MediaContainer": {}})

    assert plex.library_items() == {}


@respx.mock
def test_a_refused_request_raises_without_the_token(plex):
    respx.get("https://plex.tv/api/users").respond(401)

    with pytest.raises(PlexError) as raised:
        plex.shares()

    assert raised.value.status_code == 401
    assert str(raised.value) == (
        "plex.tv/users: request failed (HTTP 401), check the Plex token"
    )


@respx.mock
def test_an_unreachable_server_raises_plex_error(plex):
    respx.get(f"{URL}/").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(PlexError, match="^http://plex:32400/: refused$"):
        plex.server()


@respx.mock
def test_a_response_that_isnt_json_or_xml_raises_plex_error(plex):
    respx.get(f"{URL}/").respond(200, text="<html>")
    respx.get("https://plex.tv/api/home/users").respond(200, text="not xml")

    with pytest.raises(PlexError, match="unexpected response"):
        plex.server()
    with pytest.raises(PlexError, match="unexpected response"):
        plex.age_presets()


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"foo": 1},
        {"MediaContainer": {"machineIdentifier": "abc"}},
        {"MediaContainer": {"version": "beta", "machineIdentifier": "abc"}},
        [],
    ],
)
def test_a_server_reply_of_the_wrong_shape_raises_plex_error(plex, body):
    respx.get(f"{URL}/").respond(json=body)

    with pytest.raises(PlexError, match="^http://plex:32400/: unexpected response$"):
        plex.server()


@respx.mock
def test_a_plex_tv_reply_of_the_wrong_shape_raises_plex_error(plex):
    respx.get("https://plex.tv/api/users").mock(return_value=xml("<User/>"))
    respx.get("https://plex.tv/api/home/users").mock(return_value=xml('<User id="x"/>'))

    with pytest.raises(PlexError, match="^plex.tv/users: unexpected response$"):
        plex.shares()
    with pytest.raises(PlexError, match="unexpected response"):
        plex.age_presets()


@respx.mock
def test_a_library_reply_of_the_wrong_shape_raises_plex_error(plex):
    respx.get(f"{URL}/library/sections").respond(
        json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
    )
    respx.get(f"{URL}/library/sections/1/all").respond(
        json={"MediaContainer": {"Metadata": [{"Guid": [{"id": "tmdb://1"}]}]}}
    )

    with pytest.raises(
        PlexError, match="^http://plex:32400/library/sections/1/all: unexpected"
    ):
        plex.library_items()


def test_the_default_timeout_is_longer_than_httpx_default():
    assert Plex(URL, TOKEN)._client.timeout.read == 30


@pytest.mark.parametrize(
    "version, ready",
    [
        ("1.43.2.10686-abc", False),
        ("1.43.2.10687-abc", True),
        ("1.43.10.1-abc", True),  # compared as numbers, not text
        ("1.42.99.99999", False),
        ("2.0.0.1", True),
    ],
)
def test_delivery_needs_a_recent_enough_server(plex, version, ready):
    with respx.mock:
        respx.get(f"{URL}/").respond(
            json={"MediaContainer": {"version": version, "machineIdentifier": "a"}}
        )
        server = plex.server()

    problem = delivery_problem(replace(server, plex_pass=True))

    assert (problem is None) is ready
    if not ready:
        assert problem.startswith(
            f"Plex Media Server {version.partition('-')[0]} is older than"
        )


def test_delivery_needs_plex_pass():
    problem = delivery_problem(Server((1, 43, 5, 1), "abc", False))

    assert problem.startswith("the Plex admin account has no Plex Pass")
