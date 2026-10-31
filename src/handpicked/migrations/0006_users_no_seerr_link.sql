-- Picks are requested as Handpicked's own Seerr user and stay attributed to
-- it, so household members no longer need a linked Seerr user.
ALTER TABLE users DROP COLUMN seerr_link;
ALTER TABLE users DROP COLUMN seerr_user_id;
