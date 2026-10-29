import httpx
import pytest
import respx

from handpicked.tautulli import Tautulli, TautulliError

URL = "http://tautulli:8181/api/v2"


def envelope(data, result="success", message=None):
    return {"response": {"result": result, "message": message, "data": data}}


@pytest.fixture
def tautulli():
    return Tautulli("http://tautulli:8181/", "key")


@respx.mock
def test_history_pages_until_short_page(tautulli, monkeypatch):
    monkeypatch.setattr(Tautulli, "PAGE_SIZE", 2)
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=envelope({"data": [{"id": 1}, {"id": 2}]})),
            httpx.Response(200, json=envelope({"data": [{"id": 3}]})),
        ]
    )

    assert [row["id"] for row in tautulli.history()] == [1, 2, 3]
    first, second = (call.request.url.params for call in route.calls)
    assert first["cmd"] == "get_history"
    assert first["apikey"] == "key"
    assert first["grouping"] == "0"
    assert first["include_activity"] == "0"
    assert first["order_dir"] == "asc"
    assert first["start"] == "0"
    assert second["start"] == "2"
    assert "after" not in first


@respx.mock
def test_history_after(tautulli):
    route = respx.get(URL).respond(json=envelope({"data": []}))

    list(tautulli.history(after="2026-10-01"))

    assert route.calls[0].request.url.params["after"] == "2026-10-01"


@respx.mock
def test_error_envelope_raises(tautulli):
    respx.get(URL).respond(401, json=envelope({}, "error", "Invalid apikey"))

    with pytest.raises(TautulliError, match="Invalid apikey"):
        list(tautulli.history())


@respx.mock
def test_non_json_response_raises(tautulli):
    respx.get(URL).respond(502, text="Bad Gateway")

    with pytest.raises(TautulliError, match="HTTP 502"):
        list(tautulli.history())


@respx.mock
def test_connection_error_raises(tautulli):
    respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TautulliError, match="refused"):
        list(tautulli.history())


@respx.mock
def test_users(tautulli):
    route = respx.get(URL).respond(
        json=envelope([{"user_id": 111, "username": "matt", "friendly_name": "Matt"}])
    )

    assert tautulli.users() == [
        {"user_id": 111, "username": "matt", "friendly_name": "Matt"}
    ]
    assert route.calls[0].request.url.params["cmd"] == "get_users"


@respx.mock
def test_metadata(tautulli):
    respx.get(URL).respond(json=envelope({"guids": ["tmdb://603"]}))

    assert tautulli.metadata(42) == {"guids": ["tmdb://603"]}


@respx.mock
def test_metadata_missing_item_is_none(tautulli):
    respx.get(URL).respond(400, json=envelope({}, "error", "Unable to retrieve"))

    assert tautulli.metadata(42) is None


@respx.mock
def test_metadata_auth_error_raises(tautulli):
    respx.get(URL).respond(401, json=envelope({}, "error", "Invalid apikey"))

    with pytest.raises(TautulliError):
        tautulli.metadata(42)
