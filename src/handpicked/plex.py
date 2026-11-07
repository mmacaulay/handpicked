"""Thin client for Plex Media Server and the plex.tv API."""

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
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

    def shares(self) -> list[Share]:
        users = self._xml("/users")
        with _parsing("plex.tv/users"):
            return [
                Share(
                    int(user.attrib["id"]),
                    user.get("filterMovies", ""),
                    user.get("filterTelevision", ""),
                )
                for user in users
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

    def library_items(self) -> dict[Title, list[Item]]:
        """Every item in the movie and TV libraries, by TMDB id. A title can
        be in more than one library, such as a 4K one."""
        items: dict[Title, list[Item]] = {}
        body = self._json("/library/sections")
        with _parsing(f"{self._url}/library/sections"):
            sections = [
                (section["key"], MEDIA_TYPES[section["type"]])
                for section in body["MediaContainer"].get("Directory", [])
                if section["type"] in MEDIA_TYPES
            ]
        for key, media_type in sections:
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
        if response.status_code != 200:
            hint = ", check the Plex token" if response.status_code == 401 else ""
            raise PlexError(
                f"{where}: request failed (HTTP {response.status_code}){hint}",
                response.status_code,
            )
        return response


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
