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
# Permission bits that let a user request movies and series: REQUEST, or
# both REQUEST_MOVIE and REQUEST_TV.
REQUEST = 32
REQUEST_MOVIE_AND_TV = 262144 | 524288

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
    email: str | None = None

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
                email=user.get("email"),
            )
            for user in self._paged("/user")
        ]

    def create_user(self, email: str, username: str, password: str) -> int:
        """Create a local user, with the server's default permissions and quota.

        Returns its id.
        """
        user = self._send(
            "POST",
            "/user",
            expected=201,
            json={"email": email, "username": username, "password": password},
        )
        return user["id"]

    def set_permissions(self, user_id: int, permissions: int) -> None:
        self._send(
            "POST",
            f"/user/{user_id}/settings/permissions",
            json={"permissions": permissions},
        )

    def remove_quota(self, user_id: int, username: str, email: str) -> None:
        """Let the user request without limit. A limit of 0 means none."""
        # Seerr sets the username and email from this body too.
        self._send(
            "POST",
            f"/user/{user_id}/settings/main",
            json={
                "username": username,
                "email": email,
                "movieQuotaLimit": 0,
                "tvQuotaLimit": 0,
            },
        )

    def delete_user(self, user_id: int) -> None:
        self._send("DELETE", f"/user/{user_id}")

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

    def request_statuses(self) -> dict[int, int]:
        """Each request's MediaRequestStatus by id, except for blocklisted titles."""
        return {
            request["id"]: request["status"]
            for request in self._paged("/request", {"filter": "all"})
        }

    def blocklisted(self) -> set[Title]:
        return {
            (item["mediaType"], item["tmdbId"]) for item in self._paged("/blocklist")
        }

    def create_request(
        self, as_user: int, user_id: int, media_type: str, tmdb_id: int
    ) -> int:
        """Create a pending request for `user_id`. Returns its id.

        Setting a new request's user takes Manage Users and Manage Requests,
        and Manage Requests approves it. So `as_user`, which must not
        auto-approve, creates the request, and the admin key then reassigns
        it. Seerr keeps an edited request pending, and checks the new user's
        quota.
        """
        body: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            body["seasons"] = "all"
        # Created is exactly 201: Seerr answers "no seasons available" with a
        # 202 and no request.
        request = self._send(
            "POST",
            "/request",
            expected=201,
            json=body,
            headers={"X-API-User": str(as_user)},
        )
        path = f"/request/{request['id']}"
        change: dict[str, Any] = {"mediaType": media_type, "userId": user_id}
        if media_type == "tv":
            change["seasons"] = [s["seasonNumber"] for s in request["seasons"]]
        try:
            self._send("PUT", path, json=change)
        except SeerrError as e:
            # Leave nothing in the queue that reads as requested by `as_user`.
            try:
                self._send("DELETE", path, expected=204)
            except SeerrError as cleanup:
                raise SeerrError(
                    f"{e}; {path} is still in Seerr as user {as_user}'s: {cleanup}",
                    e.status_code,
                ) from e
            raise
        return request["id"]

    def request_status(self, request_id: int) -> int | None:
        """The request's MediaRequestStatus, or None on a 404.

        Seerr answers 404 for any failed lookup, not only a deleted request.
        """
        try:
            return self._get(f"/request/{request_id}")["status"]
        except SeerrError as e:
            if e.status_code == 404:
                return None
            raise

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

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # No X-API-User header: reads must run as the admin, or Seerr limits
        # them to that user's own requests and hides plexId.
        return self._send("GET", path, params=params)

    def _send(
        self,
        method: str,
        path: str,
        *,
        expected: int = 200,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                self._url + path,
                headers={"X-Api-Key": self._api_key, **(headers or {})},
                **kwargs,
            )
            body = response.json() if response.content else {}
        except httpx.HTTPError as e:
            raise SeerrError(f"{path}: {e}") from e
        except ValueError as e:
            raise SeerrError(
                f"{path}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e
        if response.status_code != expected:
            raise SeerrError(
                f"{path}: {body.get('message') or 'request failed'}",
                response.status_code,
            )
        return body
