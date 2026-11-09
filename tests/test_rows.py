from dataclasses import replace

import pytest

from handpicked.plex import Account, PlexError, Section, Server, Share, hub_id
from handpicked.rows import (
    OWN_HOME,
    SHARED_HOME,
    arrange,
    describe,
    labels,
    unsupported,
    with_labels,
)

# kid-1's movie and TV filters, as Plex stores them.
KID_MOVIES = "contentRating!=ca%2F18A%2CR%2Cca%2FR"
KID_TV = "contentRating!=ca%2F18%2B%2Cca%2F18A%2CTV-MA"


@pytest.mark.parametrize("raw", ["", KID_MOVIES, f"{KID_TV}&label!=x%2Cy"])
def test_exclude_conditions_joined_by_and_can_be_changed(raw):
    assert unsupported(raw) is None


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("contentRating!=R|label!=x", "it uses OR (|)"),
        ("contentRating=G%2CPG", "it's an allow list (contentRating=)"),
        ("label=kids", "it's an allow list (label=)"),
        ("genre!=Horror", "it filters on genre"),
        ("contentRating!=R&year", "it has a condition Handpicked can't read (year)"),
    ],
)
def test_other_filters_are_left_alone(raw, reason):
    assert unsupported(raw) == reason


def test_labels_are_found_decoded_and_case_folded():
    raw = f"{KID_MOVIES}&label!=Handpicked-1%2Ckids&label!=handpicked%2D2,HANDPICKED-3"

    assert labels(raw) == {"handpicked-1", "handpicked-2", "handpicked-3"}


def test_adding_labels_keeps_every_other_condition_byte_for_byte():
    raw = with_labels(KID_TV, {"handpicked-2", "handpicked-1"})

    assert raw == f"{KID_TV}&label!=handpicked-1%2Chandpicked-2"
    assert "%2B" in raw  # 18+ is still encoded, so isn't read as a space


def test_labels_already_there_in_another_case_are_kept_as_one_rule():
    raw = f"{KID_MOVIES}&label!=Handpicked-1"

    assert labels(raw) == {"handpicked-1"}
    assert with_labels(raw, {"handpicked-1"}) == f"{KID_MOVIES}&label!=handpicked-1"


def test_other_labels_in_a_mixed_rule_keep_their_raw_text():
    raw = "label!=Kids%20Only%2CHandpicked-1&contentRating!=R"

    assert with_labels(raw, {"handpicked-2"}) == (
        "label!=Kids%20Only&contentRating!=R&label!=handpicked-2"
    )


def test_removing_every_handpicked_label_leaves_the_rest():
    assert with_labels(f"{KID_MOVIES}&label!=handpicked-1", set()) == KID_MOVIES
    assert with_labels("", set()) == ""


def test_an_admins_own_label_with_the_prefix_is_left_alone():
    raw = "label!=Handpicked-Faves%2CHandpicked-1"

    assert labels(raw) == {"handpicked-1"}
    assert with_labels(raw, set()) == "label!=Handpicked-Faves"


MOVIES = Section("1", "movie", "Movies")
TV = Section("2", "tv", "TV Shows")
ADMIN, KID, ADULT, NEW = 1, 2, 3, 4


class FakePlex:
    """Plex as the rows step sees it. A member sees a collection unless their
    filter hides its label, and filters apply at once unless `late`."""

    def __init__(self, shares, presets=None, tokens=None, late=()):
        self.shares_ = {s.user_id: s for s in shares}
        self.presets = presets or {}
        self.tokens = (
            tokens if tokens is not None else {u: f"t{u}" for u in self.shares_}
        )
        self.late = set(late)
        self.applied = {u: (s.movies, s.television) for u, s in self.shares_.items()}
        self.collections_ = {}  # key: (section, label)
        self.titles = {}  # key: title
        self.made = 0
        self.promoted = {}  # hub id: Promotion
        self.writes = []
        self.viewer = None

    # Reads, as the admin unless self.viewer is a member.
    def account(self):
        return Account(ADMIN, "Sam Admin")

    def server(self):
        return Server((1, 43, 5, 1), "abc", True)

    def shares(self, machine_id):
        return list(self.shares_.values())

    def age_presets(self):
        return dict(self.presets)

    def member_tokens(self, machine_id):
        return dict(self.tokens)

    def sections(self):
        return [MOVIES, TV]

    def collections(self, section, label=None):
        return {
            key: self.titles[key]
            for key, (s, lab) in self.collections_.items()
            if s == section
            and (label is None or lab.casefold() == label.casefold())
            and (self.viewer is None or self._visible(section, lab))
        }

    def home_collections(self, section):
        return {
            key
            for key, (s, lab) in self.collections_.items()
            if s == section
            and (promotion := self.promoted.get(hub_id(s, key)))
            and promotion.shared_home
            and self._visible(s, lab)
        }

    def promotions(self, section):
        return {
            hub: p
            for hub, p in self.promoted.items()
            if hub.split(".")[2] == section.key
        }

    def as_member(self, token):
        view = FakePlex.__new__(FakePlex)
        view.__dict__ = self.__dict__ | {
            "viewer": next(u for u, t in self.tokens.items() if t == token)
        }
        return view

    def _visible(self, section, lab):
        movies, television = self.applied[self.viewer]
        raw = movies if section.media_type == "movie" else television
        return lab.casefold() not in labels(raw)

    # Writes
    def create_collection(self, section, title, label):
        self.made += 1
        key = str(99 + self.made)
        self.collections_[key] = (section, label.capitalize())
        self.titles[key] = title
        self.writes.append(("create", section.key, label))
        return key

    def rename_collection(self, section, key, title):
        self.writes.append(("rename", section.key, key, title))
        self.titles[key] = title

    def set_filters(self, user_id, movies, television):
        self.writes.append(("filters", user_id, movies, television))
        self.shares_[user_id] = replace(
            self.shares_[user_id], movies=movies, television=television
        )
        if user_id not in self.late:
            self.applied[user_id] = (movies, television)

    def promote(self, section, key, promotion):
        self.writes.append(("promote", section.key, key, promotion))
        self.promoted[hub_id(section, key)] = promotion

    def demote(self, section, key):
        self.writes.append(("demote", section.key, key))
        del self.promoted[hub_id(section, key)]


def household(**kwargs):
    return FakePlex(
        [Share(KID, KID_MOVIES, KID_TV, "Kid"), Share(ADULT, "", "", "Alex Adult")],
        **kwargs,
    )


def test_each_included_member_gets_a_hidden_promoted_row_in_each_library():
    plex = household()

    result = arrange(plex, {KID, ADULT}, {}, dry_run=False)

    assert [(r.user_id, r.section) for r in result.created] == [
        (KID, MOVIES),
        (KID, TV),
        (ADULT, MOVIES),
        (ADULT, TV),
    ]
    assert plex.shares_[KID] == Share(
        KID,
        f"{KID_MOVIES}&label!=handpicked-3",
        f"{KID_TV}&label!=handpicked-3",
        "Kid",
    )
    assert plex.shares_[ADULT] == Share(
        ADULT, "label!=handpicked-2", "label!=handpicked-2", "Alex Adult"
    )
    assert sorted(result.rules) == [KID, ADULT]
    assert len(result.promoted) == 4
    assert set(plex.promoted.values()) == {SHARED_HOME}
    assert result.waiting == [] and result.holding == {}


def test_rows_on_home_are_reported_for_delivery():
    plex = household(late={ADULT})
    first = arrange(plex, {ADMIN, KID}, {}, dry_run=False)

    # Every row waits on the adult's late rule, the admin's on their Home.
    assert first.on_home == []
    assert {r.user_id for r, _ in first.waiting} == {ADMIN, KID}
    assert set(plex.promoted.values()) == {OWN_HOME}
    assert len(first.found) == 4

    plex.applied[ADULT] = (plex.shares_[ADULT].movies, plex.shares_[ADULT].television)
    second = arrange(plex, {ADMIN, KID}, {}, dry_run=False)

    assert {r.user_id for r in second.on_home} == {ADMIN, KID}
    assert len(second.on_home) == 4

    # A row taken off Home isn't filled, and nor is the admin's, which stays
    # on their Home.
    plex.applied[ADULT] = ("", "")
    plex.shares_[ADULT] = Share(ADULT, "", "", "Alex Adult")
    plex.late.add(ADULT)
    third = arrange(plex, {ADMIN, KID}, {}, dry_run=False)

    assert third.on_home == []
    assert {r.user_id for r, _ in third.demoted} == {KID}
    assert {r.user_id for r, _ in third.waiting} == {ADMIN}


def test_rows_are_titled_by_library_and_first_name():
    plex = household()

    arrange(plex, {ADMIN, KID, ADULT}, {}, dry_run=False)

    assert sorted(plex.titles.values()) == [
        "Movies picked for Alex",
        "Movies picked for Kid",
        "Movies picked for Sam",
        "TV Shows picked for Alex",
        "TV Shows picked for Kid",
        "TV Shows picked for Sam",
    ]


def test_the_admins_row_isnt_filled_while_a_held_member_could_see_it():
    plex = household(presets={ADULT: "teen"})

    result = arrange(plex, {ADMIN}, {}, dry_run=False)

    assert set(plex.promoted.values()) == {OWN_HOME}
    assert result.on_home == []
    assert [(r.user_id, seen) for r, seen in result.waiting] == [
        (ADMIN, [ADULT]),
        (ADMIN, [ADULT]),
    ]


def test_members_who_share_a_first_name_share_a_title():
    plex = FakePlex([Share(KID, "", "", "Sam Kid")])

    arrange(plex, {ADMIN, KID}, {}, dry_run=False)

    assert sorted(set(plex.titles.values())) == [
        "Movies picked for Sam",
        "TV Shows picked for Sam",
    ]


def test_a_member_without_a_plex_name_is_named_by_username():
    plex = FakePlex([Share(KID, "", "")])

    arrange(plex, {KID}, {KID: "kid2"}, dry_run=False)

    assert sorted(plex.titles.values()) == [
        "Movies picked for kid2",
        "TV Shows picked for kid2",
    ]


def test_a_row_is_renamed_when_its_members_name_changes():
    plex = household()
    arrange(plex, {KID}, {}, dry_run=False)
    keys = set(plex.collections_)
    plex.writes.clear()
    plex.shares_[KID] = replace(plex.shares_[KID], title="Robin Kid")

    result = arrange(plex, {KID}, {KID: "kid"}, dry_run=False)

    assert plex.writes == [
        ("rename", "1", "100", "Movies picked for Robin"),
        ("rename", "2", "101", "TV Shows picked for Robin"),
    ]
    assert set(plex.collections_) == keys and len(result.on_home) == 2
    assert describe(result, {KID: "kid"}, dry_run=False) == [
        'Renamed kid\'s row in Movies to "Movies picked for Robin"',
        'Renamed kid\'s row in TV Shows to "TV Shows picked for Robin"',
    ]


def test_a_dry_run_reports_a_rename_without_making_it():
    plex = household()
    arrange(plex, {KID}, {}, dry_run=False)
    plex.writes.clear()
    plex.shares_[KID] = replace(plex.shares_[KID], title="Robin")

    result = arrange(plex, {KID}, {KID: "kid"}, dry_run=True)

    assert plex.writes == []
    assert describe(result, {KID: "kid"}, dry_run=True)[0] == (
        'Would rename kid\'s row in Movies to "Movies picked for Robin"'
    )


def test_a_departed_members_row_keeps_its_title():
    plex = household()
    arrange(plex, {KID, ADULT}, {}, dry_run=False)
    del plex.shares_[KID], plex.tokens[KID]
    plex.writes.clear()

    arrange(plex, {ADULT}, {KID: "kid", ADULT: "adult"}, dry_run=False)

    assert plex.writes == []


def test_a_row_shown_only_on_someones_home_is_taken_off(monkeypatch):
    plex = household()
    arrange(plex, {KID}, {}, dry_run=False)
    kid_rows = set(plex.collections_)
    monkeypatch.setattr(
        FakePlex,
        "home_collections",
        lambda self, section: kid_rows if self.viewer == ADULT else set(),
    )

    result = arrange(plex, {KID}, {}, dry_run=False)

    assert [seen for _, seen in result.demoted] == [[ADULT], [ADULT]]


def test_a_second_run_changes_nothing():
    plex = household()
    arrange(plex, {KID, ADULT}, {}, dry_run=False)
    plex.writes.clear()

    result = arrange(plex, {KID, ADULT}, {}, dry_run=False)

    assert plex.writes == []
    assert describe(result, {}, dry_run=False) == []


def test_a_row_waits_while_a_late_filter_still_shows_it():
    plex = household(late={ADULT})

    first = arrange(plex, {KID}, {}, dry_run=False)

    assert first.promoted == []
    assert [(r.user_id, seen) for r, seen in first.waiting] == [
        (KID, [ADULT]),
        (KID, [ADULT]),
    ]
    assert describe(first, {KID: "kid", ADULT: "adult"}, dry_run=False)[-1] == (
        "Holding picks back from kid's row in TV Shows: adult can still see it"
    )

    plex.applied[ADULT] = (plex.shares_[ADULT].movies, plex.shares_[ADULT].television)
    second = arrange(plex, {KID}, {}, dry_run=False)

    assert len(second.promoted) == 2
    assert second.rules == []


def test_the_admins_row_is_on_their_own_home_and_hidden_from_everyone():
    plex = household()

    result = arrange(plex, {ADMIN}, {}, dry_run=False)

    assert [r.user_id for r in result.created] == [ADMIN, ADMIN]
    assert set(plex.promoted.values()) == {OWN_HOME}
    assert sorted(result.rules) == [KID, ADULT]
    assert plex.shares_[ADULT] == Share(
        ADULT, "label!=handpicked-1", "label!=handpicked-1", "Alex Adult"
    )


def test_a_member_never_hides_their_own_row():
    plex = FakePlex([Share(KID, "label!=Handpicked-2", ""), Share(ADULT, "", "")])

    arrange(plex, {KID}, {}, dry_run=False)

    assert plex.shares_[KID] == Share(KID, "", "")
    assert plex.shares_[ADULT] == Share(
        ADULT, "label!=handpicked-2", "label!=handpicked-2"
    )


@pytest.mark.parametrize(
    "plex, reason",
    [
        (
            household(presets={ADULT: "older_kid"}),
            "have an age preset (older_kid)",
        ),
        (
            FakePlex([Share(KID, KID_MOVIES, KID_TV), Share(ADULT, "", "genre!=x")]),
            "have a TV filter Handpicked can't change: it filters on genre",
        ),
    ],
)
def test_a_member_handpicked_cant_change_holds_back_every_row(plex, reason):
    result = arrange(plex, {KID}, {}, dry_run=False)

    assert list(result.holding) == [ADULT]
    assert reason in result.holding[ADULT]
    assert result.promoted == []
    assert len(result.waiting) == 2
    assert all(w[0] != "promote" for w in plex.writes)
    assert ("filters", ADULT) not in [w[:2] for w in plex.writes]
    assert describe(result, {ADULT: "adult"}, dry_run=False)[0] == (
        "adult holds back every member's row: they " + result.holding[ADULT]
    )


def test_a_member_yet_to_accept_gets_rules_and_holds_back_every_row():
    plex = household(tokens={KID: "t2"})

    result = arrange(plex, {KID}, {}, dry_run=False)

    assert "haven't accepted the server invite" in result.holding[ADULT]
    assert plex.shares_[ADULT] == Share(
        ADULT, "label!=handpicked-2", "label!=handpicked-2", "Alex Adult"
    )
    assert result.promoted == [] and len(result.waiting) == 2


def test_a_held_member_gets_no_row():
    plex = household(presets={ADULT: "teen"})

    result = arrange(plex, {KID, ADULT}, {}, dry_run=False)

    assert [r.user_id for r in result.created] == [KID, KID]


def test_a_departed_members_row_stays_hidden_from_new_members():
    plex = household()
    arrange(plex, {KID, ADULT}, {KID: "kid", ADULT: "adult"}, dry_run=False)
    # The kid leaves, and a new member joins whose rules apply late.
    del plex.shares_[KID], plex.tokens[KID]
    plex.shares_[NEW] = Share(NEW, "", "")
    plex.applied[NEW] = ("", "")
    plex.tokens[NEW] = "t4"
    plex.late.add(NEW)

    result = arrange(plex, {ADULT}, {KID: "kid", ADULT: "adult"}, dry_run=False)

    assert labels(plex.shares_[NEW].movies) == {"handpicked-2", "handpicked-3"}
    assert sorted((r.user_id, seen) for r, seen in result.demoted) == [
        (KID, [NEW]),
        (KID, [NEW]),
        (ADULT, [NEW]),
        (ADULT, [NEW]),
    ]


def test_a_promoted_row_someone_can_see_is_taken_off_home_screens():
    plex = household()
    arrange(plex, {KID}, {}, dry_run=False)
    # A filter edited by hand drops the rule.
    plex.applied[ADULT] = ("", "")
    plex.shares_[ADULT] = Share(ADULT, "", "", "Alex Adult")
    plex.late.add(ADULT)

    result = arrange(plex, {KID}, {}, dry_run=False)

    assert [(r.section, seen) for r, seen in result.demoted] == [
        (MOVIES, [ADULT]),
        (TV, [ADULT]),
    ]
    assert plex.promoted == {}
    assert result.rules == [ADULT]


def test_an_excluded_members_existing_row_stays_hidden():
    plex = household()
    arrange(plex, {KID, ADULT}, {}, dry_run=False)
    plex.writes.clear()

    result = arrange(plex, {KID}, {}, dry_run=False)

    assert result.rules == [] and result.created == []
    assert plex.writes == []


def test_an_excluded_members_waiting_row_is_not_promoted():
    plex = household(late={ADULT})
    arrange(plex, {KID}, {KID: "kid"}, dry_run=False)
    plex.applied[ADULT] = (plex.shares_[ADULT].movies, plex.shares_[ADULT].television)

    result = arrange(plex, set(), {KID: "kid"}, dry_run=False)

    assert result.promoted == [] and result.waiting == []
    assert plex.promoted == {}


def test_a_dry_run_reports_and_writes_nothing():
    plex = household()

    result = arrange(plex, {KID}, {}, dry_run=True)

    assert plex.writes == []
    assert [r.key for r in result.created] == [None, None]
    assert result.rules == [ADULT]
    assert describe(result, {KID: "kid", ADULT: "adult"}, dry_run=True) == [
        "Would make kid's row in Movies",
        "Would make kid's row in TV Shows",
        "Would update the filters of adult, to hide other members' rows",
    ]


def test_a_plex_error_keeps_what_was_done(monkeypatch):
    plex = household()

    def refused(*args):
        raise PlexError("plex.tv/users/3: request failed (HTTP 422)", 422)

    monkeypatch.setattr(plex, "set_filters", refused)

    result = arrange(plex, {KID}, {}, dry_run=False)

    assert len(result.created) == 2
    assert result.error == "Plex error: plex.tv/users/3: request failed (HTTP 422)"
    assert describe(result, {}, dry_run=False)[-1] == result.error
