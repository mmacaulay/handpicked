"""Request picks in Seerr and track each suggestion's lifecycle."""

import json
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass

from handpicked.picks import Candidate, Pick
from handpicked.seerr import REQUEST, Seerr, SeerrError, SeerrUser
from handpicked.users import User

# Handpicked's own Seerr user, which creates every request. Found by this
# email, and created on the first live run.
REQUEST_USER_EMAIL = "handpicked@handpicked.invalid"
REQUEST_USER_NAME = "Handpicked"

# Seerr MediaRequestStatus values the admin's review leads to. Failed is an
# approved request that Radarr or Sonarr could not take.
REVIEWED = {2: "approved", 3: "declined", 4: "approved", 5: "approved"}


@dataclass(frozen=True)
class Requested:
    pick: Pick
    request_id: int | None
    error: str | None  # why Seerr refused it


def find_request_user(seerr_users: list[SeerrUser]) -> SeerrUser | None:
    return next(
        (u for u in seerr_users if (u.email or "").lower() == REQUEST_USER_EMAIL),
        None,
    )


def create_request_user(seerr: Seerr) -> SeerrUser:
    """Create Handpicked's Seerr user, able to request but not to approve."""
    # Nobody logs in as it. Seerr needs a password, or email to send one.
    user_id = seerr.create_user(
        REQUEST_USER_EMAIL, REQUEST_USER_NAME, secrets.token_urlsafe(32)
    )
    try:
        # A new user gets the server's default permissions, which may approve
        # requests, and its default quota, which would refuse picks. Approval
        # is left to the admin, who can still give it auto-approve.
        seerr.set_permissions(user_id, REQUEST)
        seerr.remove_quota(user_id, REQUEST_USER_NAME, REQUEST_USER_EMAIL)
        # Seerr emails a user every notification by default, here to an
        # address that can't receive them.
        seerr.disable_email(user_id)
    except SeerrError as e:
        # Otherwise the next run finds it and requests as it, half set up.
        try:
            seerr.delete_user(user_id)
        except SeerrError as cleanup:
            raise SeerrError(
                f"{e}; Seerr user {user_id} is left half set up: {cleanup}",
                e.status_code,
            ) from e
        raise
    return SeerrUser(user_id, REQUEST_USER_NAME, REQUEST_USER_EMAIL)


def request(
    conn: sqlite3.Connection,
    seerr: Seerr,
    as_user: int,
    user: User,
    picks: list[Pick],
    now: int,
) -> list[Requested]:
    """Request each of the user's picks as `as_user`, and record it.

    A refused pick gets no row, so the title can be picked again next run.
    """
    results = []
    for pick in picks:
        candidate = pick.candidate
        try:
            request_id = seerr.create_request(
                as_user, candidate.media_type, candidate.tmdb_id
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
    """Record the admin's approvals, declines, and deletions of open requests."""
    changes: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT id, seerr_request_id FROM suggestions
        WHERE status = 'requested' AND seerr_request_id IS NOT NULL
        """
    ).fetchall()
    if not rows:
        return changes
    listed = seerr.request_statuses()
    for row in rows:
        request_id = row["seerr_request_id"]
        if request_id in listed:
            seerr_status = listed[request_id]
        else:
            # The list leaves out blocklisted titles, and paging can skip a
            # request if the list changes meanwhile. Seerr answers 404 for any
            # failed lookup, so a request is deleted only if both miss it.
            seerr_status = seerr.request_status(request_id)
        status = "deleted" if seerr_status is None else REVIEWED.get(seerr_status)
        if status is not None:
            _record(conn, row["id"], status, now)
            changes[status] += 1
    return changes


def _record(conn: sqlite3.Connection, row_id: int, status: str, now: int) -> None:
    with conn:
        conn.execute(
            f"UPDATE suggestions SET status = ?, {status}_at = ? WHERE id = ?",
            (status, now, row_id),
        )


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
