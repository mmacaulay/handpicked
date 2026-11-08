# Changelog

## [2.0.0] - 2026-11-08

Picks now reach Plex, and Handpicked measures whether they land.

### Added
- Approved picks are followed until they're in the library. An approved request that Seerr later declines is recorded as declined (#30, #17)
- Each available pick's outcome is recorded from Tautulli: watched, abandoned, or ignored. Ignored picks are checked again each run, in case the member starts one later (#31)
- A "How picks are landing" section in the digest: per member, new arrivals, new outcomes with the seeds behind them, and a hit rate (#32)
- Optional Plex fields in the template: the Plex URL and the admin's token. Delivery needs Plex Media Server 1.43.2.10687 or newer and Plex Pass. If Plex isn't ready, the digest says why and the rest of the run carries on (#33)
- Each included member gets a private "Picked for <username>" row in each movie and TV library, shown on their Home and no one else's (#34, #35)
- Available picks are added to their member's row, and taken out once watched, abandoned, or ignored (#35)

### Changed
- With Plex configured, Handpicked edits every member's Plex sharing filters, adding rules that hide other members' rows. Every other condition, such as rating limits, is kept as it was. A member with an age preset, or a filter it can't safely change, holds back every member's row until the admin changes it, and the digest says so (#34)
- With Plex configured, a pick's ignore window counts from when it was added to the member's row, not from when it arrived in the library (#35)
- The database is upgraded on first start. Keep a copy of `handpicked.db` if you might go back to 1.0.0

### Internal
- The v2 spec, with the Plex delivery research and live test results (`docs/V2.md`)

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
