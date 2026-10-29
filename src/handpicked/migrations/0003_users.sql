-- Household members, one row per Tautulli user. Discovery refreshes the
-- username and Seerr link each run; only the admin changes the settings.
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,  -- Plex account id, same as watch_events.user_id
    username TEXT NOT NULL,  -- for a managed user, its Plex profile name
    seerr_user_id INTEGER,
    -- How the link was made. Discovery never replaces a manual link.
    seerr_link TEXT CHECK (seerr_link IN ('plex', 'name', 'manual')),
    include INTEGER NOT NULL DEFAULT 0 CHECK (include IN (0, 1)),
    max_age INTEGER CHECK (max_age >= 0),
    picks_per_run INTEGER CHECK (picks_per_run >= 1)  -- NULL uses the config default
);
