-- When a pick was put in its member's Plex row, and taken out again once its
-- outcome settled. The ignore window counts from delivered_at. The item put
-- in the row is kept, so it's the one taken out, even if the title is matched
-- again in Plex meanwhile.
ALTER TABLE suggestions ADD COLUMN delivered_at INTEGER;
ALTER TABLE suggestions ADD COLUMN undelivered_at INTEGER;
ALTER TABLE suggestions ADD COLUMN plex_rating_key TEXT;
