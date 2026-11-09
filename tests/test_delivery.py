import pytest

from handpicked import db
from handpicked.delivery import deliver, describe
from handpicked.plex import Item, PlexError, Section, Server
from handpicked.rows import Row, Rows
from handpicked.suggestions import Change

NOW = 1_000_000
MOVIES = Section("1", "movie", "Movies")
MOVIES_4K = Section("3", "movie", "Movies 4K")
KID = 7


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    with conn:
        conn.execute("INSERT INTO users (user_id, username) VALUES (7, 'kid')")
    yield conn
    conn.close()


def pick(conn, tmdb_id, status="available", delivered=None):
    """A pick, delivered at 1 as the item with rating key `delivered` if given."""
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO suggestions (
                user_id, media_type, tmdb_id, title, year, score, seeds, status,
                proposed_at, requested_at, available_at, delivered_at,
                plex_rating_key
            ) VALUES (?, 'movie', ?, ?, 2020, 1, '[]', ?, 0, 0, 0, ?, ?)
            """,
            (
                KID,
                tmdb_id,
                f"Pick {tmdb_id}",
                status,
                1 if delivered else None,
                delivered or None,
            ),
        )
    return cursor.lastrowid


def delivery_times(conn):
    return {
        r["tmdb_id"]: (r["delivered_at"], r["undelivered_at"])
        for r in conn.execute("SELECT * FROM suggestions")
    }


class FakePlex:
    def __init__(self, library, collections):
        self.library = library  # tmdb id: [Item]
        self.collections = collections  # key: set of rating keys
        self.writes = []

    def server(self):
        return Server((1, 43, 5, 1), "abc", True)

    def library_items(self):
        return {("movie", t): items for t, items in self.library.items()}

    def collection_items(self, key):
        return set(self.collections[key])

    def add_to_collection(self, machine_id, key, rating_key):
        self.writes.append(("add", key, rating_key))
        self.collections[key].add(rating_key)

    def remove_from_collection(self, key, rating_key):
        self.writes.append(("remove", key, rating_key))
        self.collections[key].discard(rating_key)


class View:
    """The server as the member sees it, with `hidden` items held back by
    their rating limits."""

    def __init__(self, hidden=()):
        self.hidden = set(hidden)

    def has_item(self, rating_key):
        return rating_key not in self.hidden


ROW = Row(KID, MOVIES, "100")
ON_HOME = Rows(found=[ROW], on_home=[ROW], views={KID: View()})


def test_an_available_pick_goes_in_its_members_row(conn):
    pick(conn, 1)
    plex = FakePlex({1: [Item("1", "50")]}, {"100": set()})

    result = deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert plex.writes == [("add", "100", "50")]
    assert delivery_times(conn) == {1: (NOW, None)}
    assert conn.execute("SELECT plex_rating_key FROM suggestions").fetchone()[0] == "50"
    assert describe(result, {KID: "kid"}, dry_run=False) == [
        "Added Pick 1 (2020) to kid's row in Movies"
    ]

    plex.writes.clear()
    assert deliver(conn, plex, ON_HOME, [], NOW + 1, dry_run=False).delivered == []
    assert plex.writes == []


def test_a_pick_already_in_the_row_is_recorded_without_adding_it_again(conn):
    pick(conn, 1)
    plex = FakePlex({1: [Item("1", "50")]}, {"100": {"50"}})

    deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert plex.writes == []
    assert delivery_times(conn) == {1: (NOW, None)}


def test_a_pick_not_in_plex_is_tried_again_next_run(conn):
    pick(conn, 1)
    plex = FakePlex({}, {"100": set()})

    first = deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert delivery_times(conn) == {1: (None, None)}
    assert describe(first, {KID: "kid"}, dry_run=False) == [
        "Not in Plex yet: Pick 1 (2020) for kid, tried again next run"
    ]

    plex.library[1] = [Item("1", "50")]
    second = deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert [p.tmdb_id for p, _ in second.delivered] == [1]


def test_a_pick_waits_while_its_members_row_isnt_on_home(conn):
    pick(conn, 1)
    plex = FakePlex({1: [Item("1", "50")]}, {"100": set()})

    result = deliver(conn, plex, Rows(found=[ROW]), [], NOW, dry_run=False)

    assert plex.writes == []
    assert delivery_times(conn) == {1: (None, None)}
    assert describe(result, {KID: "kid"}, dry_run=False) == [
        "Not delivered yet: Pick 1 (2020), kid's row isn't on their Home,"
        " or someone else can see it"
    ]


def test_a_pick_its_member_cant_see_is_held_back_and_tried_again(conn):
    pick(conn, 1)
    plex = FakePlex({1: [Item("1", "50", "ca/R")]}, {"100": set()})
    view = View(hidden={"50"})
    rows = Rows(found=[ROW], on_home=[ROW], views={KID: view})

    first = deliver(conn, plex, rows, [], NOW, dry_run=False)

    assert plex.writes == []
    assert delivery_times(conn) == {1: (None, None)}
    assert describe(first, {KID: "kid"}, dry_run=False) == [
        "Hidden from kid in Plex: Pick 1 (2020), Plex rates it ca/R, tried again"
        " next run"
    ]

    # The admin changes the limit, or Plex's rating.
    view.hidden.clear()
    second = deliver(conn, plex, rows, [], NOW, dry_run=False)

    assert [p.tmdb_id for p, _ in second.delivered] == [1]


def test_a_dry_run_reports_a_hidden_pick_the_same_way(conn):
    pick(conn, 1)
    plex = FakePlex({1: [Item("1", "50")]}, {"100": set()})
    rows = Rows(found=[ROW], on_home=[ROW], views={KID: View(hidden={"50"})})

    result = deliver(conn, plex, rows, [], NOW, dry_run=True)

    assert describe(result, {KID: "kid"}, dry_run=True) == [
        "Hidden from kid in Plex: Pick 1 (2020), Plex has no rating for it,"
        " tried again next run"
    ]


def test_a_pick_waits_for_a_member_yet_to_accept_the_invite(conn):
    pick(conn, 1)
    plex = FakePlex({1: [Item("1", "50")]}, {"100": set()})

    result = deliver(
        conn, plex, Rows(found=[ROW], on_home=[ROW]), [], NOW, dry_run=False
    )

    assert plex.writes == [] and [p.tmdb_id for p in result.waiting] == [1]


def test_a_title_in_two_libraries_goes_in_the_first_with_a_row_on_home(conn):
    pick(conn, 1)
    pick(conn, 2)
    row_4k = Row(KID, MOVIES_4K, "300")
    plex = FakePlex(
        {1: [Item("1", "50"), Item("3", "51")], 2: [Item("3", "52")]},
        {"100": set(), "300": set()},
    )

    deliver(
        conn,
        plex,
        Rows(found=[ROW, row_4k], on_home=[ROW, row_4k], views={KID: View()}),
        [],
        NOW,
        dry_run=False,
    )

    assert plex.writes == [("add", "100", "50"), ("add", "300", "52")]


def test_a_hidden_copy_gives_way_to_one_in_another_library_the_member_sees(conn):
    pick(conn, 1)
    row_4k = Row(KID, MOVIES_4K, "300")
    plex = FakePlex(
        {1: [Item("3", "51"), Item("1", "50")]}, {"100": set(), "300": set()}
    )
    rows = Rows(
        found=[ROW, row_4k], on_home=[ROW, row_4k], views={KID: View(hidden={"51"})}
    )

    result = deliver(conn, plex, rows, [], NOW, dry_run=False)

    assert plex.writes == [("add", "100", "50")]
    assert [row for _, row in result.delivered] == [ROW] and result.hidden == []
    assert conn.execute("SELECT plex_rating_key FROM suggestions").fetchone()[0] == "50"


@pytest.mark.parametrize("status", ["watched", "abandoned", "ignored"])
def test_a_settled_pick_is_taken_out(conn, status):
    pick(conn, 1, status, delivered="50")
    # Matched again in Plex since: the item delivered is the one taken out.
    plex = FakePlex({1: [Item("1", "99")]}, {"100": {"50", "60"}})

    result = deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert plex.writes == [("remove", "100", "50")]
    assert delivery_times(conn) == {1: (1, NOW)}
    assert describe(result, {KID: "kid"}, dry_run=False) == [
        f"Took Pick 1 (2020) out of kid's row: {status}"
    ]


def test_a_settled_pick_already_gone_is_recorded(conn):
    pick(conn, 1, "watched", delivered="50")
    plex = FakePlex({}, {"100": set()})

    deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert plex.writes == []
    assert delivery_times(conn) == {1: (1, NOW)}


def test_a_pick_settled_before_delivery_is_left_alone(conn):
    pick(conn, 1, "watched")
    plex = FakePlex({1: [Item("1", "50")]}, {"100": set()})

    result = deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert plex.writes == [] and describe(result, {}, dry_run=False) == []


def test_a_dry_run_uses_this_runs_changes_and_writes_nothing(conn):
    # Neither change is recorded in a dry run.
    became_available = pick(conn, 1, "approved")
    was_ignored = pick(conn, 2, delivered="51")
    plex = FakePlex({1: [Item("1", "50")], 2: [Item("1", "51")]}, {"100": {"51"}})
    rows = conn.execute("SELECT * FROM suggestions ORDER BY id").fetchall()
    changes = [Change.of(rows[0], "available"), Change.of(rows[1], "ignored")]
    assert [c.row_id for c in changes] == [became_available, was_ignored]

    result = deliver(conn, plex, ON_HOME, changes, NOW, dry_run=True)

    assert plex.writes == []
    assert delivery_times(conn) == {1: (None, None), 2: (1, None)}
    assert describe(result, {KID: "kid"}, dry_run=True) == [
        "Would add Pick 1 (2020) to kid's row in Movies",
        "Would take Pick 2 (2020) out of kid's row: ignored",
    ]


def test_a_plex_error_keeps_what_was_delivered(conn, monkeypatch):
    pick(conn, 1)
    pick(conn, 2)
    plex = FakePlex({1: [Item("1", "50")], 2: [Item("1", "51")]}, {"100": set()})
    add = plex.add_to_collection

    def fails_second_time(machine_id, key, rating_key):
        if rating_key == "51":
            raise PlexError("http://plex/library/collections/100/items: HTTP 500")
        add(machine_id, key, rating_key)

    monkeypatch.setattr(plex, "add_to_collection", fails_second_time)

    result = deliver(conn, plex, ON_HOME, [], NOW, dry_run=False)

    assert delivery_times(conn) == {1: (NOW, None), 2: (None, None)}
    assert result.error == (
        "Plex error: http://plex/library/collections/100/items: HTTP 500"
    )
