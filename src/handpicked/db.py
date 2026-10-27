"""SQLite connection and schema migrations."""

import re
import sqlite3
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

MIGRATIONS = files("handpicked") / "migrations"
MIGRATION_NAME = re.compile(r"^(\d+)_\w+\.sql$")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, autocommit=True)
    # These pragmas fail or are ignored inside a transaction, so set them
    # before switching to autocommit=False (which keeps one always open).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.autocommit = False
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn: sqlite3.Connection, migrations: Traversable = MIGRATIONS) -> int:
    """Apply pending migrations in order. Returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in _pending(migrations, current):
        # executescript runs inside the open transaction, so a failure rolls
        # back both the schema changes and the version bump.
        try:
            # The lone ";" ends the script's last statement even if it lacks one.
            conn.executescript(f"{script}\n;\nPRAGMA user_version = {version};")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        current = version
    return current


def _pending(migrations: Traversable, current: int) -> list[tuple[int, str]]:
    found: dict[int, Traversable] = {}
    for entry in migrations.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = MIGRATION_NAME.match(entry.name)
        if not match:
            # Skipping it would leave the schema silently incomplete.
            raise ValueError(
                f"Migration name must look like 0001_name.sql: {entry.name}"
            )
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"Duplicate migration version {version}: {entry.name}")
        found[version] = entry
    return [
        (version, found[version].read_text())
        for version in sorted(found)
        if version > current
    ]
