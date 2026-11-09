"""Put each available pick in its member's Plex row, and take it out once its
outcome settles. See "Pipeline additions" in docs/V2.md."""

import sqlite3
from dataclasses import dataclass, field

from handpicked.outcomes import OUTCOMES
from handpicked.plex import Item, Plex, PlexError
from handpicked.rows import Row, Rows
from handpicked.seerr import Title
from handpicked.suggestions import Change


@dataclass
class Delivery:
    """What one run put in members' rows and took out, or would have."""

    delivered: list[tuple[Change, Row]] = field(default_factory=list)
    undelivered: list[Change] = field(default_factory=list)
    not_found: list[Change] = field(default_factory=list)
    waiting: list[Change] = field(default_factory=list)  # no row it can go in
    error: str | None = None


def deliver(
    conn: sqlite3.Connection,
    plex: Plex,
    rows: Rows,
    changes: list[Change],
    now: int,
    dry_run: bool,
) -> Delivery:
    """Deliver available picks and undeliver settled ones. `changes` are this
    run's status changes, which a dry run hasn't recorded. Each pick is
    recorded as soon as Plex has it, so a Plex error keeps what was done."""
    delivery = Delivery()
    try:
        _deliver(conn, plex, rows, changes, now, dry_run, delivery)
    except PlexError as e:
        delivery.error = f"Plex error: {e}"
    return delivery


def _deliver(
    conn: sqlite3.Connection,
    plex: Plex,
    rows: Rows,
    changes: list[Change],
    now: int,
    dry_run: bool,
    delivery: Delivery,
) -> None:
    status = {c.row_id: c.status for c in changes}
    to_deliver: list[Change] = []
    to_undeliver: list[tuple[Change, str]] = []  # with the item delivered
    for row in conn.execute(
        """
        SELECT * FROM suggestions
        WHERE (
            delivered_at IS NULL
            AND status IN ('requested', 'approved', 'available')
        ) OR (delivered_at IS NOT NULL AND undelivered_at IS NULL)
        ORDER BY id
        """
    ):
        pick = Change.of(row, status.get(row["id"], row["status"]))
        if row["delivered_at"] is None:
            if pick.status == "available":
                to_deliver.append(pick)
        elif pick.status in OUTCOMES:
            to_undeliver.append((pick, row["plex_rating_key"]))
    if not to_deliver and not to_undeliver:
        return

    contents: dict[Row, set[str]] = {}

    def items_in(row: Row) -> set[str]:
        if row not in contents:
            contents[row] = plex.collection_items(row.key)
        return contents[row]

    def record(pick: Change, **columns: object) -> None:
        if not dry_run:
            assignments = ", ".join(f"{name} = :{name}" for name in columns)
            with conn:
                conn.execute(
                    f"UPDATE suggestions SET {assignments} WHERE id = :id",
                    {**columns, "id": pick.row_id},
                )

    on_home = {(r.user_id, r.section.key): r for r in rows.on_home}
    library = plex.library_items() if to_deliver else {}
    machine_id = plex.server().machine_id if to_deliver else ""
    for pick in to_deliver:
        found = library.get(_title(pick), [])
        if not found:
            delivery.not_found.append(pick)
            continue
        # The first library it's in with the member's row on Home, such as
        # the main library over a 4K one.
        target = _first(found, on_home, pick.user_id)
        if target is None:
            delivery.waiting.append(pick)
            continue
        item, row = target
        if not dry_run and item.rating_key not in items_in(row):
            plex.add_to_collection(machine_id, row.key, item.rating_key)
        record(pick, delivered_at=now, plex_rating_key=item.rating_key)
        delivery.delivered.append((pick, row))

    for pick, rating_key in to_undeliver:
        for row in (r for r in rows.found if r.user_id == pick.user_id):
            if rating_key in items_in(row) and not dry_run:
                plex.remove_from_collection(row.key, rating_key)
        # Also when it's already gone: taken out by hand, or deleted from
        # the library.
        record(pick, undelivered_at=now)
        delivery.undelivered.append(pick)


def _title(pick: Change) -> Title:
    return (pick.media_type, pick.tmdb_id)


def _first(
    found: list[Item], on_home: dict[tuple[int, str], Row], user_id: int
) -> tuple[Item, Row] | None:
    for item in found:
        if (row := on_home.get((user_id, item.section_id))) is not None:
            return item, row
    return None


def describe(delivery: Delivery, names: dict[int, str], dry_run: bool) -> list[str]:
    """One line per pick delivered, undelivered or held up, for the log and
    the digest."""

    def did(past: str, present: str) -> str:
        return f"Would {present}" if dry_run else past

    def name(user_id: int) -> str:
        return names.get(user_id, f"Plex user {user_id}")

    def title(pick: Change) -> str:
        return f"{pick.title} ({pick.year or '?'})"

    lines = [
        f"{did('Added', 'add')} {title(p)} to {name(p.user_id)}'s row"
        f" in {row.section.title}"
        for p, row in delivery.delivered
    ]
    lines += [
        f"{did('Took', 'take')} {title(p)} out of {name(p.user_id)}'s row: {p.status}"
        for p in delivery.undelivered
    ]
    lines += [
        f"Not in Plex yet: {title(p)} for {name(p.user_id)}, tried again next run"
        for p in delivery.not_found
    ]
    lines += [
        f"Not delivered yet: {title(p)}, {name(p.user_id)}'s row isn't on their"
        " Home, or someone else can see it"
        for p in delivery.waiting
    ]
    if delivery.error:
        lines.append(delivery.error)
    return lines
