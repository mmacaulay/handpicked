"""Discover household users and change their settings."""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from handpicked.tautulli import Tautulli

SETTINGS = {"include", "max_age", "picks_per_run"}


class UserError(Exception):
    pass


@dataclass(frozen=True)
class User:
    user_id: int
    username: str
    include: bool
    max_age: int | None
    picks_per_run: int | None


def discover(conn: sqlite3.Connection, tautulli: Tautulli) -> list[User]:
    """Record every Tautulli user, and return the household's current members."""
    rows = []
    for t in tautulli.users():
        # User 0 is Tautulli's placeholder for local playback, and always
        # marked active. Inactive users have left the Plex server or Home.
        if t["user_id"] != 0 and t["is_active"]:
            rows.append((t["user_id"], t["username"]))

    with conn:
        conn.executemany(
            """
            INSERT INTO users (user_id, username) VALUES (?, ?)
            ON CONFLICT (user_id) DO UPDATE SET username = excluded.username
            """,
            rows,
        )

    current = {user_id for user_id, _ in rows}
    saved = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")
    return [
        User(
            user_id=row["user_id"],
            username=row["username"],
            include=bool(row["include"]),
            max_age=row["max_age"],
            picks_per_run=row["picks_per_run"],
        )
        for row in saved
        # Users deleted from Tautulli or inactive keep their row and settings,
        # but are not household members until they return.
        if row["user_id"] in current
    ]


def update_settings(
    conn: sqlite3.Connection, username: str, changes: Mapping[str, object]
) -> None:
    """Change include, max_age, or picks_per_run. None clears the last two."""
    if not changes.keys() <= SETTINGS:
        raise ValueError(f"Unknown settings: {sorted(changes.keys() - SETTINGS)}")
    user_id = _find(conn, username)
    columns = ", ".join(f"{name} = :{name}" for name in changes)
    with conn:
        conn.execute(
            f"UPDATE users SET {columns} WHERE user_id = :user_id",
            {**changes, "user_id": user_id},
        )


def _find(conn: sqlite3.Connection, username: str) -> int:
    """The user_id for a username, ignoring case unless that is ambiguous."""
    rows = conn.execute("SELECT user_id, username FROM users").fetchall()
    matches = [r for r in rows if r["username"].casefold() == username.casefold()]
    if len(matches) > 1:
        matches = [r for r in matches if r["username"] == username] or matches
    if len(matches) == 1:
        return matches[0]["user_id"]
    if not matches:
        raise UserError(
            f"No user named {username!r}. Run `handpicked users` to discover users."
        )
    raise UserError(f"Several users are named {username!r}, ignoring case")
