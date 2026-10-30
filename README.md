# Handpicked

A household-curator recommender for Plex. Each week it picks a few titles for each household member from their Tautulli watch history and TMDB recommendations, queues them as pending requests in Seerr on their behalf, and emails the admin a digest of who each pick is for and why. The admin approves or declines in Seerr's normal queue.

See `docs/PRODUCT.md` for the why and `docs/V1.md` for the spec.

## Running on Unraid

1. **Add the container.** In the Docker tab, add a container from the template at `unraid/handpicked.xml`, or save that file to `/boot/config/plugins/dockerMan/templates-user/` and pick it from the template list. The template maps `/config` to `/mnt/user/appdata/handpicked`.
2. **Start it once.** With no config, it writes `config.yml` from the example into the appdata folder and stops. If the log says it cannot create the file, run `chown -R 99:100 /mnt/user/appdata/handpicked`: the container runs as `nobody:users`.
3. **Edit `config.yml`.** Fill in the Tautulli, Seerr, and TMDB settings and the `digest:` block. Secrets can go in the template's masked fields instead of the file. Keep `dry_run: true` for now.
4. **Opt users in.** Start the container, then:
   ```
   docker exec handpicked handpicked users
   docker exec handpicked handpicked users set <name> --include --max-age 12
   ```
5. **Try a dry run.** `docker exec handpicked handpicked run --dry-run` prints the picks and emails a digest marked DRY RUN. Nothing is requested in Seerr.
6. **Go live.** Create the Seerr request user and set `services.seerr.request_user_id` (the comments in `config.yml` explain it), then set `dry_run: false` and restart. Runs follow `schedule` (default Sundays at 09:00, in the container's `TZ`), and the log shows the next run time.

Stopping the container lets a run in progress finish first, for up to 5 minutes. A manual run while another is in progress exits with "A run is already in progress".

## Development

```
uv sync
HANDPICKED_CONFIG=dev/config.yml uv run handpicked run --dry-run
uv run ruff check && uv run ruff format --check && uv run pytest
```

`dev/` is git-ignored and is the place for a local config and database. The example config is `src/handpicked/config.example.yml`.
