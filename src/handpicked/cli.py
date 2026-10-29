import argparse
import sqlite3
import sys
import time

from handpicked import db, digest, suggestions, users
from handpicked.config import Config, ConfigError, config_path, load_config
from handpicked.digest import Entry, Report, because
from handpicked.ingest import ingest
from handpicked.pipeline import UserPicks, recommend
from handpicked.seerr import Seerr, SeerrError
from handpicked.suggestions import Requested
from handpicked.tautulli import Tautulli, TautulliError
from handpicked.tmdb import Tmdb, TmdbCache


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    path = config_path()
    try:
        config = load_config(path)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    conn = db.connect(path.parent / "handpicked.db")
    db.migrate(conn)

    tautulli = Tautulli(config.services.tautulli.url, config.services.tautulli.api_key)
    seerr = Seerr(config.services.seerr.url, config.services.seerr.api_key)
    try:
        if args.command == "run":
            return _run(args, config, conn, tautulli, seerr)
        if args.users_command == "set":
            changes = {k: getattr(args, k) for k in users.SETTINGS if hasattr(args, k)}
            if not changes:
                print(
                    "Nothing to change; see `handpicked users set -h`", file=sys.stderr
                )
                return 2
            users.update_settings(conn, args.username, changes)
            print(f"Updated {args.username}.")
        elif args.users_command == "link":
            users.link(conn, seerr, args.username, args.seerr_user_id)
            print(f"Linked {args.username} to Seerr user {args.seerr_user_id}.")
        else:
            _print_users(users.discover(conn, tautulli, seerr), config.picks_per_run)
    except TautulliError as e:
        print(f"Tautulli error: {e}", file=sys.stderr)
        return 1
    except SeerrError as e:
        print(f"Seerr error: {e}", file=sys.stderr)
        return 1
    except users.UserError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handpicked")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Generate suggestions once")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report, write nothing (overrides config)",
    )

    users_parser = commands.add_parser(
        "users", help="List household users, or change their settings"
    )
    users_commands = users_parser.add_subparsers(dest="users_command")

    # Settings default to SUPPRESS so only the options given become changes.
    set_parser = users_commands.add_parser("set", help="Change a user's settings")
    set_parser.add_argument("username")
    include = set_parser.add_mutually_exclusive_group()
    include.add_argument(
        "--include",
        dest="include",
        action="store_const",
        const=True,
        default=argparse.SUPPRESS,
        help="Give this user picks",
    )
    include.add_argument(
        "--exclude",
        dest="include",
        action="store_const",
        const=False,
        default=argparse.SUPPRESS,
        help="Stop giving this user picks",
    )
    max_age = set_parser.add_mutually_exclusive_group()
    max_age.add_argument(
        "--max-age",
        type=_at_least(0),
        default=argparse.SUPPRESS,
        metavar="AGE",
        help="Exclude titles rated above this age, and unrated titles",
    )
    max_age.add_argument(
        "--no-max-age",
        dest="max_age",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
        help="Remove the age limit",
    )
    picks = set_parser.add_mutually_exclusive_group()
    picks.add_argument(
        "--picks",
        dest="picks_per_run",
        type=_at_least(1),
        default=argparse.SUPPRESS,
        metavar="N",
        help="Picks per run for this user",
    )
    picks.add_argument(
        "--default-picks",
        dest="picks_per_run",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
        help="Use picks_per_run from the config",
    )

    link = users_commands.add_parser(
        "link", help="Link a user to a Seerr user when discovery cannot"
    )
    link.add_argument("username")
    link.add_argument("seerr_user_id", type=int)
    return parser


def _at_least(minimum: int):
    def parse(value: str) -> int:
        number = int(value)
        if number < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return number

    return parse


def _run(
    args: argparse.Namespace,
    config: Config,
    conn: sqlite3.Connection,
    tautulli: Tautulli,
    seerr: Seerr,
) -> int:
    dry_run = args.dry_run or config.dry_run
    print(f"handpicked run (dry_run={dry_run})")
    report = Report(dry_run=dry_run)
    try:
        _run_steps(report, config, conn, tautulli, seerr)
    except TautulliError as e:
        report.stopped = f"Tautulli error: {e}"
    except SeerrError as e:
        report.stopped = f"Seerr error: {e}"
    except Exception as e:
        # Tell the admin, then fail with the traceback for the logs.
        report.stopped = f"Unexpected error: {e!r}"
        _send_digest(config, report)
        raise
    if report.stopped:
        print(report.stopped, file=sys.stderr)
    sent = _send_digest(config, report)
    return 0 if sent and not report.stopped else 1


def _run_steps(
    report: Report,
    config: Config,
    conn: sqlite3.Connection,
    tautulli: Tautulli,
    seerr: Seerr,
) -> None:
    """Run the job, filling in the report. Sets report.stopped if it stops."""
    dry_run = report.dry_run
    now = int(time.time())

    as_user = config.services.seerr.request_user_id
    problem = suggestions.request_user_problem(seerr.users(), as_user)
    if problem and not dry_run:
        report.stopped = f"Cannot create requests: {problem}"
        return
    if problem:
        report.request_problem = problem
        print(f"Warning: requests cannot be created yet: {problem}")
    if not dry_run:
        changes = report.changes = suggestions.sync(conn, seerr, now)
        print(
            f"Requests since last run: {changes['approved']} approved,"
            f" {changes['declined']} declined, {changes['deleted']} deleted"
        )

    stats = report.ingest = ingest(conn, tautulli)
    print(
        f"Watch history: {stats.new_events} new events,"
        f" {stats.resolved} titles resolved,"
        f" {stats.unresolved} unresolved (retried next run)"
    )

    household = users.discover(conn, tautulli, seerr)
    included = report.included = [u for u in household if u.include]
    excluded = report.excluded = [u for u in household if not u.include]
    print(f"Users: {_count(included, 'included')}, {_count(excluded, 'not included')}")
    for user in included:
        if user.problem:
            print(f"Warning: {user.username}: {user.problem}")

    tmdb = Tmdb(config.services.tmdb.token, TmdbCache(conn))
    results = recommend(conn, config, tmdb, seerr, included, now)
    for result in results:
        requested = None
        if not dry_run and result.picks:
            if result.user.seerr_user:
                requested = suggestions.request(
                    conn, seerr, as_user, result.user, result.picks.picks, now
                )
            else:
                reason = result.user.problem or "no Seerr user"
                requested = [Requested(p, None, reason) for p in result.picks.picks]
        _print_picks(result, requested)
        report.entries.append(Entry(result, requested))
    if report.checked:
        print(
            f"\nAge ratings: found for {report.rated} of {report.checked} titles"
            f" checked ({report.rated / report.checked:.0%})"
        )


def _send_digest(config: Config, report: Report) -> bool:
    """Email the digest if one is configured. False if sending failed."""
    if config.digest is None:
        print("\nDigest not configured, not sent")
        return True
    try:
        digest.send(config.digest, digest.render(report, config.digest))
    except digest.DigestError as e:
        print(f"Digest not sent: {e}", file=sys.stderr)
        return False
    print(f"\nDigest sent to {', '.join(config.digest.recipients)}")
    return True


def _print_picks(result: UserPicks, requested: list[Requested] | None) -> None:
    """Print a user's picks, with what Seerr did with each unless a dry run."""
    history = result.history
    unresolved = (
        f", {history.unresolved} recent titles skipped (no TMDB id)"
        if history.unresolved
        else ""
    )
    print(
        f"\n{result.user.username}: {len(history.seeds)} seeds,"
        f" {len(history.abandoned)} abandoned{unresolved}"
    )
    if result.picks is None:
        print(f"  Skipped: {result.skipped}")
        return
    if not result.picks.picks:
        print(f"  No picks from {result.picks.candidates} candidates")
    for i, pick in enumerate(result.picks.picks, 1):
        title, candidate = pick.title, pick.candidate
        kind = "movie" if title.media_type == "movie" else "TV"
        print(
            f"  {i}. {title.title} ({title.year or '?'}, {kind},"
            f" {title.certification or 'unrated'}) score {candidate.score:.2f}"
        )
        print(f"     {because(candidate)}")
        if requested:
            outcome = requested[i - 1]
            print(
                f"     requested in Seerr (#{outcome.request_id})"
                if outcome.request_id
                else f"     not requested: {outcome.error}"
            )


def _count(group: list[users.User], label: str) -> str:
    names = ", ".join(u.username for u in group)
    return f"{len(group)} {label}" + (f" ({names})" if names else "")


def _print_users(household: list[users.User], default_picks: int) -> None:
    if not household:
        print("No users found in Tautulli.")
        return
    rows = [("User", "Included", "Max age", "Picks", "Seerr user", "Notes")]
    for user in household:
        seerr_user = user.seerr_user
        rows.append(
            (
                user.username,
                "yes" if user.include else "no",
                "-" if user.max_age is None else str(user.max_age),
                str(user.picks_per_run or f"{default_picks} (default)"),
                f"{seerr_user.display_name} (id {seerr_user.id}, by {user.seerr_link})"
                if seerr_user
                else "-",
                user.problem or "",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print(
            "  ".join(
                cell.ljust(w) for cell, w in zip(row, widths, strict=True)
            ).rstrip()
        )
