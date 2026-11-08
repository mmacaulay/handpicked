"""Give each member a private "Picked for <name>" row on their Plex Home.

Each member's row is a collection with a title and label of their own in each
movie and TV library, hidden from every other member by a `label!=` rule in their share
filter. A row is promoted to members' Homes only once no one else can see it.
See open question 1 in docs/V2.md.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from handpicked.plex import Plex, PlexError, Promotion, Section, Share, hub_id

LABEL_PREFIX = "handpicked-"
OWN_HOME = Promotion(own_home=True, shared_home=False)
SHARED_HOME = Promotion(own_home=False, shared_home=True)

# Plex stores a filter's values URL-encoded and comma-separated.
_VALUE_SEPARATOR = re.compile(r"%2C|,", re.IGNORECASE)
# Exactly the labels from label(), so an admin's own `handpicked-faves` is
# left alone.
_OURS = re.compile(rf"{LABEL_PREFIX}[0-9]+")


def label(user_id: int) -> str:
    return f"{LABEL_PREFIX}{user_id}"


def title(user_id: int, names: dict[int, str]) -> str:
    """A row's title, which must be the member's own when it's made: Plex
    gives collections made with one title one tag, so an item added to one is
    in all of them, and renaming doesn't separate them. A row made before a
    member changed their username keeps the old one."""
    return f"Picked for {names.get(user_id, f'Plex user {user_id}')}"


def unsupported(raw: str) -> str | None:
    """Why Handpicked won't change this filter, or None if it can.

    Only exclude conditions joined by `&` are changed: `|` doesn't bind
    looser than `&` in Plex, and an allow list would hide the member's own
    row, which has no rating.
    """
    if "|" in raw:
        return "it uses OR (|)"
    for condition in _conditions(raw):
        name, op, _ = condition.partition("=")
        if not op:
            return f"it has a condition Handpicked can't read ({condition})"
        if name in ("contentRating", "label"):
            return f"it's an allow list ({name}=)"
        if name not in ("contentRating!", "label!"):
            return f"it filters on {name.rstrip('!')}"
    return None


def labels(raw: str) -> set[str]:
    """Handpicked's labels in a filter's `label!=` rules, decoded and
    case-folded: Plex saves labels with a capital first letter."""
    return {
        token
        for condition in _conditions(raw)
        if condition.startswith("label!=")
        for token in map(_decoded, _tokens(condition))
        if _OURS.fullmatch(token)
    }


def with_labels(raw: str, wanted: set[str]) -> str:
    """The filter with Handpicked's label rules set to `wanted`.

    Every condition and label Handpicked didn't write is kept exactly as
    stored. Decoding and encoding one again could change it: a `+` read
    back as a space would drop a rating limit.
    """
    kept = []
    for condition in _conditions(raw):
        if not condition.startswith("label!="):
            kept.append(condition)
            continue
        tokens = _tokens(condition)
        others = [t for t in tokens if not _OURS.fullmatch(_decoded(t))]
        if len(others) == len(tokens):
            kept.append(condition)
        elif others:
            kept.append("label!=" + "%2C".join(others))
    if wanted:
        kept.append("label!=" + "%2C".join(sorted(wanted)))
    return "&".join(kept)


def _conditions(raw: str) -> list[str]:
    return [c for c in raw.split("&") if c]


def _tokens(condition: str) -> list[str]:
    return _VALUE_SEPARATOR.split(condition.partition("=")[2])


def _decoded(token: str) -> str:
    return unquote(token).casefold()


@dataclass(frozen=True)
class Row:
    user_id: int
    section: Section
    key: str | None  # None when a dry run would make it


@dataclass
class Rows:
    """What one run did to members' rows, or would have in a dry run."""

    created: list[Row] = field(default_factory=list)
    rules: list[int] = field(default_factory=list)  # members whose filters changed
    promoted: list[Row] = field(default_factory=list)
    demoted: list[tuple[Row, list[int]]] = field(default_factory=list)
    waiting: list[tuple[Row, list[int]]] = field(default_factory=list)
    holding: dict[int, str] = field(default_factory=dict)  # member: why
    error: str | None = None
    found: list[Row] = field(default_factory=list)  # every row that exists
    # Rows on their member's Home after the run, or that would be in a dry run.
    on_home: list[Row] = field(default_factory=list)


def arrange(
    plex: Plex, include: set[int], names: dict[int, str], dry_run: bool
) -> Rows:
    """Make each included member's rows, hide them from everyone else, and
    promote the ones no one else can see. `names` has every user Handpicked
    has seen, so the row of a member who has left stays hidden. Stops at a
    Plex error, keeping what it got to: every step is safe to run again."""
    rows = Rows()
    try:
        _arrange(plex, include, names, dry_run, rows)
    except PlexError as e:
        rows.error = f"Plex error: {e}"
    return rows


def _arrange(
    plex: Plex, include: set[int], names: dict[int, str], dry_run: bool, rows: Rows
) -> None:
    admin = plex.account_id()
    machine_id = plex.server().machine_id
    shares = {share.user_id: share for share in plex.shares(machine_id)}
    presets = plex.age_presets()
    tokens = plex.member_tokens(machine_id)
    sections = plex.sections()

    unchangeable = set()
    for user_id, share in shares.items():
        if reason := _unchangeable(share, presets.get(user_id)):
            unchangeable.add(user_id)
            rows.holding[user_id] = reason
        elif user_id not in tokens:
            rows.holding[user_id] = (
                "haven't accepted the server invite, so their view can't be checked"
            )

    members_rows = []
    for user_id in sorted(include | names.keys() | shares.keys() | {admin}):
        for section in sections:
            found = plex.collections(section, label(user_id))
            if not found:
                continue
            members_rows.append(Row(user_id, section, found[0]))
    have = {(r.user_id, r.section) for r in members_rows}
    for user_id in sorted(include):
        if (user_id not in shares and user_id != admin) or user_id in rows.holding:
            continue
        for section in sections:
            if (user_id, section) in have:
                continue
            key = None
            if not dry_run:
                key = plex.create_collection(
                    section, title(user_id, names), label(user_id)
                )
            row = Row(user_id, section, key)
            rows.created.append(row)
            members_rows.append(row)
    rows.found = [r for r in members_rows if r.key]

    # The admin's row is only on their own Home, so it needs no rules.
    hidden = {label(r.user_id) for r in members_rows if r.user_id != admin}
    for user_id, share in shares.items():
        if user_id in unchangeable:
            continue
        movies, television = (
            with_labels(raw, (labels(raw) | hidden) - {label(user_id)})
            for raw in (share.movies, share.television)
        )
        if (movies, television) != (share.movies, share.television):
            if not dry_run:
                plex.set_filters(user_id, movies, television)
            rows.rules.append(user_id)

    views = {u: plex.as_member(t) for u, t in tokens.items() if u in shares}
    for section in sections:
        section_rows = [r for r in members_rows if r.section == section and r.key]
        if not section_rows:
            continue
        promoted = plex.promotions(section)
        # What each member's Collections tab and Home show.
        shown = {
            u: set(view.collections(section)) | view.home_collections(section)
            for u, view in views.items()
        }
        for row in section_rows:
            seen_by = [
                u for u, keys in shown.items() if u != row.user_id and row.key in keys
            ]
            _promote(plex, row, admin, include, promoted, seen_by, dry_run, rows)


def _unchangeable(share: Share, preset: str | None) -> str | None:
    """Why Handpicked can't give this member rules, or None."""
    if preset:
        return (
            f"have an age preset ({preset}); switch it to None, and exclude"
            " ratings instead"
        )
    for kind, raw in (("movie", share.movies), ("TV", share.television)):
        if problem := unsupported(raw):
            return f"have a {kind} filter Handpicked can't change: {problem}"
    return None


def _promote(
    plex: Plex,
    row: Row,
    admin: int,
    include: set[int],
    promoted: dict[str, Promotion],
    seen_by: list[int],
    dry_run: bool,
    rows: Rows,
) -> None:
    current = promoted.get(hub_id(row.section, row.key))
    if row.user_id == admin:
        if current is None and admin in include:
            if not dry_run:
                plex.promote(row.section, row.key, OWN_HOME)
            rows.promoted.append(row)
        if current is not None or admin in include:
            rows.on_home.append(row)
        return
    if current is not None:
        # A member added since, or a filter edited by hand, can show a
        # promoted row to someone else.
        if seen_by:
            if not dry_run:
                plex.demote(row.section, row.key)
            rows.demoted.append((row, seen_by))
        else:
            rows.on_home.append(row)
        return
    if row.user_id not in include:
        return
    if rows.holding or seen_by:
        rows.waiting.append((row, seen_by))
        return
    if not dry_run:
        plex.promote(row.section, row.key, SHARED_HOME)
    rows.promoted.append(row)
    rows.on_home.append(row)


def describe(rows: Rows, names: dict[int, str], dry_run: bool) -> list[str]:
    """One line per change or problem, for the log and the digest."""

    def did(past: str, present: str) -> str:
        return f"Would {present}" if dry_run else past

    def name(user_id: int) -> str:
        return names.get(user_id, f"Plex user {user_id}")

    def where(row: Row) -> str:
        return f"{name(row.user_id)}'s row in {row.section.title}"

    def who(user_ids: list[int]) -> str:
        return ", ".join(map(name, user_ids))

    # First: these need the admin to change something in Plex.
    lines = [
        f"{name(u)} holds back every member's row: they {reason}"
        for u, reason in rows.holding.items()
    ]
    lines += [f"{did('Made', 'make')} {where(r)}" for r in rows.created]
    if rows.rules:
        lines.append(
            f"{did('Updated', 'update')} the filters of {who(rows.rules)},"
            " to hide other members' rows"
        )
    lines += [f"{did('Promoted', 'promote')} {where(r)}" for r in rows.promoted]
    lines += [
        f"{did('Took', 'take')} {where(r)} off Home screens: {who(seen)} can see it"
        for r, seen in rows.demoted
    ]
    lines += [
        f"Not promoted yet: {where(r)}, {who(seen)} can still see it"
        for r, seen in rows.waiting
        if seen
    ]
    if rows.error:
        lines.append(rows.error)
    return lines
