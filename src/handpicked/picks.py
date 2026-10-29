"""Score a user's candidates and choose their picks."""

from dataclasses import dataclass, field

from handpicked.seeds import History, Seed
from handpicked.tmdb import MediaType, Title, Tmdb

# The most a perfect TMDB rating adds to a score. Small next to a seed
# (0.5 to 1.5), so it mostly breaks ties between single-seed candidates.
RATING_PRIOR_MAX = 0.25


@dataclass
class Candidate:
    media_type: MediaType
    tmdb_id: int
    vote_average: float
    seeds: list[Seed] = field(default_factory=list)  # abandoned ones included

    @property
    def rating_prior(self) -> float:
        return self.vote_average / 10 * RATING_PRIOR_MAX

    @property
    def score(self) -> float:
        return sum(seed.weight for seed in self.seeds) + self.rating_prior


@dataclass(frozen=True)
class Pick:
    candidate: Candidate
    title: Title


@dataclass
class Picks:
    picks: list[Pick]
    candidates: int
    checked: int  # candidates whose details were fetched
    rated: int  # of those, how many have an age rating


def choose(
    history: History,
    tmdb: Tmdb,
    exclude: set[tuple[str, int]],
    *,
    count: int,
    max_age: int | None,
    min_vote_count: int,
) -> Picks:
    """The top `count` candidates recommended by the user's seeds.

    Abandoned titles only lower the score of candidates a seed recommends.
    Details are fetched in score order until enough candidates pass the age
    filter, since only they carry the certification.
    """
    candidates: dict[tuple[str, int], Candidate] = {}
    # Seeds first, so abandoned titles only reach candidates that exist.
    for seed in history.seeds + history.abandoned:
        for rec in tmdb.recommendations(seed.media_type, seed.tmdb_id):
            # Recommendations share the seed's media type, already in the
            # 'movie'/'tv' form the exclude set uses.
            key = (seed.media_type, rec["id"])
            if key in exclude or rec["vote_count"] < min_vote_count:
                continue
            if key not in candidates:
                if seed.weight < 0:
                    continue
                candidates[key] = Candidate(
                    seed.media_type, rec["id"], rec["vote_average"]
                )
            candidates[key].seeds.append(seed)

    ranked = sorted(
        # Drop candidates whose abandoned titles cancel out their seeds.
        (c for c in candidates.values() if c.score > c.rating_prior),
        key=lambda c: (-c.score, c.media_type, c.tmdb_id),
    )
    picks: list[Pick] = []
    checked = rated = 0
    for candidate in ranked:
        if len(picks) == count:
            break
        title = tmdb.details(candidate.media_type, candidate.tmdb_id)
        if title is None:
            continue
        checked += 1
        rated += title.certification is not None
        # No rating fails closed for a user with an age limit.
        if max_age is not None and (title.age is None or title.age > max_age):
            continue
        picks.append(Pick(candidate, title))
    return Picks(picks, len(candidates), checked, rated)
