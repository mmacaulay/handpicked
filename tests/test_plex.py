from dataclasses import replace

import httpx
import pytest
import respx

from handpicked.plex import (
    Item,
    Plex,
    PlexError,
    Promotion,
    Section,
    Server,
    Share,
    delivery_problem,
)

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
            '<User id="1" filterMovies="" filterTelevision="">'
            '<Server machineIdentifier="abc"/></User>'
            '<User id="2" filterMovies="contentRating!%3DR"'
            ' filterTelevision="label!%3Dx"><Server machineIdentifier="abc"/></User>'
            # A friend with no access to this server
            '<User id="3"><Server machineIdentifier="other"/></User>'
        )
    )

    assert plex.shares("abc") == [
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
                    {"key": "1", "type": "movie", "title": "L1"},
                    {"key": "2", "type": "show", "title": "L2"},
                    {"key": "3", "type": "artist", "title": "L3"},
                    {"key": "4", "type": "movie", "title": "L4"},
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
        json={
            "MediaContainer": {
                "Directory": [{"key": "1", "type": "movie", "title": "L1"}]
            }
        }
    )
    respx.get(f"{URL}/library/sections/1/all").respond(json={"MediaContainer": {}})

    assert plex.library_items() == {}


@respx.mock
def test_a_refused_request_raises_without_the_token(plex):
    respx.get("https://plex.tv/api/users").respond(401)

    with pytest.raises(PlexError) as raised:
        plex.shares("abc")

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
    respx.get("https://plex.tv/api/users").mock(
        return_value=xml('<User><Server machineIdentifier="abc"/></User>')
    )
    respx.get("https://plex.tv/api/home/users").mock(return_value=xml('<User id="x"/>'))

    with pytest.raises(PlexError, match="^plex.tv/users: unexpected response$"):
        plex.shares("abc")
    with pytest.raises(PlexError, match="unexpected response"):
        plex.age_presets()


@respx.mock
def test_a_library_reply_of_the_wrong_shape_raises_plex_error(plex):
    respx.get(f"{URL}/library/sections").respond(
        json={
            "MediaContainer": {
                "Directory": [{"key": "1", "type": "movie", "title": "L1"}]
            }
        }
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


MOVIES = Section("1", "movie", "Movies")


@respx.mock
def test_account_id_is_the_token_owners(plex):
    route = respx.get("https://plex.tv/api/v2/user").respond(json={"id": 7})

    assert plex.account_id() == 7
    assert route.calls.last.request.headers["Accept"] == "application/json"


@respx.mock
def test_sections_are_the_movie_and_tv_libraries(plex):
    respx.get(f"{URL}/library/sections").respond(
        json={
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "type": "movie", "title": "Movies"},
                    {"key": "2", "type": "show", "title": "TV"},
                    {"key": "3", "type": "photo", "title": "Photos"},
                ]
            }
        }
    )

    assert plex.sections() == [MOVIES, Section("2", "tv", "TV")]


@respx.mock
def test_collections_can_be_found_by_label(plex):
    route = respx.get(f"{URL}/library/sections/1/collections").respond(
        json={"MediaContainer": {"Metadata": [{"ratingKey": "9"}]}}
    )

    assert plex.collections(MOVIES, "handpicked-2") == ["9"]
    assert route.calls.last.request.url.params["label"] == "handpicked-2"
    assert plex.collections(MOVIES) == ["9"]
    assert "label" not in route.calls.last.request.url.params


@respx.mock
def test_create_collection_makes_it_empty_then_labels_it(plex):
    create = respx.post(f"{URL}/library/collections").respond(
        json={"MediaContainer": {"Metadata": [{"ratingKey": "9"}]}}
    )
    label = respx.put(f"{URL}/library/sections/2/all").respond(200)

    assert (
        plex.create_collection(Section("2", "tv", "TV"), "Picked for you", "x") == "9"
    )
    params = create.calls.last.request.url.params
    assert dict(params) == {
        "type": "2",
        "title": "Picked for you",
        "smart": "0",
        "sectionId": "2",
    }
    assert dict(label.calls.last.request.url.params) == {
        "type": "18",
        "id": "9",
        "label[0].tag.tag": "x",
        "label.locked": "1",
    }


@respx.mock
def test_a_collection_that_cant_be_labelled_is_deleted(plex):
    respx.post(f"{URL}/library/collections").respond(
        json={"MediaContainer": {"Metadata": [{"ratingKey": "9"}]}}
    )
    respx.put(f"{URL}/library/sections/2/all").respond(500)
    delete = respx.delete(f"{URL}/library/collections/9").respond(200)

    with pytest.raises(PlexError, match="HTTP 500"):
        plex.create_collection(Section("2", "tv", "TV"), "Picked for you", "x")
    assert delete.called


@respx.mock
def test_a_failed_cleanup_keeps_the_label_error(plex):
    respx.post(f"{URL}/library/collections").respond(
        json={"MediaContainer": {"Metadata": [{"ratingKey": "9"}]}}
    )
    respx.put(f"{URL}/library/sections/2/all").respond(500)
    respx.delete(f"{URL}/library/collections/9").respond(404)

    with pytest.raises(PlexError, match="sections/2/all.*HTTP 500"):
        plex.create_collection(Section("2", "tv", "TV"), "Picked for you", "x")


@respx.mock
def test_a_home_row_name_too_short_raises_plex_error(plex):
    respx.get(f"{URL}/hubs/promoted").respond(
        json={"MediaContainer": {"Hub": [{"hubIdentifier": "custom.collection.1"}]}}
    )

    with pytest.raises(PlexError, match="unexpected response"):
        plex.home_collections(MOVIES)


@respx.mock
def test_a_collection_is_deleted_by_key(plex):
    route = respx.delete(f"{URL}/library/collections/9").respond(200)

    plex.delete_collection("9")

    assert route.called


@respx.mock
def test_collection_items_are_rating_keys(plex):
    respx.get(f"{URL}/library/collections/9/children").respond(
        json={"MediaContainer": {"Metadata": [{"ratingKey": "5"}, {"ratingKey": "6"}]}}
    )
    respx.get(f"{URL}/library/collections/8/children").respond(
        json={"MediaContainer": {"size": 0}}
    )

    assert plex.collection_items("9") == {"5", "6"}
    assert plex.collection_items("8") == set()


@respx.mock
def test_items_are_added_by_server_uri_and_removed_by_key(plex):
    add = respx.put(f"{URL}/library/collections/9/items").respond(200)
    remove = respx.delete(f"{URL}/library/collections/9/items/5").respond(200)

    plex.add_to_collection("abc", "9", "5")
    plex.remove_from_collection("9", "5")

    assert add.calls.last.request.url.params["uri"] == (
        "server://abc/com.plexapp.plugins.library/library/metadata/5"
    )
    assert remove.called


@respx.mock
def test_promotions_by_hub(plex):
    respx.get(f"{URL}/hubs/sections/1/manage").respond(
        json={
            "MediaContainer": {
                "Hub": [
                    {
                        "identifier": "custom.collection.1.9",
                        "promotedToOwnHome": False,
                        "promotedToSharedHome": True,
                    },
                    {"identifier": "movie.recentlyadded"},
                ]
            }
        }
    )

    assert plex.promotions(MOVIES) == {
        "custom.collection.1.9": Promotion(own_home=False, shared_home=True),
        "movie.recentlyadded": Promotion(own_home=False, shared_home=False),
    }


@respx.mock
def test_promote_never_reaches_recommended(plex):
    route = respx.post(f"{URL}/hubs/sections/1/manage").respond(200)

    plex.promote(MOVIES, "9", Promotion(own_home=False, shared_home=True))

    assert dict(route.calls.last.request.url.params) == {
        "metadataItemId": "9",
        "promotedToRecommended": "0",
        "promotedToOwnHome": "0",
        "promotedToSharedHome": "1",
    }


@respx.mock
def test_demote_removes_the_managed_row(plex):
    route = respx.delete(f"{URL}/hubs/sections/1/manage/custom.collection.1.9")

    plex.demote(MOVIES, "9")

    assert route.called


@respx.mock
def test_home_collections_are_read_from_the_promoted_hubs(plex):
    # As the household's server named them.
    hubs = ["home.continue", "movie.recentlyadded.1", "custom.collection.1.9.9"]
    route = respx.get(f"{URL}/hubs/promoted").respond(
        json={"MediaContainer": {"Hub": [{"hubIdentifier": h} for h in hubs]}}
    )

    assert plex.as_member("m").home_collections(MOVIES) == {"9"}
    request = route.calls.last.request
    assert request.url.params["contentDirectoryID"] == "1"
    assert request.headers["X-Plex-Token"] == "m"


@respx.mock
def test_set_filters_sends_both_with_the_admin_token(plex):
    route = respx.put("https://plex.tv/api/users/2").respond(200)

    plex.set_filters(2, "contentRating!=ca%2F18%2B&label!=handpicked-3", "")

    request = route.calls.last.request
    assert dict(request.url.params) == {
        "filterMovies": "contentRating!=ca%2F18%2B&label!=handpicked-3",
        "filterTelevision": "",
    }
    assert request.headers["X-Plex-Token"] == TOKEN
    assert TOKEN not in str(request.url)


@respx.mock
def test_a_refused_write_raises_plex_error(plex):
    respx.put("https://plex.tv/api/users/2").respond(422)
    respx.post(f"{URL}/hubs/sections/1/manage").respond(400)

    with pytest.raises(PlexError, match="^plex.tv/users/2: request failed .HTTP 422.$"):
        plex.set_filters(2, "", "")
    with pytest.raises(PlexError, match="manage: request failed .HTTP 400.$"):
        plex.promote(MOVIES, "9", Promotion(own_home=True, shared_home=False))
