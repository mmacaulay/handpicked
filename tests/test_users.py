import pytest

from handpicked import db
from handpicked.users import UserError, discover, update_settings


class FakeTautulli:
    def __init__(self, *users, inactive=()):
        self.users_list = [
            {"user_id": i, "username": name, "is_active": int(i not in inactive)}
            for i, name in users
        ]

    def users(self):
        return self.users_list


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def test_new_users_are_not_included(conn):
    household = discover(conn, FakeTautulli((111, "matt")))

    assert household[0].include is False
    assert household[0].max_age is None
    assert household[0].picks_per_run is None


def test_rerun_keeps_settings_and_updates_names(conn):
    discover(conn, FakeTautulli((111, "matt")))
    update_settings(conn, "matt", {"include": True, "max_age": 18})

    (user,) = discover(conn, FakeTautulli((111, "matthew")))

    assert user.username == "matthew"
    assert user.include is True
    assert user.max_age == 18
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_users_gone_from_tautulli_are_not_returned(conn):
    discover(conn, FakeTautulli((111, "matt"), (222, "sam")))

    household = discover(conn, FakeTautulli((111, "matt")))

    assert [u.username for u in household] == ["matt"]
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_local_and_inactive_users_are_skipped(conn):
    tautulli = FakeTautulli((0, "Local"), (111, "matt"), (444, "old"), inactive={444})

    household = discover(conn, tautulli)

    assert [u.username for u in household] == ["matt"]


def test_returning_user_keeps_settings(conn):
    discover(conn, FakeTautulli((333, "emma")))
    update_settings(conn, "emma", {"include": True, "max_age": 10})

    assert discover(conn, FakeTautulli((333, "emma"), inactive={333})) == []
    (user,) = discover(conn, FakeTautulli((333, "emma")))

    assert user.include is True
    assert user.max_age == 10


def test_update_settings_clears_with_none(conn):
    discover(conn, FakeTautulli((333, "Emma")))
    update_settings(conn, "emma", {"include": True, "max_age": 10, "picks_per_run": 5})
    update_settings(conn, "Emma", {"max_age": None})

    row = conn.execute("SELECT include, max_age, picks_per_run FROM users").fetchone()
    assert tuple(row) == (1, None, 5)


def test_update_settings_unknown_user(conn):
    with pytest.raises(UserError, match="Run `handpicked users`"):
        update_settings(conn, "nobody", {"include": True})


def test_update_settings_rejects_other_columns(conn):
    discover(conn, FakeTautulli((111, "matt")))

    with pytest.raises(ValueError):
        update_settings(conn, "matt", {"username": "x"})


def test_exact_case_breaks_a_tie(conn):
    discover(conn, FakeTautulli((1, "Sam"), (2, "sam")))

    update_settings(conn, "sam", {"include": True})

    assert conn.execute("SELECT user_id FROM users WHERE include").fetchone()[0] == 2
    with pytest.raises(UserError, match="Several users"):
        update_settings(conn, "SAM", {"include": True})
