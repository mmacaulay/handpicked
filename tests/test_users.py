import pytest

from handpicked import db
from handpicked.seerr import SeerrUser
from handpicked.users import UserError, discover, link, update_settings

ADMIN = SeerrUser(id=1, plex_id=111, display_name="matt", permissions=2)
PARTNER = SeerrUser(id=2, plex_id=222, display_name="sam", permissions=32)
KID = SeerrUser(id=7, plex_id=None, display_name="Emma", permissions=32)


class FakeTautulli:
    def __init__(self, *users, inactive=()):
        self.users_list = [
            {"user_id": i, "username": name, "is_active": int(i not in inactive)}
            for i, name in users
        ]

    def users(self):
        return self.users_list


class FakeSeerr:
    def __init__(self, *users):
        self.users_list = list(users)

    def users(self):
        return self.users_list


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    yield conn
    conn.close()


def by_name(household):
    return {u.username: u for u in household}


def test_links_by_plex_id_and_by_name(conn):
    tautulli = FakeTautulli((111, "matt"), (222, "sam"), (333, "emma"))
    seerr = FakeSeerr(ADMIN, PARTNER, KID)

    household = by_name(discover(conn, tautulli, seerr))

    assert household["matt"].seerr_user == ADMIN
    assert household["matt"].seerr_link == "plex"
    assert household["emma"].seerr_user == KID
    assert household["emma"].seerr_link == "name"
    assert household["emma"].problem is None
    stored = dict(conn.execute("SELECT user_id, seerr_user_id FROM users"))
    assert stored == {111: 1, 222: 2, 333: 7}


def test_new_users_are_not_included(conn):
    household = discover(conn, FakeTautulli((111, "matt")), FakeSeerr(ADMIN))

    assert household[0].include is False
    assert household[0].max_age is None
    assert household[0].picks_per_run is None


def test_name_match_skips_users_with_a_plex_account(conn):
    # An imported adult named like a kid's profile must never be linked to it.
    tautulli = FakeTautulli((333, "sam"))
    seerr = FakeSeerr(PARTNER)

    (user,) = discover(conn, tautulli, seerr)

    assert user.seerr_user is None
    assert "no matching Seerr user" in user.problem


def test_ambiguous_name_is_reported(conn):
    twin = SeerrUser(id=8, plex_id=None, display_name="EMMA", permissions=32)
    tautulli = FakeTautulli((333, "emma"))

    (user,) = discover(conn, tautulli, FakeSeerr(KID, twin))

    assert user.seerr_user is None
    assert "ids 7, 8" in user.problem


def test_rerun_keeps_settings_and_updates_names(conn):
    seerr = FakeSeerr(ADMIN)
    discover(conn, FakeTautulli((111, "matt")), seerr)
    update_settings(conn, "matt", {"include": True, "max_age": 18})

    (user,) = discover(conn, FakeTautulli((111, "matthew")), seerr)

    assert user.username == "matthew"
    assert user.include is True
    assert user.max_age == 18
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_name_link_survives_renames(conn):
    discover(conn, FakeTautulli((333, "emma")), FakeSeerr(KID))
    renamed = SeerrUser(id=7, plex_id=None, display_name="Em", permissions=32)

    (user,) = discover(conn, FakeTautulli((333, "emma.b")), FakeSeerr(renamed))

    assert user.seerr_user == renamed
    assert user.seerr_link == "name"


def test_plex_match_replaces_name_link(conn):
    tautulli = FakeTautulli((333, "emma"))
    discover(conn, tautulli, FakeSeerr(KID))
    imported = SeerrUser(id=9, plex_id=333, display_name="emma", permissions=32)

    (user,) = discover(conn, tautulli, FakeSeerr(KID, imported))

    assert user.seerr_user == imported
    assert user.seerr_link == "plex"


def test_name_link_dropped_when_seerr_user_gets_a_plex_account(conn):
    tautulli = FakeTautulli((333, "emma"))
    discover(conn, tautulli, FakeSeerr(KID))
    claimed = SeerrUser(id=7, plex_id=444, display_name="Emma", permissions=32)

    (user,) = discover(conn, tautulli, FakeSeerr(claimed))

    assert user.seerr_user is None
    assert "no matching Seerr user" in user.problem


def test_link_lost_when_seerr_user_removed(conn):
    tautulli = FakeTautulli((333, "emma"))
    discover(conn, tautulli, FakeSeerr(KID))

    (user,) = discover(conn, tautulli, FakeSeerr())

    assert user.seerr_user is None
    assert user.problem is not None
    assert conn.execute("SELECT seerr_user_id FROM users").fetchone()[0] is None


def test_manual_link_survives_discovery(conn):
    other = SeerrUser(id=9, plex_id=None, display_name="kiddo", permissions=32)
    tautulli = FakeTautulli((333, "emma"))
    seerr = FakeSeerr(KID, other)
    discover(conn, tautulli, seerr)

    link(conn, seerr, "emma", 9)
    (user,) = discover(conn, tautulli, seerr)

    assert user.seerr_user == other
    assert user.seerr_link == "manual"


def test_manual_link_to_deleted_seerr_user_is_reported(conn):
    other = SeerrUser(id=9, plex_id=None, display_name="kiddo", permissions=32)
    tautulli = FakeTautulli((333, "emma"))
    discover(conn, tautulli, FakeSeerr(other))
    link(conn, FakeSeerr(other), "emma", 9)

    (user,) = discover(conn, tautulli, FakeSeerr(KID))

    assert user.seerr_user is None
    assert user.seerr_link == "manual"
    assert "9 no longer exists" in user.problem


def test_link_rejects_unknown_seerr_user(conn):
    discover(conn, FakeTautulli((333, "emma")), FakeSeerr())

    with pytest.raises(UserError, match="no user with id 9"):
        link(conn, FakeSeerr(KID), "emma", 9)


def test_users_gone_from_tautulli_are_not_returned(conn):
    seerr = FakeSeerr(ADMIN, PARTNER)
    discover(conn, FakeTautulli((111, "matt"), (222, "sam")), seerr)

    household = discover(conn, FakeTautulli((111, "matt")), seerr)

    assert [u.username for u in household] == ["matt"]
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_local_and_inactive_users_are_skipped(conn):
    tautulli = FakeTautulli((0, "Local"), (111, "matt"), (444, "old"), inactive={444})

    household = discover(conn, tautulli, FakeSeerr(ADMIN))

    assert [u.username for u in household] == ["matt"]


def test_returning_user_keeps_settings(conn):
    seerr = FakeSeerr(KID)
    discover(conn, FakeTautulli((333, "emma")), seerr)
    update_settings(conn, "emma", {"include": True, "max_age": 10})

    assert discover(conn, FakeTautulli((333, "emma"), inactive={333}), seerr) == []
    (user,) = discover(conn, FakeTautulli((333, "emma")), seerr)

    assert user.include is True
    assert user.max_age == 10


def test_update_settings_clears_with_none(conn):
    discover(conn, FakeTautulli((333, "Emma")), FakeSeerr(KID))
    update_settings(conn, "emma", {"include": True, "max_age": 10, "picks_per_run": 5})
    update_settings(conn, "Emma", {"max_age": None})

    row = conn.execute("SELECT include, max_age, picks_per_run FROM users").fetchone()
    assert tuple(row) == (1, None, 5)


def test_update_settings_unknown_user(conn):
    with pytest.raises(UserError, match="Run `handpicked users`"):
        update_settings(conn, "nobody", {"include": True})


def test_update_settings_rejects_other_columns(conn):
    discover(conn, FakeTautulli((111, "matt")), FakeSeerr(ADMIN))

    with pytest.raises(ValueError):
        update_settings(conn, "matt", {"seerr_user_id": 5})


def test_exact_case_breaks_a_tie(conn):
    discover(conn, FakeTautulli((1, "Sam"), (2, "sam")), FakeSeerr())

    update_settings(conn, "sam", {"include": True})

    assert conn.execute("SELECT user_id FROM users WHERE include").fetchone()[0] == 2
    with pytest.raises(UserError, match="Several users"):
        update_settings(conn, "SAM", {"include": True})
