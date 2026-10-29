"""Thin client for the Seerr API v1."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

# Seerr MediaStatus values: processing, partially available, available.
OWNED_STATUSES = {3, 4, 5}

# Seerr Permission bits that make requests skip the approval queue when the
# user is the API caller: ADMIN, MANAGE_REQUESTS, AUTO_APPROVE,
# AUTO_APPROVE_MOVIE, AUTO_APPROVE_TV.
AUTO_APPROVE = 2 | 16 | 128 | 256 | 512

Title = tuple[str, int]  # (media_type, tmdb_id); movie and TV ids overlap


class SeerrError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SeerrUser:
    id: int
    plex_id: int | None  # None for local users, e.g. those made for managed accounts
    display_name: str
    permissions: int

    @property
    def auto_approve(self) -> bool:
        return bool(self.permissions & AUTO_APPROVE)


class Seerr:
    PAGE_SIZE = 100

    def __init__(self, url: str, api_key: str, client: httpx.Client | None = None):
        self._url = url.rstrip("/") + "/api/v1"
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30)

    def users(self) -> list[SeerrUser]:
        return [
            SeerrUser(
                id=user["id"],
                plex_id=user.get("plexId"),
                display_name=user["displayName"],
                permissions=user["permissions"],
            )
            for user in self._paged("/user")
        ]

    def owned(self) -> set[Title]:
        """Titles in the library, or monitored in Radarr or Sonarr."""
        return {
            (media["mediaType"], media["tmdbId"])
            for media in self._paged("/media")
            if media["status"] in OWNED_STATUSES or media["status4k"] in OWNED_STATUSES
        }

    def requested(self) -> set[Title]:
        """Titles with a request in any status, including declined."""
        return {
            (request["media"]["mediaType"], request["media"]["tmdbId"])
            for request in self._paged("/request", {"filter": "all"})
        }

    def blocklisted(self) -> set[Title]:
        return {
            (item["mediaType"], item["tmdbId"]) for item in self._paged("/blocklist")
        }

    def _paged(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        skip = 0
        while True:
            # Always pass take: each endpoint has a different default page size.
            body = self._get(
                path, {**(params or {}), "take": self.PAGE_SIZE, "skip": skip}
            )
            results = body["results"]
            yield from results
            skip += len(results)
            if not results or skip >= body["pageInfo"]["results"]:
                return

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        # No X-API-User header: reads must run as the admin, or Seerr limits
        # them to that user's own requests and hides plexId.
        try:
            response = self._client.get(
                self._url + path, params=params, headers={"X-Api-Key": self._api_key}
            )
            body = response.json()
        except httpx.HTTPError as e:
            raise SeerrError(f"{path}: {e}") from e
        except ValueError as e:
            raise SeerrError(
                f"{path}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e
        if response.is_error:
            raise SeerrError(
                f"{path}: {body.get('message') or 'request failed'}",
                response.status_code,
            )
        return body
