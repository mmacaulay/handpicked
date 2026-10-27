import argparse
import sys

from handpicked import db
from handpicked.config import ConfigError, config_path, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handpicked")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Generate suggestions once")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report, write nothing (overrides config)",
    )
    args = parser.parse_args(argv)

    path = config_path()
    try:
        config = load_config(path)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    conn = db.connect(path.parent / "handpicked.db")
    db.migrate(conn)

    dry_run = args.dry_run or config.dry_run
    print(f"handpicked run (dry_run={dry_run}): pipeline not implemented yet")
    return 0
