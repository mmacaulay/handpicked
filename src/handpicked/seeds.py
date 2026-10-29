"""Turn one user's watch history into weighted seeds."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from handpicked.config import Thresholds
from handpicked.tmdb import MediaType

DAY = 24 * 60 * 60

# Starting points for tuning against real dry runs.
RECENCY_FLOOR = 0.5  # weight of a seed finished at the far edge of the window
REWATCH_BOOST = 1.5
ABANDONED_WEIGHT = -0.5
# A second finish this soon is one viewing, e.g. stopping at the credits
# (counted as finished) and later playing the last few minutes.
REPEAT_FINISH = DAY


@dataclass(frozen=True)
class Viewing:
    """One viewing: the sessions Tautulli grouped under a reference_id."""

    media_type: str  # 'movie' or 'episode'
    rating_key: int  # the movie or episode
    item_key: int  # the movie, or the show for episodes
    tmdb_id: int | None  # of the movie or show
    title: str  # movie title, or show title for episodes
    percent_complete: int
    stopped: int


@dataclass(frozen=True)
class Seed:
    media_type: MediaType
    tmdb_id: int
    title: str
    weight: float  # negative for an abandoned title


@dataclass
class History:
    seeds: list[Seed] = field(default_factory=list)
    abandoned: list[Seed] = field(default_factory=list)
    watched: set[tuple[str, int]] = field(default_factory=set)  # every resolved title
    unresolved: int = 0  # titles viewed in the window with no TMDB id


def load_viewings(conn: sqlite3.Connection, user_id: int) -> list[Viewing]:
    rows = conn.execute(
        """
        SELECT v.*, t.tmdb_id FROM (
            SELECT
                media_type,
                rating_key,
                CASE media_type
                    WHEN 'episode' THEN grandparent_rating_key
                    ELSE rating_key
                END AS item_key,
                title,
                -- Position, so the furthest session is the viewing's completion.
                -- Tautulli reports huge values for items with no duration.
                MIN(MAX(percent_complete), 100) AS percent_complete,
                MAX(stopped) AS stopped
            FROM watch_events
            WHERE user_id = ?
            GROUP BY reference_id
        ) AS v
        LEFT JOIN tmdb_ids AS t ON t.rating_key = v.item_key
        WHERE v.item_key IS NOT NULL
        ORDER BY v.stopped
        """,
        (user_id,),
    )
    return [Viewing(**row) for row in rows]


def analyze(viewings: list[Viewing], thresholds: Thresholds, now: int) -> History:
    """Seeds and abandoned titles from viewings sorted by stopped."""
    window = thresholds.seed_window_days * DAY
    window_start = now - window
    history = History()

    # Keyed by title rather than Plex item, so copies in several libraries
    # (e.g. HD and 4K) count as one title.
    items: dict[tuple[MediaType, int], list[Viewing]] = defaultdict(list)
    unresolved = set()
    for viewing in viewings:
        if viewing.tmdb_id is None:
            if viewing.stopped >= window_start:
                unresolved.add(viewing.item_key)
            continue
        seed_type: MediaType = "movie" if viewing.media_type == "movie" else "tv"
        items[seed_type, viewing.tmdb_id].append(viewing)
    history.unresolved = len(unresolved)
    history.watched = set(items)

    for (seed_type, tmdb_id), item in items.items():
        latest = item[-1]
        finishes = _finishes(item, thresholds.completion_pct)
        in_window = [t for t in finishes.values() if max(t) >= window_start]
        recent = [max(t) for t in in_window]
        rewatched = any(len(t) > 1 for t in in_window)

        if seed_type == "movie":
            is_seed = bool(recent)
            is_abandoned = (
                not finishes
                and latest.stopped >= window_start
                and max(v.percent_complete for v in item) < thresholds.abandon_pct
            )
        else:
            is_seed = len(recent) >= thresholds.tv_min_finished_episodes
            is_abandoned = (
                len({v.rating_key for v in item}) <= 2
                and window_start
                <= latest.stopped
                <= now - thresholds.tv_abandon_weeks * 7 * DAY
            )

        if is_seed:
            weight = _recency(max(recent), now, window)
            if rewatched:
                weight *= REWATCH_BOOST
            history.seeds.append(Seed(seed_type, tmdb_id, latest.title, weight))
        elif is_abandoned:
            history.abandoned.append(
                Seed(seed_type, tmdb_id, latest.title, ABANDONED_WEIGHT)
            )
    return history


def _finishes(item: list[Viewing], completion_pct: int) -> dict[int, list[int]]:
    """Stop times of finished viewings per movie or episode, near repeats dropped."""
    finishes: dict[int, list[int]] = defaultdict(list)
    for viewing in item:
        if viewing.percent_complete < completion_pct:
            continue
        times = finishes[viewing.rating_key]
        if times and viewing.stopped - times[-1] < REPEAT_FINISH:
            continue
        times.append(viewing.stopped)
    return finishes


def _recency(stopped: int, now: int, window: int) -> float:
    """1.0 for today, falling linearly to RECENCY_FLOOR at the window's edge."""
    age = min(max(now - stopped, 0), window) / window
    return 1 - (1 - RECENCY_FLOOR) * age
