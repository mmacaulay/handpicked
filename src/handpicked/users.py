"""Discover household users, link them to Seerr, and change their settings."""

import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from handpicked.seerr import Seerr, SeerrUser
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
    seerr_link: str | None
    seerr_user: SeerrUser | None
    problem: str | None  # why there is no usable Seerr link


def discover(conn: sqlite3.Connection, tautulli: Tautulli, seerr: Seerr) -> list[User]:
    """Record every Tautulli user and link each one to a Seerr user.

    A manual link is kept. Otherwise users match on plexId, then by name
    against local Seerr users (those without a plexId), ignoring case. A name
    link is kept by Seerr id while that user exists, so renames do not break it.
    """
    seerr_users = seerr.users()
    by_id = {u.id: u for u in seerr_users}
    by_plex_id = {u.plex_id: u for u in seerr_users if u.plex_id is not None}
    by_name: dict[str, list[SeerrUser]] = defaultdict(list)
    for u in seerr_users:
        if u.plex_id is None:
            by_name[u.display_name.casefold()].append(u)
    saved_links = {
        row["user_id"]: (row["seerr_link"], row["seerr_user_id"])
        for row in conn.execute("SELECT user_id, seerr_link, seerr_user_id FROM users")
    }

    links: dict[int, tuple[str | None, SeerrUser | None, str | None]] = {}
    rows = []
    for t in tautulli.users():
        user_id, username = t["user_id"], t["username"]
        # User 0 is Tautulli's placeholder for local playback, and always
        # marked active. Inactive users have left the Plex server or Home.
        if user_id == 0 or not t["is_active"]:
            continue
        saved_link, seerr_user_id = saved_links.get(user_id, (None, None))
        if saved_link == "manual":
            seerr_user = by_id.get(seerr_user_id)
            problem = (
                None
                if seerr_user
                else f"linked Seerr user {seerr_user_id} no longer exists"
            )
            links[user_id] = ("manual", seerr_user, problem)
        elif (
            saved_link == "name"
            and seerr_user_id in by_id
            and by_id[seerr_user_id].plex_id is None
            and user_id not in by_plex_id
        ):
            links[user_id] = ("name", by_id[seerr_user_id], None)
        else:
            links[user_id] = _match(user_id, username, by_plex_id, by_name)
            seerr_user_id = links[user_id][1].id if links[user_id][1] else None
        rows.append((user_id, username, seerr_user_id, links[user_id][0]))

    with conn:
        conn.executemany(
            """
            INSERT INTO users (user_id, username, seerr_user_id, seerr_link)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                username = excluded.username,
                seerr_user_id = excluded.seerr_user_id,
                seerr_link = excluded.seerr_link
            """,
            rows,
        )

    saved = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")
    return [
        User(
            user_id=row["user_id"],
            username=row["username"],
            include=bool(row["include"]),
            max_age=row["max_age"],
            picks_per_run=row["picks_per_run"],
            seerr_link=links[row["user_id"]][0],
            seerr_user=links[row["user_id"]][1],
            problem=links[row["user_id"]][2],
        )
        for row in saved
        # Users deleted from Tautulli or inactive keep their row and settings,
        # but are not household members until they return.
        if row["user_id"] in links
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


def link(
    conn: sqlite3.Connection, seerr: Seerr, username: str, seerr_user_id: int
) -> None:
    """Link a user to a Seerr user explicitly. Discovery keeps this link."""
    user_id = _find(conn, username)
    if seerr_user_id not in {u.id for u in seerr.users()}:
        raise UserError(f"Seerr has no user with id {seerr_user_id}")
    with conn:
        conn.execute(
            "UPDATE users SET seerr_user_id = ?, seerr_link = 'manual'"
            " WHERE user_id = ?",
            (seerr_user_id, user_id),
        )


def _match(
    user_id: int,
    username: str,
    by_plex_id: dict[int, SeerrUser],
    by_name: dict[str, list[SeerrUser]],
) -> tuple[str | None, SeerrUser | None, str | None]:
    if seerr_user := by_plex_id.get(user_id):
        return "plex", seerr_user, None
    matches = by_name.get(username.casefold(), [])
    if len(matches) == 1:
        return "name", matches[0], None
    if matches:
        ids = ", ".join(str(m.id) for m in matches)
        return (
            None,
            None,
            (
                f"several local Seerr users are named {username!r} (ids {ids});"
                " choose one with `handpicked users link`"
            ),
        )
    return (
        None,
        None,
        (
            "no matching Seerr user; import them from Plex in Seerr, or create a"
            f" local Seerr user named {username!r}"
        ),
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
