"""Thin client for the TMDB API v3, with an optional response cache."""

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from handpicked import ratings

MediaType = Literal["movie", "tv"]

# Well over a week, so a weekly run finds last week's responses still fresh.
CACHE_TTL = 30 * 24 * 60 * 60
# 185px wide, shown at half that so posters stay sharp on high-DPI screens.
POSTER_URL = "https://image.tmdb.org/t/p/w185"


class TmdbError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Title:
    tmdb_id: int
    media_type: MediaType
    title: str
    year: int | None
    vote_count: int
    vote_average: float
    certification: str | None
    age: int | None
    poster_path: str | None = None

    @property
    def poster_url(self) -> str | None:
        return POSTER_URL + self.poster_path if self.poster_path else None


class TmdbCache:
    """Response bodies in tmdb_cache.

    Writes are not committed here, since that would also commit whatever the
    caller has pending. They persist with the caller's next commit.
    """

    def __init__(self, conn: sqlite3.Connection, ttl: int = CACHE_TTL):
        self._conn = conn
        self._ttl = ttl

    def get(self, key: str) -> Any | None:
        row = self._conn.execute(
            "SELECT body FROM tmdb_cache WHERE key = ? AND fetched_at > ?",
            (key, int(time.time()) - self._ttl),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, body: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO tmdb_cache (key, body, fetched_at)"
            " VALUES (?, ?, ?)",
            (key, json.dumps(body), int(time.time())),
        )


class Tmdb:
    URL = "https://api.themoviedb.org/3"
    MAX_ATTEMPTS = 4

    def __init__(
        self,
        token: str,
        cache: TmdbCache | None = None,
        client: httpx.Client | None = None,
    ):
        self._headers = {"Authorization": f"Bearer {token}"}
        self._cache = cache
        self._client = client or httpx.Client(timeout=30)

    def recommendations(self, media_type: MediaType, tmdb_id: int) -> list[dict]:
        """First page of recommendations (about 20), or [] if the title is gone."""
        body = self._get(f"/{media_type}/{tmdb_id}/recommendations")
        return body["results"] if body else []

    def details(self, media_type: MediaType, tmdb_id: int) -> Title | None:
        """Title details with certification, or None if the title is gone."""
        append = "release_dates" if media_type == "movie" else "content_ratings"
        body = self._get(f"/{media_type}/{tmdb_id}", {"append_to_response": append})
        if body is None:
            return None
        if media_type == "movie":
            title, date = body["title"], body.get("release_date")
        else:
            title, date = body["name"], body.get("first_air_date")
        certification, age = ratings.resolve(media_type, body)
        return Title(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            year=int(date[:4]) if date else None,
            vote_count=body["vote_count"],
            vote_average=body["vote_average"],
            certification=certification,
            age=age,
            poster_path=body.get("poster_path"),
        )

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any | None:
        """Response body, or None on 404. Served from the cache while fresh."""
        params = params or {}
        key = f"{path}?{urlencode(sorted(params.items()))}" if params else path
        if self._cache and (body := self._cache.get(key)) is not None:
            return body
        body = self._fetch(path, params)
        if self._cache and body is not None:
            self._cache.put(key, body)
        return body

    def _fetch(self, path: str, params: dict[str, str]) -> Any | None:
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                response = self._client.get(
                    self.URL + path, params=params, headers=self._headers
                )
            except httpx.HTTPError as e:
                raise TmdbError(f"{path}: {e}") from e
            if response.status_code != 429 or attempt == self.MAX_ATTEMPTS:
                break
            delay = _retry_after(response)
            time.sleep(delay if delay is not None else 2 ** (attempt - 1))

        if response.status_code == 404:
            return None
        try:
            body = response.json()
        except ValueError as e:
            raise TmdbError(
                f"{path}: unexpected response (HTTP {response.status_code})",
                response.status_code,
            ) from e
        if response.is_error:
            raise TmdbError(
                f"{path}: {body.get('status_message') or 'request failed'}",
                response.status_code,
            )
        return body


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except KeyError, ValueError:
        return None
