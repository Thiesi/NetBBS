"""
Tests for the "Name & details" screen
(`netbbs.net.profile_flow._identity_details_screen`, reached from
`_edit_profile`'s own Name & details field) -- previously
untested; converted onto `edit_resource_draft` alongside the profile
screen itself (issue #160's cursor-nav follow-up).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date

import pytest

from netbbs.attestation import (
    attest_age,
    set_display_name,
    set_display_name_visible,
    set_location,
    set_location_visible,
    set_birthdate,
    set_birthdate_visible,
    set_attestation_link_visible,
    attest_name,
    compute_age,
    get_birthdate,
    get_display_name,
    get_location,
    is_birthdate_visible,
    is_display_name_visible,
    is_location_visible,
    is_verified_badge_visible,
)
from netbbs.attestation import get_attestation
from netbbs.auth.users import create_user
from netbbs.net import profile_flow
from netbbs.net.char_input import HELP_KEY
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def squeezed(text: str) -> str:
    """`text` with runs of spaces collapsed to one.

    Field screens align values into a shared column (#529), so a label and
    its value are separated by as many spaces as that column needs. An
    assertion about *which* value is shown should not also pin the width of
    the column it is shown in.
    """
    return re.sub(r" {2,}", " ", text)


class FakeSession(Session):
    """One ordered input queue serves both `read_key()` and `read_line()`
    -- same shape tests/test_login_flow_sort_preferences_screen.py's own
    FakeSession already established; `read_editor_key` isn't implemented,
    so `edit_resource_draft`'s cursor navigation falls back to plain
    `read_key()`, exactly like every hotkey-only test double."""

    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def test_shows_current_state_with_nothing_set(db, lane, alice):
    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "Display name: (not set)" in squeezed(text)
    assert "Display name visibility: private" in squeezed(text)
    assert "Location: (not set)" in squeezed(text)
    assert "Location visibility: private" in squeezed(text)
    assert "Birthdate: (not set)" in squeezed(text)
    assert "Age visibility: private" in squeezed(text)
    assert "Verified by this node: (none)" in squeezed(text)
    assert "Share verified age over Link: (not verified)" in squeezed(text)
    assert "Share verified name over Link: (not verified)" in squeezed(text)


def test_ctrl_h_shows_real_help_text_for_every_field(db, lane, alice):
    # Dogfood feature request: this screen's five fields previously had
    # no help= authored at all, so Ctrl-H was a discoverable dead end
    # ("No help is available for ... yet" for every one of them).
    # One more page than before issue #596: the two Link-sharing entries now
    # say who receives the value and what withdrawal can and cannot do.
    session = FakeSession([HELP_KEY, " ", " ", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "No help is available" not in text
    assert "self-reported and unverified" in text.lower()
    assert "minimum age to post or join" in text
    # The Link-sharing help used to say the value reaches "a remote node's
    # trust/vouch policy", which design doc §5.5 explicitly denies -- reporter
    # and vouch configuration grant no attestation authority. Issue #584 made
    # the toggle actually do something and corrected the claim with it, so
    # what this pins is the true one: the caller's own gate-passing, and who
    # it does and does not reach.
    assert "trust/vouch policy" not in text
    assert "an age gate can let you in" in text
    assert "requires a verified name can let you in" in text
    # Issue #596 replaced v7.7.0's honest-but-unenforced wording ("any node
    # this one has linked with ... a decision you cannot reverse") with what
    # the node now enforces, and the limit it cannot: a recipient list, and a
    # withdrawal that a node which already copied the value may ignore.
    # The help is drawn in a box, so a sentence is interrupted by the frame
    # at every wrap; drop the frame before reading it as prose.
    flowed = " ".join("".join(ch if ch.isascii() else " " for ch in text).split())
    assert "only to the nodes your SysOp has named" in flowed
    assert "nothing can force a node that already copied the date to" in flowed
    assert "nothing can force a node that already copied the name to" in flowed
    assert "you cannot reverse" not in flowed
    assert "any node this one has linked with" not in flowed


def test_display_name_edit_sets_only_the_value(db, lane, alice):
    session = FakeSession(["0", "1", "Alice W", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_display_name(db, alice) == "Alice W"
    assert is_display_name_visible(db, alice) is False
    assert "Display name: Alice W" in squeezed(_visible(session))


def test_display_name_visibility_is_its_own_toggle(db, lane, alice):
    session = FakeSession(["0", "2", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert is_display_name_visible(db, alice) is True
    assert "Display name visibility: public" in squeezed(_visible(session))


def test_blank_value_prompt_leaves_visibility_untouched(db, lane, alice):
    # Issue #282 regression: the value prompt used to be followed by an
    # unconditional "Show it publicly? [y/N]" whose answer was always
    # written, so pressing Enter twice just to look at a field silently
    # set it private. Each line now opens on its value, so Enter on it
    # keeps the value and touches nothing else.
    set_display_name(db, alice, "Alice W")
    set_display_name_visible(db, alice, True)
    set_location(db, alice, "Retro City")
    set_location_visible(db, alice, True)
    set_birthdate(db, alice, date(2000, 1, 1))
    set_birthdate_visible(db, alice, True)
    session = FakeSession(["0", "1", "Alice W", "0", "3", "Retro City", "0", "5", "2000-01-01", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_display_name(db, alice) == "Alice W"
    assert is_display_name_visible(db, alice) is True
    assert get_location(db, alice) == "Retro City"
    assert is_location_visible(db, alice) is True
    assert get_birthdate(db, alice) == date(2000, 1, 1)
    assert is_birthdate_visible(db, alice) is True
    assert "Show it publicly" not in _written_text(session)


def test_location_edit_and_visibility_toggle(db, lane, alice):
    session = FakeSession(["0", "3", "Retro City", "0", "4", "0", "4", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_location(db, alice) == "Retro City"
    # Two presses of the toggle return to the starting state.
    assert is_location_visible(db, alice) is False
    text = _visible(session)
    assert "Location: Retro City" in squeezed(text)
    assert "Location visibility: public" in squeezed(text)
    assert "Location visibility: private" in squeezed(text)


def test_birthdate_edit_sets_value_and_age(db, lane, alice):
    session = FakeSession(["0", "5", "2000-01-01", "0", "6", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_birthdate(db, alice) == date(2000, 1, 1)
    assert is_birthdate_visible(db, alice) is True
    text = _visible(session)
    assert f"(age {compute_age(date(2000, 1, 1))})" in text
    assert "Age visibility: public" in squeezed(text)


def test_each_value_opens_in_its_own_line_and_esc_keeps_it(db, lane, alice):
    """The prompt used to put "[current] -- new value as YYYY-MM-DD (blank
    to keep, - to clear):" before the cursor, leaving two columns of an
    80-column screen to type a birthdate into. The value now opens in a
    line of its own, the way the SysOp edits it (#1110)."""
    from netbbs.net.char_input import InputCancelled

    set_display_name(db, alice, "Alice W")
    set_birthdate(db, alice, date(2000, 1, 1))
    seeded = []

    class Escaping(FakeSession):
        async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
            seeded.append(kwargs.get("initial"))
            raise InputCancelled

    session = Escaping(["0", "1", "0", "5", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert seeded == ["Alice W", "2000-01-01"]
    assert get_display_name(db, alice) == "Alice W"
    assert get_birthdate(db, alice) == date(2000, 1, 1)
    text = _visible(session)
    assert "Birthdate (YYYY-MM-DD) (Enter saves, blank clears, Esc keeps):" in text
    assert "[2000-01-01]" not in text


def test_birthdate_rejects_an_invalid_date_format(db, lane, alice):
    session = FakeSession(["0", "5", "not-a-date", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_birthdate(db, alice) is None
    assert "Not a valid date" in _written_text(session)


def test_verified_badge_visibility_toggles(db, lane, alice):
    assert is_verified_badge_visible(db, alice) is False  # default
    session = FakeSession(["0", "7", "0", "7", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    # Two presses of a bool toggle return to the starting state.
    assert is_verified_badge_visible(db, alice) is False


def test_verified_summary_shows_attested_attributes(db, lane, alice):
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_age(db, alice, date(1990, 5, 1), verifier=verifier)
    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "Verified by this node: born 1990-05-01" in squeezed(text)
    assert "Share verified age over Link: off" in squeezed(text)
    assert "Share verified name over Link: (not verified)" in squeezed(text)


def test_remote_sharing_rejects_an_attribute_with_no_attestation(db, lane, alice):
    session = FakeSession(["0", "8", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert "No age attestation exists" in _written_text(session)
    assert get_attestation(db, alice, "age") is None


def test_remote_sharing_toggles_both_ways_without_a_question(db, lane, alice):
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)
    session = FakeSession(["0", "9", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_attestation(db, alice, "name").link_visible is True
    # Issue #596: "on" with nobody named to receive it shares nothing, and
    # the caller is shown that beside the value rather than left to wonder.
    assert "Share verified name over Link: on (your SysOp shares with no node yet)" in squeezed(_visible(session))
    assert "Allow this verified" not in _written_text(session)

    # Turning it off is the same single keystroke -- previously the
    # sub-screen toggled off silently but asked a yes/no before turning
    # on (issue #282).
    session = FakeSession(["0", "9", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_attestation(db, alice, "name").link_visible is False
    assert "Share verified name over Link: off" in squeezed(_visible(session))


def test_remote_sharing_refreshes_instead_of_toggling_a_changed_attestation(db, lane, alice):
    # Codex review (PR #284): the screen shows sharing "on", then a SysOp
    # re-attests while it is open -- which clears link_visible (worklog
    # invariant). The caller's press was meant to turn the displayed
    # "on" off; re-reading and blindly negating would instead *enable*
    # sharing of the replacement attestation they have never seen. The
    # toggle must refresh and report, and only act on a second press.
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_age(db, alice, date(1990, 5, 1), verifier=verifier)
    set_attestation_link_visible(db, alice, "age", True)

    class ReattestingSession(FakeSession):
        """Re-attests between the first draw and the first keypress."""

        def __init__(self, inputs):
            super().__init__(inputs)
            self.reattested = False

        async def read_key(self, echo: bool = True) -> str:
            if not self.reattested:
                self.reattested = True
                attest_age(db, alice, date(1991, 6, 2), verifier=verifier)
            return await super().read_key(echo)

    session = ReattestingSession(["0", "8", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "Share verified age over Link: on" in squeezed(text)  # what the caller saw first
    assert "changed since this screen was drawn" in text
    assert get_attestation(db, alice, "age").link_visible is False
    assert get_attestation(db, alice, "age").attested_value == "1991-06-02"
    # The redraw now shows the real state; a second press acts on it.
    session = FakeSession(["0", "8", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_attestation(db, alice, "age").link_visible is True


def test_remote_sharing_reports_an_attestation_removed_since_the_draw(db, lane, alice):
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)

    class RevokingSession(FakeSession):
        def __init__(self, inputs):
            super().__init__(inputs)
            self.revoked = False

        async def read_key(self, echo: bool = True) -> str:
            if not self.revoked:
                self.revoked = True
                # No removal API exists yet (a re-attestation replaces in
                # place); model a SysOp-side deletion at the storage level.
                db.connection.execute(
                    "DELETE FROM user_attestations WHERE subject_user_id = ? AND attribute = 'name'",
                    (alice.id,),
                )
                db.connection.commit()
            return await super().read_key(echo)

    session = RevokingSession(["0", "9", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "was removed since this screen was drawn" in text
    assert "Share verified name over Link: (not verified)" in squeezed(text)
    assert get_attestation(db, alice, "name") is None


def _publish(db):
    """Sign what consent allows, as the sync pass does."""
    import nacl.signing

    from netbbs.identity.keys import Identity, IdentityKind
    from netbbs.link.remote_attestation import reconcile_issued_attestations

    identity = Identity(
        kind=IdentityKind.NODE, label="node", signing_key=nacl.signing.SigningKey(b"\x22" * 32),
        created_at="2026-10-01T00:00:00.000000Z",
    )
    reconcile_issued_attestations(db, identity, home_node_fingerprint="home-node")


def _deliver_to(db, fingerprint):
    """Record that `fingerprint` was sent the current snapshot (issue #632)."""
    from netbbs.link.attestation_delivery import plan_attestation_deliveries, record_attestation_delivery

    _publish(db)
    for plan in plan_attestation_deliveries(db):
        if plan.recipient_fingerprint == fingerprint:
            record_attestation_delivery(
                db, fingerprint, digest=plan.digest, route="relay", final=False, content_ids=plan.content_ids,
            )


def test_remote_sharing_shows_how_many_nodes_it_reaches(db, lane, alice):
    """Issue #596, Decision 4: the caller is told how many, never which.
    Issue #632: and how many were actually sent it, not who could ask."""
    from netbbs.link.remote_attestation import configure_attestation_recipient

    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)
    from netbbs.attestation import set_attestation_link_visible as _share
    _share(db, alice, "name", True)
    configure_attestation_recipient(db, "a" * 32, reason="first")
    _deliver_to(db, "a" * 32)

    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = squeezed(_visible(session))
    assert "Share verified name over Link: on (sent to 1 node)" in text
    assert "a" * 32 not in text

    configure_attestation_recipient(db, "b" * 32, reason="second")
    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert "Share verified name over Link: on (sent to 1 of 2 nodes)" in squeezed(_visible(session))
    _deliver_to(db, "b" * 32)
    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert "Share verified name over Link: on (sent to 2 nodes)" in squeezed(_visible(session))


def test_another_callers_change_does_not_undeliver_this_callers_value(db, lane, alice):
    """Review of #1045: counts are per caller. Bob switching sharing on makes
    every recipient's snapshot out of date, but they still hold Alice's
    value, and her toggle must keep saying so."""
    from netbbs.attestation import set_attestation_link_visible
    from netbbs.link.remote_attestation import configure_attestation_recipient

    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)
    from netbbs.attestation import set_attestation_link_visible as _share
    _share(db, alice, "name", True)
    configure_attestation_recipient(db, "a" * 32, reason="first")
    _deliver_to(db, "a" * 32)
    bob = create_user(db, "bob", password="hunter2")
    attest_name(db, bob, "Bob Builder", verifier=verifier)
    set_attestation_link_visible(db, bob, "name", True)
    _publish(db)  # a new object for Bob, not yet sent anywhere

    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert "Share verified name over Link: on (sent to 1 node)" in squeezed(_visible(session))


def test_remote_sharing_says_not_delivered_until_a_snapshot_is_sent(db, lane, alice):
    """Issue #632: whether the node can be dialed no longer decides this; what
    was sent does. Named recipients that nothing was sent to yet read as not
    delivered, on any node."""
    from netbbs.link.onboarding import record_link_reachability
    from netbbs.link.remote_attestation import configure_attestation_recipient

    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)
    configure_attestation_recipient(db, "a" * 32, reason="first")
    record_link_reachability(db, outgoing_only=True)

    session = FakeSession(["0", "9", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))

    text = squeezed(_visible(session))
    assert "Share verified name over Link: on (not delivered yet)" in text
    assert "sent to" not in text



def test_a_recipient_on_an_older_netbbs_is_not_counted(db, lane, alice):
    """Issue #1046: with the pull gone, a recipient that cannot take sealed
    snapshots receives nothing, and the caller is not told otherwise."""
    from netbbs.attestation import set_attestation_link_visible
    from netbbs.link.attestation_delivery import plan_attestation_deliveries, record_attestation_delivery_failure
    from netbbs.link.remote_attestation import configure_attestation_recipient

    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)
    set_attestation_link_visible(db, alice, "name", True)
    configure_attestation_recipient(db, "a" * 32, reason="current node")
    configure_attestation_recipient(db, "b" * 32, reason="older node")
    _deliver_to(db, "a" * 32)
    plan_attestation_deliveries(db)
    record_attestation_delivery_failure(db, "b" * 32, "needs a newer NetBBS to receive this")

    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert "Share verified name over Link: on (sent to 1 of 2 nodes)" in squeezed(_visible(session))
