"""Score a user's candidates and choose their picks."""

from collections import Counter
from dataclasses import dataclass, field, replace

from handpicked.seeds import History, Seed
from handpicked.tmdb import MediaType, Title, Tmdb

# The most a perfect TMDB rating adds to a score. Small next to a seed
# (0.5 to 1.5), so it mostly breaks ties between single-seed candidates.
RATING_PRIOR_MAX = 0.25
# A seed already behind an earlier pick counts at this fraction (again for
# each further pick), so one cluster of related seeds cannot fill every
# slot. Above zero so a user with a single seed still gets their picks.
SEED_REUSE = 0.1


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
    Picks are chosen one at a time, with seeds behind earlier picks
    discounted, and each pick keeps the weights it was scored with. Details
    are fetched in that order until enough candidates pass the age filter,
    since only they carry the certification.
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

    remaining = dict(candidates)
    uses: Counter[tuple[str, int]] = Counter()  # picks each seed is behind
    picks: list[Pick] = []
    checked = rated = 0
    while len(picks) < count:
        ranked = sorted(
            (
                c
                for c in (_reused(c, uses) for c in remaining.values())
                # Drop candidates whose abandoned titles cancel out their seeds.
                if c.score > c.rating_prior
            ),
            key=lambda c: (-c.score, c.media_type, c.tmdb_id),
        )
        if not ranked:
            break
        candidate = ranked[0]
        del remaining[candidate.media_type, candidate.tmdb_id]
        title = tmdb.details(candidate.media_type, candidate.tmdb_id)
        if title is None:
            continue
        checked += 1
        rated += title.certification is not None
        # No rating fails closed for a user with an age limit.
        if max_age is not None and (title.age is None or title.age > max_age):
            continue
        picks.append(Pick(candidate, title))
        uses.update((s.media_type, s.tmdb_id) for s in candidate.seeds if s.weight > 0)
    return Picks(picks, len(candidates), checked, rated)


def _reused(candidate: Candidate, uses: Counter[tuple[str, int]]) -> Candidate:
    """The candidate with each seed discounted for the picks it is already behind."""
    return replace(
        candidate,
        seeds=[
            replace(s, weight=s.weight * SEED_REUSE ** uses[s.media_type, s.tmdb_id])
            for s in candidate.seeds
        ],
    )
