"""Record whether each available pick was watched, abandoned, or ignored."""

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from handpicked.config import Config
from handpicked.seeds import DAY, Viewing, finish_times, load_viewings
from handpicked.suggestions import Change, write_statuses

WEEK = 7 * DAY
OUTCOMES = ("watched", "abandoned", "ignored")


@dataclass
class Landed:
    """How one member's picks are landing."""

    username: str
    available: list[Change]  # since the last run, without an outcome yet
    outcomes: list[Change]  # since the last run
    totals: Counter[str]  # every outcome recorded so far

    @property
    def hit_rate(self) -> float | None:
        settled = sum(self.totals[o] for o in OUTCOMES)
        return self.totals["watched"] / settled if settled else None


def record(
    conn: sqlite3.Connection, config: Config, now: int, dry_run: bool = False
) -> list[Change]:
    """Settle available picks from watch history, and recheck ignored ones.
    A dry run returns the outcomes without recording them."""
    changes: list[Change] = []
    rows = conn.execute(
        "SELECT * FROM suggestions WHERE status IN ('available', 'ignored')"
    ).fetchall()
    titles: dict[int, dict[tuple[str, int], list[Viewing]]] = {}
    for row in rows:
        user_id = row["user_id"]
        if user_id not in titles:
            titles[user_id] = _by_title(load_viewings(conn, user_id))
        # Counted from the request, not available_at: a run can notice the
        # title days after it arrived. A pick is never a title the member had.
        item = [
            v
            for v in titles[user_id].get((row["media_type"], row["tmdb_id"]), [])
            if v.stopped >= row["requested_at"]
        ]
        outcome = _outcome(row, item, config, now)
        if outcome is not None and outcome != row["status"]:
            changes.append(Change.of(row, outcome))

    if not dry_run:
        write_statuses(conn, changes, now)
    return changes


def landed(conn: sqlite3.Connection, changes: list[Change]) -> list[Landed]:
    """Each member with picks that became available or settled in `changes`,
    or with outcomes recorded before, by username."""
    totals: dict[int, Counter[str]] = defaultdict(Counter)
    for row in conn.execute(
        """
        SELECT user_id, status, COUNT(*) AS n FROM suggestions
        WHERE status IN ('watched', 'abandoned', 'ignored')
        GROUP BY user_id, status
        """
    ):
        totals[row["user_id"]][row["status"]] = row["n"]
    settled = [c for c in changes if c.status in OUTCOMES]
    # A pick that became available and settled in one run is shown settled.
    done = {c.row_id for c in settled}
    available = [c for c in changes if c.status == "available" and c.row_id not in done]
    members = {c.user_id for c in available + settled} | totals.keys()
    # Not users.discover: members who have since left still have outcomes.
    names = conn.execute(
        "SELECT user_id, username FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    return [
        Landed(
            username,
            [c for c in available if c.user_id == user_id],
            [c for c in settled if c.user_id == user_id],
            totals[user_id],
        )
        for user_id, username in names
        if user_id in members
    ]


def _by_title(viewings: list[Viewing]) -> dict[tuple[str, int], list[Viewing]]:
    titles: dict[tuple[str, int], list[Viewing]] = defaultdict(list)
    for viewing in viewings:
        media_type = "movie" if viewing.media_type == "movie" else "tv"
        titles[media_type, viewing.tmdb_id].append(viewing)
    return titles


def _outcome(
    row: sqlite3.Row, item: list[Viewing], config: Config, now: int
) -> str | None:
    """The pick's outcome from its viewings, or None while it's unsettled."""
    movie = row["media_type"] == "movie"
    windows = config.outcomes
    if not item:
        weeks = windows.movie_ignore_weeks if movie else windows.tv_ignore_weeks
        return "ignored" if now - row["available_at"] >= weeks * WEEK else None
    thresholds = config.thresholds
    needed = 1 if movie else thresholds.tv_min_finished_episodes
    if len(finish_times(item, thresholds.completion_pct)) >= needed:
        return "watched"
    weeks = windows.movie_abandon_weeks if movie else windows.tv_abandon_weeks
    # A partly available show can be started before season 1 is all there,
    # and the member can't return until it is.
    last = max(item[-1].stopped, row["available_at"])
    return "abandoned" if now - last >= weeks * WEEK else None
