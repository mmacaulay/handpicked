"""Map TMDB certifications to the minimum age a title suits."""

# Ratings with no age in their name (PG, Exempt, TV-PG) are judgment calls.
AGES = {
    ("movie", "CA"): {"E": 0, "G": 0, "PG": 8, "14A": 14, "18A": 18, "R": 18, "A": 18},
    ("tv", "CA"): {
        "Exempt": 0,
        "C": 0,
        "C8": 8,
        "G": 0,
        "PG": 8,
        "14+": 14,
        "18+": 18,
    },
    ("movie", "US"): {"G": 0, "PG": 8, "PG-13": 13, "R": 17, "NC-17": 18},
    ("tv", "US"): {
        "TV-Y": 0,
        "TV-Y7": 7,
        "TV-G": 0,
        "TV-PG": 8,
        "TV-14": 14,
        "TV-MA": 17,
    },
}

THEATRICAL = 3


def resolve(media_type: str, details: dict) -> tuple[str | None, int | None]:
    """The certification and its age from a details response.

    Canada first, then the US. Ratings missing from the tables (empty, "NR")
    count as no rating, so an unrated title resolves to (None, None).
    """
    for country in ("CA", "US"):
        ages = AGES[media_type, country]
        for certification in _certifications(media_type, details, country):
            if certification in ages:
                return certification, ages[certification]
    return None, None


def _certifications(media_type: str, details: dict, country: str) -> list[str]:
    if media_type == "tv":
        return [
            entry["rating"]
            for entry in details["content_ratings"]["results"]
            if entry["iso_3166_1"] == country
        ]
    for entry in details["release_dates"]["results"]:
        if entry["iso_3166_1"] == country:
            # Stable sort: theatrical first, the rest in TMDB's order.
            dates = sorted(
                entry["release_dates"], key=lambda d: d["type"] != THEATRICAL
            )
            return [d["certification"] for d in dates]
    return []
