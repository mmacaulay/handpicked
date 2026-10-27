-- Raw Tautulli sessions (get_history with grouping=0). Rows never change once
-- written; reference_id groups sessions into viewings the way Tautulli does.
CREATE TABLE watch_events (
    id INTEGER PRIMARY KEY,  -- Tautulli session_history row id
    reference_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,  -- Plex account id, same as Seerr's plexId
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'episode')),
    rating_key INTEGER NOT NULL,
    grandparent_rating_key INTEGER,  -- the show, for episodes
    title TEXT NOT NULL,  -- movie title, or show title for episodes
    year INTEGER,
    percent_complete INTEGER NOT NULL,
    started INTEGER NOT NULL,
    stopped INTEGER NOT NULL
);

CREATE INDEX watch_events_user_started ON watch_events (user_id, started);

-- TMDB ids for Plex movies and shows. Only successful lookups are stored, so
-- items that fail (deleted, or Plex briefly unreachable) are retried next run.
CREATE TABLE tmdb_ids (
    rating_key INTEGER PRIMARY KEY,
    tmdb_id INTEGER NOT NULL,
    resolved_at INTEGER NOT NULL
);
