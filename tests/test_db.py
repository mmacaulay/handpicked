import sqlite3

import pytest

from handpicked import db


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def migrations(tmp_path):
    path = tmp_path / "migrations"
    path.mkdir()
    return path


def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def test_connect_sets_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_applies_migrations_in_order(conn, migrations):
    (migrations / "0002_add_b.sql").write_text(
        "CREATE TABLE b (a_id REFERENCES a(id));"
    )
    (migrations / "0001_add_a.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);"
    )
    (migrations / "README.md").write_text("ignored")

    assert db.migrate(conn, migrations) == 2
    assert tables(conn) == {"a", "b"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_rerun_is_a_no_op(conn, migrations):
    (migrations / "0001_add_a.sql").write_text("CREATE TABLE a (id INTEGER);")
    db.migrate(conn, migrations)

    assert db.migrate(conn, migrations) == 1


def test_only_new_migrations_run(conn, migrations):
    (migrations / "0001_add_a.sql").write_text("CREATE TABLE a (id INTEGER);")
    db.migrate(conn, migrations)
    (migrations / "0002_add_b.sql").write_text("CREATE TABLE b (id INTEGER);")

    assert db.migrate(conn, migrations) == 2
    assert tables(conn) == {"a", "b"}


def test_failed_migration_rolls_back_completely(conn, migrations):
    (migrations / "0001_add_a.sql").write_text("CREATE TABLE a (id INTEGER);")
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE b (id INTEGER);\nCREATE TABLE broken (;"
    )

    with pytest.raises(sqlite3.Error):
        db.migrate(conn, migrations)

    assert tables(conn) == {"a"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_duplicate_version_is_rejected(conn, migrations):
    (migrations / "0001_add_a.sql").write_text("CREATE TABLE a (id INTEGER);")
    (migrations / "001_add_b.sql").write_text("CREATE TABLE b (id INTEGER);")

    with pytest.raises(ValueError, match="Duplicate"):
        db.migrate(conn, migrations)


@pytest.mark.parametrize("name", ["0002_add-users.sql", "add_users.sql"])
def test_misnamed_sql_file_is_rejected(conn, migrations, name):
    (migrations / name).write_text("CREATE TABLE users (id INTEGER);")

    with pytest.raises(ValueError, match="0001_name.sql"):
        db.migrate(conn, migrations)


def test_migration_without_trailing_semicolon(conn, migrations):
    (migrations / "0001_add_a.sql").write_text(
        "CREATE TABLE a (id INTEGER);\nCREATE TABLE b (id INTEGER) -- no semicolon"
    )

    assert db.migrate(conn, migrations) == 1
    assert tables(conn) == {"a", "b"}


def test_packaged_migrations_apply(conn):
    db.migrate(conn)


def test_deleted_status_keeps_existing_suggestions(conn, migrations):
    for entry in db.MIGRATIONS.iterdir():
        if entry.name[:4] in {"0001", "0002", "0003", "0004"}:
            (migrations / entry.name).write_text(entry.read_text())
    db.migrate(conn, migrations)
    with conn:
        conn.execute("INSERT INTO users (user_id, username) VALUES (7, 'user7')")
        conn.execute(
            """
            INSERT INTO suggestions (
                user_id, media_type, tmdb_id, score, seeds, status,
                seerr_request_id, proposed_at, requested_at, declined_at
            ) VALUES (7, 'tv', 10, 1.5, '[]', 'declined', 41, 1, 2, 3)
            """
        )
    before = dict(conn.execute("SELECT * FROM suggestions").fetchone())

    db.migrate(conn)

    after = dict(conn.execute("SELECT * FROM suggestions").fetchone())
    assert after == {**before, "deleted_at": None}
    with conn:
        conn.execute("UPDATE suggestions SET status = 'deleted', deleted_at = 4")
