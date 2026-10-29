-- Add the deleted status, for a request deleted in Seerr while pending:
-- proposed -> requested -> approved -> available -> watched | abandoned | ignored
--                       -> declined | deleted (final)
-- SQLite cannot change a CHECK constraint, so rebuild the table. Nothing
-- references suggestions, so dropping it is safe with foreign keys on.
CREATE TABLE suggestions_new (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (user_id),
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
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
    -- When each status was reached, kept for measuring outcomes later.
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
    id, user_id, media_type, tmdb_id, score, seeds, status, seerr_request_id,
    proposed_at, requested_at, approved_at, declined_at, available_at,
    watched_at, abandoned_at, ignored_at
)
SELECT
    id, user_id, media_type, tmdb_id, score, seeds, status, seerr_request_id,
    proposed_at, requested_at, approved_at, declined_at, available_at,
    watched_at, abandoned_at, ignored_at
FROM suggestions;

DROP TABLE suggestions;

ALTER TABLE suggestions_new RENAME TO suggestions;
