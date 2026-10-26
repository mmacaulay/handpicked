# CLAUDE.md

## Project

Handpicked is a household-curator recommender for Plex: per-user suggestions from watch history, queued as pending requests in Seerr, with a digest for the admin. See:

- `docs/PRODUCT.md` for the why, positioning, and parked or rejected ideas
- `docs/V1.md` for the current spec, scope, and open questions

Read both before making design decisions. Stay within v1 scope unless asked; do not build parked or out-of-scope features.

## Stack

- Python, managed with uv
- SQLite for storage, via stdlib `sqlite3` with hand-written SQL (no ORM); migrations are numbered `.sql` files tracked with `PRAGMA user_version`
- Config in YAML, validated with pydantic; long-running process with APScheduler
- See "Technical decisions" in `docs/V1.md` for the reasoning
- Packaged as a single Docker container, targeting Unraid
- Integrations: Tautulli, TMDB, Seerr (not Overseerr). Radarr/Sonarr are possible future integrations, not used in v1.

## Conventions

- The `suggestions` table and its lifecycle are the core data model. Changes to it need deliberate thought.
- Scoring must stay explainable: every suggestion records the seeds that produced it.
- Every write to an external service must respect dry-run mode.
- The core pipeline must work with no LLM configured. Any LLM features are optional and bring-your-own-key.
- Keep integration clients thin and isolated so they can be mocked in tests.
- Never use em dashes in code comments, docs, commit messages, or any other writing in this repo.
