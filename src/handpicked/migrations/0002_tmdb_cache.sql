-- Raw TMDB response bodies, keyed by request path and query. Entries past the
-- TTL are refetched and overwritten in place. Errors and 404s are not stored.
CREATE TABLE tmdb_cache (
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL,  -- JSON
    fetched_at INTEGER NOT NULL
);
