"""Thin client for the Tautulli API v2."""

from collections.abc import Iterator
from typing import Any

import httpx


class TautulliError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class Tautulli:
    PAGE_SIZE = 1000

    def __init__(self, url: str, api_key: str, client: httpx.Client | None = None):
        self._url = url.rstrip("/") + "/api/v2"
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30)

    def history(self, after: str | None = None) -> Iterator[dict]:
        """Raw session rows, oldest first, optionally from a YYYY-MM-DD date."""
        start = 0
        while True:
            params: dict[str, Any] = {
                # Pass both explicitly: omitted, they follow the admin's
                # Tautulli settings. Activity rows are in-progress sessions
                # with no row id.
                "grouping": 0,
                "include_activity": 0,
                # Oldest first, so rows written mid-sync land on later pages
                # instead of shifting earlier ones.
                "order_column": "date",
                "order_dir": "asc",
                "start": start,
                "length": self.PAGE_SIZE,
            }
            if after:
                params["after"] = after
            rows = self._call("get_history", params)["data"]
            yield from rows
            if len(rows) < self.PAGE_SIZE:
                return
            start += len(rows)

    def users(self) -> list[dict]:
        """Server users, excluding deleted ones.

        A Plex Home managed user has no Plex username, so Tautulli stores its
        profile name as the username.
        """
        return self._call("get_users", {})

    def metadata(self, rating_key: int) -> dict | None:
        """Item metadata, or None if Plex cannot return it."""
        try:
            return self._call("get_metadata", {"rating_key": rating_key}) or None
        except TautulliError as e:
            # Tautulli answers 400 both for items gone from Plex and for Plex
            # being unreachable; other failures (e.g. 401 bad key) are real.
            if e.status_code == 400:
                return None
            raise

    def _call(self, cmd: str, params: dict[str, Any]) -> Any:
        try:
            response = self._client.get(
                self._url, params={"apikey": self._api_key, "cmd": cmd, **params}
            )
            body = response.json()["response"]
        except httpx.HTTPError as e:
            raise TautulliError(f"{cmd}: {e}") from e
        except (ValueError, KeyError) as e:
            raise TautulliError(
                f"{cmd}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e
        if body.get("result") != "success":
            raise TautulliError(
                f"{cmd}: {body.get('message') or 'request failed'}",
                response.status_code,
            )
        return body["data"]
