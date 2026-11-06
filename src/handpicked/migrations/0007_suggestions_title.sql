-- Store each pick's title and year, so the digest can name it without TMDB.
-- Existing rows take them from the cached TMDB details fetched when they were
-- picked; cache rows are replaced, never deleted. A row with none is named by
-- its TMDB id. SQLite cannot add a NOT NULL column without a default, so
-- rebuild the table. Nothing references suggestions.
CREATE TABLE suggestions_new (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (user_id),
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    score REAL NOT NULL,
    -- JSON list of every part of the score: each recommending seed (abandoned
    -- titles with negative weights) and the rating prior. They sum to score.
    seeds TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'proposed', 'requested', 'approved', 'declined', 'deleted',
            'available', 'watched', 'abandoned', 'ignored'
        )
    ),
    seerr_request_id INTEGER,
    -- When each status was reached, kept for measuring outcomes.
    proposed_at INTEGER NOT NULL,
    requested_at INTEGER,
    approved_at INTEGER,
    declined_at INTEGER,
    deleted_at INTEGER,
    available_at INTEGER,
    watched_at INTEGER,
    abandoned_at INTEGER,
    ignored_at INTEGER,
    UNIQUE (user_id, media_type, tmdb_id)
);

INSERT INTO suggestions_new (
    id, user_id, media_type, tmdb_id, title, year, score, seeds, status,
    seerr_request_id, proposed_at, requested_at, approved_at, declined_at,
    deleted_at, available_at, watched_at, abandoned_at, ignored_at
)
SELECT
    s.id, s.user_id, s.media_type, s.tmdb_id,
    COALESCE(
        json_extract(c.body, CASE s.media_type WHEN 'movie' THEN '$.title' ELSE '$.name' END),
        'TMDB ' || s.tmdb_id
    ),
    CAST(NULLIF(substr(json_extract(
        c.body,
        CASE s.media_type WHEN 'movie' THEN '$.release_date' ELSE '$.first_air_date' END
    ), 1, 4), '') AS INTEGER),
    s.score, s.seeds, s.status, s.seerr_request_id, s.proposed_at,
    s.requested_at, s.approved_at, s.declined_at, s.deleted_at, s.available_at,
    s.watched_at, s.abandoned_at, s.ignored_at
FROM suggestions AS s
LEFT JOIN tmdb_cache AS c ON c.key = '/' || s.media_type || '/' || s.tmdb_id
    || '?append_to_response='
    || CASE s.media_type WHEN 'movie' THEN 'release_dates' ELSE 'content_ratings' END;

DROP TABLE suggestions;

ALTER TABLE suggestions_new RENAME TO suggestions;
