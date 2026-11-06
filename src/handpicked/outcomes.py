"""Record whether each available pick was watched, abandoned, or ignored."""

import sqlite3
from collections import Counter, defaultdict

from handpicked.config import Config
from handpicked.seeds import DAY, Viewing, finish_times, load_viewings
from handpicked.suggestions import write_statuses

WEEK = 7 * DAY


def record(
    conn: sqlite3.Connection, config: Config, now: int, dry_run: bool = False
) -> Counter[str]:
    """Settle available picks from watch history, and recheck ignored ones.
    A dry run counts the outcomes without recording them."""
    changes: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT id, user_id, media_type, tmdb_id, status, requested_at, available_at
        FROM suggestions WHERE status IN ('available', 'ignored')
        """
    ).fetchall()
    titles: dict[int, dict[tuple[str, int], list[Viewing]]] = {}
    updates = []
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
            updates.append((row["id"], outcome))
            changes[outcome] += 1

    if not dry_run:
        write_statuses(conn, updates, now)
    return changes


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
