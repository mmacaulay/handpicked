"""Request picks in Seerr and track each suggestion's lifecycle."""

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass

from handpicked.picks import Candidate, Pick
from handpicked.seerr import REQUEST, REQUEST_MOVIE_AND_TV, Seerr, SeerrError, SeerrUser
from handpicked.users import User

# Seerr MediaRequestStatus values the admin's review leads to. Failed is an
# approved request that Radarr or Sonarr could not take.
REVIEWED = {2: "approved", 3: "declined", 4: "approved", 5: "approved"}


@dataclass(frozen=True)
class Requested:
    pick: Pick
    request_id: int | None
    error: str | None  # why Seerr refused it


def request_user_problem(
    seerr_users: list[SeerrUser], user_id: int | None
) -> str | None:
    """Why the configured user cannot create pending requests, if it cannot."""
    if user_id is None:
        return "services.seerr.request_user_id is not set"
    user = next((u for u in seerr_users if u.id == user_id), None)
    if user is None:
        return f"Seerr user {user_id} (services.seerr.request_user_id) does not exist"
    if user.auto_approve:
        return (
            f"Seerr user {user.display_name} must not have Admin, Manage Requests,"
            " or Auto-Approve permissions, or its requests skip approval"
        )
    both = user.permissions & REQUEST_MOVIE_AND_TV == REQUEST_MOVIE_AND_TV
    if not (user.permissions & REQUEST or both):
        return f"Seerr user {user.display_name} needs the Request permission"
    return None


def request(
    conn: sqlite3.Connection,
    seerr: Seerr,
    as_user: int,
    user: User,
    picks: list[Pick],
    now: int,
) -> list[Requested]:
    """Create a pending request for each of the user's picks, and record it.

    A refused pick gets no row, so the title can be picked again next run.
    """
    assert user.seerr_user is not None
    results = []
    for pick in picks:
        candidate = pick.candidate
        try:
            request_id = seerr.create_request(
                as_user, user.seerr_user.id, candidate.media_type, candidate.tmdb_id
            )
        except SeerrError as e:
            results.append(Requested(pick, None, str(e)))
            continue
        with conn:
            conn.execute(
                """
                INSERT INTO suggestions (
                    user_id, media_type, tmdb_id, score, seeds, status,
                    seerr_request_id, proposed_at, requested_at
                ) VALUES (?, ?, ?, ?, ?, 'requested', ?, ?, ?)
                """,
                (
                    user.user_id,
                    candidate.media_type,
                    candidate.tmdb_id,
                    candidate.score,
                    _score_parts(candidate),
                    request_id,
                    now,
                    now,
                ),
            )
        results.append(Requested(pick, request_id, None))
    return results


def sync(conn: sqlite3.Connection, seerr: Seerr, now: int) -> Counter[str]:
    """Record the admin's approvals and declines of open requests.

    A request deleted in Seerr keeps its last known status.
    """
    changes: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT id, seerr_request_id FROM suggestions
        WHERE status = 'requested' AND seerr_request_id IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        status = REVIEWED.get(seerr.request_status(row["seerr_request_id"]))
        if status is None:
            continue
        with conn:
            conn.execute(
                f"UPDATE suggestions SET status = ?, {status}_at = ? WHERE id = ?",
                (status, now, row["id"]),
            )
        changes[status] += 1
    return changes


def _score_parts(candidate: Candidate) -> str:
    """Every part of the score as JSON. The weights sum to the score."""
    parts: list[dict] = [
        {
            "media_type": seed.media_type,
            "tmdb_id": seed.tmdb_id,
            "title": seed.title,
            "weight": seed.weight,
        }
        for seed in candidate.seeds
    ]
    parts.append(
        {"tmdb_rating": candidate.vote_average, "weight": candidate.rating_prior}
    )
    return json.dumps(parts)
