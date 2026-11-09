"""Give each member a private "<library> picked for <name>" row on their Plex
Home.

Each member's row is a collection with a label of their own in each movie and
TV library, hidden from every other member by a `label!=` rule in their share
filter. A row is promoted to members' Homes, and filled, only once no one else
can see it.
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


def title(section: Section, name: str) -> str:
    """Members can share one: Plex.create_collection keeps rows' tags apart."""
    return f"{section.title} picked for {name}"


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
    renamed: list[tuple[Row, str]] = field(default_factory=list)  # with the title
    rules: list[int] = field(default_factory=list)  # members whose filters changed
    promoted: list[Row] = field(default_factory=list)
    demoted: list[tuple[Row, list[int]]] = field(default_factory=list)
    waiting: list[tuple[Row, list[int]]] = field(default_factory=list)
    holding: dict[int, str] = field(default_factory=dict)  # member: why
    error: str | None = None
    found: list[Row] = field(default_factory=list)  # every row that exists
    # Rows delivery can fill: on their member's Home after the run, or that
    # would be in a dry run, and seen by no one else.
    on_home: list[Row] = field(default_factory=list)
    # The server as each member sees it, the admin included. Holds their
    # tokens: keep it in memory only.
    views: dict[int, Plex] = field(default_factory=dict)


def arrange(
    plex: Plex, include: set[int], names: dict[int, str], dry_run: bool
) -> Rows:
    """Make each included member's rows, title them, hide them from everyone
    else, and promote the ones no one else can see. `names` has every user
    Handpicked has seen, by username, so the row of a member who has left
    stays hidden. Stops at a Plex error, keeping what it got to: every step
    is safe to run again."""
    rows = Rows()
    try:
        _arrange(plex, include, names, dry_run, rows)
    except PlexError as e:
        rows.error = f"Plex error: {e}"
    return rows


def _arrange(
    plex: Plex, include: set[int], names: dict[int, str], dry_run: bool, rows: Rows
) -> None:
    account = plex.account()
    admin = account.user_id
    machine_id = plex.server().machine_id
    shares = {share.user_id: share for share in plex.shares(machine_id)}
    # The first name on plex.tv, or the username. Members who have left keep
    # their rows' titles.
    full = {u: s.title for u, s in shares.items()} | {admin: account.title}
    first = {
        u: (name.split() or [names.get(u) or f"Plex user {u}"])[0]
        for u, name in full.items()
    }
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
            key, current = next(iter(found.items()))
            row = Row(user_id, section, key)
            members_rows.append(row)
            wanted = title(section, first[user_id]) if user_id in first else current
            if current != wanted:
                if not dry_run:
                    plex.rename_collection(section, key, wanted)
                rows.renamed.append((row, wanted))
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
                    section, title(section, first[user_id]), label(user_id)
                )
            row = Row(user_id, section, key)
            rows.created.append(row)
            members_rows.append(row)
    rows.found = [r for r in members_rows if r.key]

    hidden = {label(r.user_id) for r in members_rows}
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
    rows.views = views | {admin: plex}
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
        # Only ever on the admin's own Home, so it's promoted at once, but
        # it's filled only once no one else can see it.
        if current is None and admin in include:
            if not dry_run:
                plex.promote(row.section, row.key, OWN_HOME)
            rows.promoted.append(row)
        if current is not None or admin in include:
            if seen_by:
                rows.waiting.append((row, seen_by))
            else:
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
    lines += [
        f'{did("Renamed", "rename")} {where(r)} to "{new}"' for r, new in rows.renamed
    ]
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
        f"Holding picks back from {where(r)}: {who(seen)} can still see it"
        for r, seen in rows.waiting
        if seen
    ]
    if rows.error:
        lines.append(rows.error)
    return lines
