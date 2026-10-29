"""Choose picks for every included user."""

import sqlite3
from dataclasses import dataclass

from handpicked import seeds
from handpicked.config import Config
from handpicked.picks import Picks, choose
from handpicked.seeds import History
from handpicked.seerr import Seerr
from handpicked.tmdb import Tmdb
from handpicked.users import User


@dataclass
class UserPicks:
    user: User
    history: History
    picks: Picks | None  # None when skipped
    skipped: str | None


def recommend(
    conn: sqlite3.Connection,
    config: Config,
    tmdb: Tmdb,
    seerr: Seerr,
    included: list[User],
    now: int,
) -> list[UserPicks]:
    """Picks for each included user. Writes nothing but the TMDB cache."""
    if not included:
        return []
    owned_or_requested = seerr.owned() | seerr.requested() | seerr.blocklisted()

    results = []
    try:
        for user in included:
            viewings = seeds.load_viewings(conn, user.user_id)
            history = seeds.analyze(viewings, config.thresholds, now)
            min_seeds = config.thresholds.min_seeds
            if len(history.seeds) < min_seeds:
                skipped = f"{len(history.seeds)} seeds, needs {min_seeds}"
                results.append(UserPicks(user, history, None, skipped))
                continue
            suggested = {
                (row["media_type"], row["tmdb_id"])
                for row in conn.execute(
                    "SELECT media_type, tmdb_id FROM suggestions WHERE user_id = ?",
                    (user.user_id,),
                )
            }
            picks = choose(
                history,
                tmdb,
                owned_or_requested | suggested | history.watched,
                count=user.picks_per_run or config.picks_per_run,
                max_age=user.max_age,
                min_vote_count=config.thresholds.min_vote_count,
            )
            results.append(UserPicks(user, history, picks, None))
    finally:
        # Keep the TMDB responses fetched so far, on dry runs and on errors.
        conn.commit()
    return results
