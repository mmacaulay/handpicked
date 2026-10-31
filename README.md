# Handpicked

A household-curator recommender for Plex. Each week it picks a few titles for each household member from their Tautulli watch history and TMDB recommendations, queues them as pending requests in Seerr on their behalf, and emails the admin a digest of who each pick is for and why. The admin approves or declines in Seerr's normal queue.

See `docs/PRODUCT.md` for the why and `docs/V1.md` for the spec.

## Running on Unraid

1. **Add the container.** In the Docker tab, add a container from the template at `unraid/handpicked.xml`, or save that file to `/boot/config/plugins/dockerMan/templates-user/` and pick it from the template list.
2. **Fill in the template.** The Tautulli, Seerr, and TMDB fields are required. The SMTP fields are optional: fill them in to get the digest email. The schedule (default Sundays at 09:00, in the server's time zone) and picks per run are under "Show more settings". Then start the container. The log shows the next run time.
3. **Opt users in.** Nobody gets picks until included:
   ```
   docker exec handpicked handpicked users
   docker exec handpicked handpicked users set <name> --include --max-age 12
   ```
4. **Preview, if you like.** `docker exec handpicked handpicked run --dry-run` prints the picks, and emails the digest marked DRY RUN if SMTP is set up. Nothing is requested in Seerr.
5. **That's it.** Each scheduled run creates pending requests for the included users, for you to approve or decline in Seerr. The first one also creates a Seerr user called Handpicked, which Handpicked makes each request as before handing it to the member. It can request but not approve. Leave it be: deleting it just means the next run creates it again.

`config.yml` is optional. It can tune the scoring thresholds; `src/handpicked/config.example.yml` lists every setting. File changes apply from the next run, but the template's fields win over the file, the schedule and picks per run included. Template changes restart the container.

Stopping the container lets a run in progress finish first, for up to 5 minutes. A manual run while another is in progress exits with "A run is already in progress".

## Development

```
uv sync
HANDPICKED_CONFIG=dev/config.yml uv run handpicked run --dry-run
uv run ruff check && uv run ruff format --check && uv run pytest
```

`dev/` is git-ignored and is the place for a local config and database. The example config is `src/handpicked/config.example.yml`.
