-- Every title suggested to a user, through its whole lifecycle:
-- proposed -> requested -> approved | declined -> available
--   -> watched | abandoned | ignored
-- One row per user and title, so a title is never suggested to them twice.
CREATE TABLE suggestions (
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
            'proposed', 'requested', 'approved', 'declined',
            'available', 'watched', 'abandoned', 'ignored'
        )
    ),
    seerr_request_id INTEGER,
    -- When each status was reached, kept for measuring outcomes later.
    proposed_at INTEGER NOT NULL,
    requested_at INTEGER,
    approved_at INTEGER,
    declined_at INTEGER,
    available_at INTEGER,
    watched_at INTEGER,
    abandoned_at INTEGER,
    ignored_at INTEGER,
    UNIQUE (user_id, media_type, tmdb_id)
);
