# Changelog

## [1.0.0] - 2026-10-31

The first release.

### Added
- Weekly picks for each household member, from their Tautulli watch history and TMDB recommendations. Each pick records the watched titles behind it and how much each one counted (#2, #3, #5)
- Picks become pending requests in Seerr, for the admin to approve or decline in Seerr's queue. A Seerr user called Handpicked makes them, created on the first live run, so household members need no Seerr account (#6, #21, #23)
- Every pick is recorded with its status. Each run picks up the admin's approvals, declines and deletions in Seerr, and a title is never suggested to the same member twice (#6, #15)
- A digest email to the admin after every run, listing each member's picks and why (#7)
- Members opt in with `handpicked users set <name> --include`, and `--max-age` leaves out titles rated above an age (#4)
- `handpicked run --dry-run` prints the picks and emails the digest marked DRY RUN, without requesting anything (#5, #7)
- A Docker image on GHCR and an Unraid template, with an icon. It runs on a schedule and takes its settings from the template's fields, with no config file needed (#8, #20, #28)

### Internal
- Project setup: uv, ruff, pytest, numbered SQLite migrations, and CI that tests and builds the image on every pull request (#1)
