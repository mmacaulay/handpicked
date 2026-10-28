from handpicked.ratings import resolve


def movie(*countries):
    """countries: (iso, [(release type, certification), ...]) pairs."""
    return {
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": iso,
                    "release_dates": [
                        {"type": type, "certification": cert} for type, cert in dates
                    ],
                }
                for iso, dates in countries
            ]
        }
    }


def tv(*ratings):
    return {
        "content_ratings": {
            "results": [{"iso_3166_1": iso, "rating": r} for iso, r in ratings]
        }
    }


def test_movie_prefers_theatrical():
    details = movie(("CA", [(4, "PG"), (3, "14A"), (5, "18A")]))

    assert resolve("movie", details) == ("14A", 14)


def test_movie_without_theatrical_uses_first_non_empty():
    details = movie(("CA", [(1, ""), (4, "PG"), (5, "14A")]))

    assert resolve("movie", details) == ("PG", 8)


def test_movie_empty_theatrical_uses_another_release():
    details = movie(("CA", [(3, ""), (4, "G")]))

    assert resolve("movie", details) == ("G", 0)


def test_movie_empty_certifications_fall_back_to_us():
    details = movie(("CA", [(3, ""), (4, "")]), ("US", [(4, "R"), (3, "PG-13")]))

    assert resolve("movie", details) == ("PG-13", 13)


def test_tv_prefers_canada():
    details = tv(("US", "TV-MA"), ("CA", "C8"))

    assert resolve("tv", details) == ("C8", 8)


def test_tv_falls_back_to_us():
    details = tv(("GB", "15"), ("US", "TV-14"))

    assert resolve("tv", details) == ("TV-14", 14)


def test_unrecognised_rating_falls_through():
    details = movie(("CA", [(3, "NR")]), ("US", [(3, "R")]))

    assert resolve("movie", details) == ("R", 17)


def test_quebec_is_ignored():
    details = tv(("CA-QC", "13+"), ("US", "TV-PG"))

    assert resolve("tv", details) == ("TV-PG", 8)


def test_exempt_is_age_zero():
    assert resolve("tv", tv(("CA", "Exempt"))) == ("Exempt", 0)


def test_no_rating():
    assert resolve("movie", movie()) == (None, None)
    assert resolve("movie", movie(("US", [(3, "NR")]))) == (None, None)
    assert resolve("tv", tv()) == (None, None)
