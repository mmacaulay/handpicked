"""Sync Tautulli watch history into watch_events and resolve TMDB ids."""

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from handpicked.tautulli import Tautulli

# Re-scan this far back on each sync. A long session gets its row id when it
# stops, after shorter sessions that started later, so a sync between the two
# would otherwise miss it.
OVERLAP = timedelta(days=2)


@dataclass
class IngestStats:
    new_events: int
    resolved: int
    unresolved: int


def ingest(conn: sqlite3.Connection, tautulli: Tautulli) -> IngestStats:
    new_events = sync_history(conn, tautulli)
    resolved, unresolved = resolve_tmdb_ids(conn, tautulli)
    return IngestStats(new_events, resolved, unresolved)


def sync_history(conn: sqlite3.Connection, tautulli: Tautulli) -> int:
    """Insert new watch events. Returns how many were new."""
    latest = conn.execute("SELECT MAX(started) FROM watch_events").fetchone()[0]
    after = None
    if latest is not None:
        after = (datetime.fromtimestamp(latest, UTC) - OVERLAP).date().isoformat()

    events = [_event(row) for row in tautulli.history(after=after) if _wanted(row)]
    with conn:
        cursor = conn.executemany(
            """
            INSERT OR IGNORE INTO watch_events (
                id, reference_id, user_id, media_type, rating_key,
                grandparent_rating_key, title, year, percent_complete,
                started, stopped
            ) VALUES (
                :id, :reference_id, :user_id, :media_type, :rating_key,
                :grandparent_rating_key, :title, :year, :percent_complete,
                :started, :stopped
            )
            """,
            events,
        )
    return cursor.rowcount


def resolve_tmdb_ids(conn: sqlite3.Connection, tautulli: Tautulli) -> tuple[int, int]:
    """Look up TMDB ids for movies and shows not yet resolved.

    Returns (resolved, unresolved) counts for this run.
    """
    keys = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT item_key FROM (
                SELECT CASE media_type
                    WHEN 'episode' THEN grandparent_rating_key
                    ELSE rating_key
                END AS item_key
                FROM watch_events
            )
            WHERE item_key IS NOT NULL
                AND item_key NOT IN (SELECT rating_key FROM tmdb_ids)
            """
        )
    ]

    resolved = unresolved = 0
    for key in keys:
        metadata = tautulli.metadata(key)
        tmdb_id = _tmdb_id(metadata.get("guids", [])) if metadata else None
        if tmdb_id is None:
            unresolved += 1
            continue
        with conn:
            conn.execute(
                "INSERT INTO tmdb_ids (rating_key, tmdb_id, resolved_at)"
                " VALUES (?, ?, ?)",
                (key, tmdb_id, int(time.time())),
            )
        resolved += 1
    return resolved, unresolved


def _wanted(row: dict) -> bool:
    # Live TV rows are labelled "episode" and only marked by the live flag.
    return row["media_type"] in ("movie", "episode") and not row.get("live")


def _event(row: dict) -> dict:
    episode = row["media_type"] == "episode"
    return {
        "id": int(row["id"]),
        "reference_id": int(row["reference_id"]),
        "user_id": int(row["user_id"]),
        "media_type": row["media_type"],
        "rating_key": int(row["rating_key"]),
        "grandparent_rating_key": _int_or_none(row.get("grandparent_rating_key")),
        "title": row["grandparent_title"] if episode else row["title"],
        "year": _int_or_none(row.get("year")),
        "percent_complete": int(row["percent_complete"]),
        "started": int(row["started"]),
        "stopped": int(row["stopped"]),
    }


def _int_or_none(value: object) -> int | None:
    # Tautulli returns "" for empty numeric columns.
    return int(value) if value not in (None, "") else None


def _tmdb_id(guids: list[str]) -> int | None:
    for guid in guids:
        scheme, _, value = guid.partition("://")
        if scheme == "tmdb" and value.isdigit():
            return int(value)
    return None
