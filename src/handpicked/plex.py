"""Thin client for Plex Media Server and the plex.tv API."""

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

import httpx

from handpicked.seerr import Title

# The legacy endpoints answer in XML, whatever the Accept header says.
PLEX_TV = "https://plex.tv/api"

# Hiding a labelled collection from a member's Home needs this server
# version, and Plex Pass on the admin account.
MIN_VERSION = (1, 43, 2, 10687)

# Plex library types, as Handpicked's media types.
MEDIA_TYPES = {"movie": "movie", "show": "tv"}
# Plex metadata type numbers, for making a collection of that media type.
COLLECTION_TYPES = {"movie": 1, "tv": 2}


class PlexError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Server:
    release: tuple[int, ...]  # the version without its build hash, as numbers
    machine_id: str
    plex_pass: bool

    @property
    def version(self) -> str:
        return _dotted(self.release)


@dataclass(frozen=True)
class Share:
    """A member's share filters, one per library type, as Plex stores them."""

    user_id: int
    movies: str
    television: str


@dataclass(frozen=True)
class Item:
    section_id: str
    rating_key: str


@dataclass(frozen=True)
class Section:
    key: str
    media_type: str
    title: str


@dataclass(frozen=True)
class Promotion:
    own_home: bool
    shared_home: bool


class Plex:
    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self._url = url.rstrip("/")
        self._token = token
        # Longer than httpx's default: the server can be slow to build Home
        # rows after being idle.
        self._client = client or httpx.Client(timeout=30)

    def as_member(self, token: str) -> Plex:
        """Read the server as a member, with their token from member_tokens."""
        return Plex(self._url, token, self._client)

    def server(self) -> Server:
        body = self._json("/")
        with _parsing(f"{self._url}/"):
            body = body["MediaContainer"]
            version = body["version"].partition("-")[0]
            return Server(
                tuple(int(part) for part in version.split(".")),
                body["machineIdentifier"],
                bool(body.get("myPlexSubscription")),
            )

    def shares(self, machine_id: str) -> list[Share]:
        """The filters of each member this server is shared with. plex.tv
        lists every friend, some with no access to it."""
        users = self._xml("/users")
        with _parsing("plex.tv/users"):
            return [
                Share(
                    int(user.attrib["id"]),
                    user.get("filterMovies", ""),
                    user.get("filterTelevision", ""),
                )
                for user in users
                if any(
                    server.get("machineIdentifier") == machine_id
                    for server in user.iter("Server")
                )
            ]

    def age_presets(self) -> dict[int, str | None]:
        """Each Plex Home member's age preset (Little kid, Older kid, Teen),
        or None."""
        users = self._xml("/home/users")
        with _parsing("plex.tv/home/users"):
            return {
                int(user.attrib["id"]): user.get("restrictionProfile") or None
                for user in users
            }

    def account_id(self) -> int:
        """The Plex account id of the token's owner, the server admin."""
        where = "plex.tv/v2/user"
        response = self._get(where, f"{PLEX_TV}/v2/user", accept="application/json")
        with _parsing(where):
            return int(response.json()["id"])

    def member_tokens(self, machine_id: str) -> dict[int, str]:
        """Each member's token for this server. Keep them in memory only, and
        never log them. A member yet to accept an invite has none."""
        path = f"/servers/{machine_id}/shared_servers"
        shares = self._xml(path)
        with _parsing(f"plex.tv{path}"):
            return {
                int(share.attrib["userID"]): share.attrib["accessToken"]
                for share in shares
                if share.get("accessToken")
            }

    def sections(self) -> list[Section]:
        """The movie and TV libraries."""
        body = self._json("/library/sections")
        with _parsing(f"{self._url}/library/sections"):
            return [
                Section(section["key"], MEDIA_TYPES[section["type"]], section["title"])
                for section in body["MediaContainer"].get("Directory", [])
                if section["type"] in MEDIA_TYPES
            ]

    def library_items(self) -> dict[Title, list[Item]]:
        """Every item in the movie and TV libraries, by TMDB id. A title can
        be in more than one library, such as a 4K one."""
        items: dict[Title, list[Item]] = {}
        for section in self.sections():
            key, media_type = section.key, section.media_type
            path = f"/library/sections/{key}/all"
            body = self._json(path, {"includeGuids": 1})
            with _parsing(self._url + path):
                for entry in body["MediaContainer"].get("Metadata", []):
                    for guid in entry.get("Guid", []):
                        scheme, _, tmdb_id = guid["id"].partition("://")
                        if scheme == "tmdb" and tmdb_id.isdigit():
                            items.setdefault((media_type, int(tmdb_id)), []).append(
                                Item(key, entry["ratingKey"])
                            )
        return items

    def collections(self, section: Section, label: str | None = None) -> list[str]:
        """The rating keys of the section's collections this token can see,
        only those with `label` if given. Plex matches labels ignoring case."""
        path = f"/library/sections/{section.key}/collections"
        body = self._json(path, {"label": label} if label else None)
        with _parsing(self._url + path):
            return [c["ratingKey"] for c in body["MediaContainer"].get("Metadata", [])]

    def create_collection(self, section: Section, title: str, label: str) -> str:
        """Make an empty, unpromoted collection with a label. Returns its
        rating key."""
        path = "/library/collections"
        params = {
            "type": COLLECTION_TYPES[section.media_type],
            "title": title,
            "smart": 0,
            "sectionId": section.key,
        }
        body = self._send("POST", path, params)
        with _parsing(self._url + path):
            key = body["MediaContainer"]["Metadata"][0]["ratingKey"]
        try:
            self._send(
                "PUT",
                f"/library/sections/{section.key}/all",
                {"type": 18, "id": key, "label[0].tag.tag": label, "label.locked": 1},
            )
        except PlexError:
            # Unlabelled, it would show to every member, and no run would
            # find it again. If this fails too, the label error is the one
            # to report.
            with suppress(PlexError):
                self._send("DELETE", f"/library/collections/{key}")
            raise
        return key

    def promotions(self, section: Section) -> dict[str, Promotion]:
        """Where the section's managed rows are promoted, by hub identifier."""
        path = f"/hubs/sections/{section.key}/manage"
        body = self._json(path)
        with _parsing(self._url + path):
            return {
                hub["identifier"]: Promotion(
                    bool(hub.get("promotedToOwnHome")),
                    bool(hub.get("promotedToSharedHome")),
                )
                for hub in body["MediaContainer"].get("Hub", [])
            }

    def promote(self, section: Section, key: str, promotion: Promotion) -> None:
        """Promote a collection that isn't managed yet. Never to the
        library's Recommended tab."""
        self._send(
            "POST",
            f"/hubs/sections/{section.key}/manage",
            {
                "metadataItemId": key,
                "promotedToRecommended": 0,
                "promotedToOwnHome": int(promotion.own_home),
                "promotedToSharedHome": int(promotion.shared_home),
            },
        )

    def demote(self, section: Section, key: str) -> None:
        """Take a collection off every Home."""
        self._send(
            "DELETE", f"/hubs/sections/{section.key}/manage/{hub_id(section, key)}"
        )

    def home_rows(self, section: Section) -> set[str]:
        """The hub identifiers of the section's rows on this token's Home."""
        path = "/hubs/promoted"
        body = self._json(path, {"contentDirectoryID": section.key})
        with _parsing(self._url + path):
            return {
                hub["hubIdentifier"] for hub in body["MediaContainer"].get("Hub", [])
            }

    def set_filters(self, user_id: int, movies: str, television: str) -> None:
        """Replace a member's share filters. Both are sent: each write replaces
        the whole filter."""
        where = f"plex.tv/users/{user_id}"
        try:
            response = self._client.put(
                f"{PLEX_TV}/users/{user_id}",
                params={"filterMovies": movies, "filterTelevision": television},
                headers={"X-Plex-Token": self._token},
            )
        except httpx.HTTPError as e:
            raise PlexError(f"{where}: {e}") from e
        _check(where, response)

    def _send(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        url = self._url + path
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                headers={"X-Plex-Token": self._token, "Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise PlexError(f"{url}: {e}") from e
        _check(url, response)
        try:
            return response.json() if response.content else {}
        except ValueError as e:
            raise PlexError(
                f"{url}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e

    def _json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._url + path
        response = self._get(url, url, params, "application/json")
        try:
            return response.json()
        except ValueError as e:
            raise PlexError(
                f"{url}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e

    def _xml(self, path: str) -> ET.Element:
        where = f"plex.tv{path}"
        response = self._get(where, PLEX_TV + path)
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as e:
            raise PlexError(
                f"{where}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e

    def _get(
        self,
        where: str,
        url: str,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        # The token goes in a header, never the URL, which errors can quote.
        headers = {"X-Plex-Token": self._token}
        if accept:
            headers["Accept"] = accept
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise PlexError(f"{where}: {e}") from e
        _check(where, response)
        return response


def hub_id(section: Section, key: str) -> str:
    """How Plex names a collection's row."""
    return f"custom.collection.{section.key}.{key}"


def _check(where: str, response: httpx.Response) -> None:
    if response.status_code != 200:
        hint = ", check the Plex token" if response.status_code == 401 else ""
        raise PlexError(
            f"{where}: request failed (HTTP {response.status_code}){hint}",
            response.status_code,
        )


@contextmanager
def _parsing(where: str) -> Iterator[None]:
    """Raise a reply of the wrong shape as a PlexError."""
    try:
        yield
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise PlexError(f"{where}: unexpected response") from e


def delivery_problem(server: Server) -> str | None:
    """Why picks can't be delivered on this server, or None if they can."""
    if server.release < MIN_VERSION:
        problem = (
            f"Plex Media Server {server.version} is older than {_dotted(MIN_VERSION)}"
        )
    elif not server.plex_pass:
        problem = "the Plex admin account has no Plex Pass"
    else:
        return None
    return f"{problem}, which keeps a member's row off the others' Home screens"


def _dotted(release: tuple[int, ...]) -> str:
    return ".".join(map(str, release))
