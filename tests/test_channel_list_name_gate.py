"""The channel list says which channels will ask for a name (issue #541).

The report was that the picker offers a channel and then refuses the
caller. The obvious reading -- that `_visible_channels_for` forgot to
filter on the name requirement the way it filters on level and age --
turned out to be wrong: `tests/test_chat_flow_picker_authorization.py`
records the opposite as a decision. An age gate is a *content*
restriction and hides the channel; a name requirement is a
*participation* gate, so the channel stays listed and entry is refused
with its own message.

What was actually missing is any sign of the gate before that refusal. A
caller picked a channel, was turned away, and had nothing to act on. So
the list now names it, which is something they can act on: go and get
attested. The channel stays visible, because that part was never the
bug.
"""

from __future__ import annotations

import pytest

from netbbs.attestation import attest_name
from netbbs.auth.users import SYSOP_LEVEL, create_user, set_can_verify_identity
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.communities import create_community
from netbbs.net.chat_flow import _channel_description, list_visible_channels_for
from netbbs.storage.database import Database

from netbbs.net.chat_flow import NAME_GATE_NOTE as _NOTE


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return set_can_verify_identity(
        db, create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL),
        True, changed_by=create_user(db, "root", password="hunter2", user_level=SYSOP_LEVEL),
    )


@pytest.fixture
def caller(db):
    return create_user(db, "caller", password="hunter2", user_level=10)


# -- the decision that was already made --------------------------------


def test_a_gated_channel_is_still_listed(db, sysop, caller):
    """Hiding it would be the other product, and the one this codebase
    deliberately did not choose."""
    create_channel(db, "verified-only", creator=sysop, name_requirement="verified")
    assert [c.name for c in list_visible_channels_for(db, caller)] == ["verified-only"]


# -- and what the list now says about it -------------------------------


def test_the_line_says_a_name_is_needed(db, sysop, caller):
    channel = create_channel(db, "verified-only", creator=sysop, name_requirement="verified")
    assert _NOTE in _channel_description(ChatHub(), channel, {channel.id})


def test_it_says_nothing_once_the_caller_has_a_name(db, sysop, caller):
    channel = create_channel(db, "verified-only", creator=sysop, name_requirement="verified")
    attest_name(db, caller, "Real Person", verifier=sysop)
    # The caller now meets it, so the channel is not in the gated set.
    assert _NOTE not in _channel_description(ChatHub(), channel, set())


def test_an_ungated_channel_says_nothing(db, sysop, caller):
    channel = create_channel(db, "lobby", creator=sysop)
    assert _NOTE not in _channel_description(ChatHub(), channel, set())


def test_the_description_and_the_count_are_still_there(db, sysop):
    channel = create_channel(
        db, "verified-only", creator=sysop,
        description="Attested callers only", name_requirement="verified",
    )
    line = _channel_description(ChatHub(), channel, {channel.id})
    assert "Attested callers only" in line and "0 online" in line and _NOTE in line


def test_a_caller_with_no_gate_set_at_all_is_unchanged(db, sysop):
    """`needs_name=None` is the callback's own default, for any caller
    that has not resolved the set -- it must not invent a gate."""
    channel = create_channel(db, "lobby", creator=sysop)
    assert _channel_description(ChatHub(), channel) == _channel_description(
        ChatHub(), channel, set()
    )


# -- the gate is the effective one -------------------------------------


def test_a_requirement_inherited_from_a_community_counts(db, sysop, caller):
    """`get_effective_name_requirement` is what entry enforces, so it is
    what the list has to read -- a requirement set on the Community and
    not on the channel would otherwise be enforced without ever being
    mentioned."""
    from netbbs.attestation import meets_name_requirement
    from netbbs.communities import get_effective_name_requirement

    community = create_community(
        db, "Verified", default_name_requirement="verified", creator=sysop
    )
    channel = create_channel(db, "inherited", creator=sysop, community_id=community.id)

    assert channel.name_requirement is None, "the channel's own field is unset"
    assert get_effective_name_requirement(db, channel) == "verified"
    assert not meets_name_requirement(db, caller, get_effective_name_requirement(db, channel))


# -- the note survives a narrow row ------------------------------------


def test_the_note_comes_before_the_description(db, sysop):
    """`pick_item` clips the whole row to the terminal width, so
    anything after a free-form description is the first thing lost --
    and on a narrow terminal a gated channel would have looked exactly
    like an ungated one again, which is the bug (Codex review)."""
    channel = create_channel(
        db, "verified-only", creator=sysop,
        description="A very long description that will certainly be clipped on a narrow terminal",
        name_requirement="verified",
    )
    line = _channel_description(ChatHub(), channel, {channel.id})
    assert line.startswith(_NOTE)


def test_a_channel_with_no_description_still_reads_properly(db, sysop):
    channel = create_channel(db, "verified-only", creator=sysop, name_requirement="verified")
    line = _channel_description(ChatHub(), channel, {channel.id})
    assert line.startswith(_NOTE)
    assert not line.startswith(f"{_NOTE} -- ("), "no dangling separator before the count"


# -- and New scan says it too ------------------------------------------


def test_the_scan_picker_answers_the_same_question(db, sysop, caller):
    """`[N]ew scan` builds its own picker over the same channels, so
    fixing one of the two caller-facing channel pickers fixes it only
    for whoever happens to use that one (Codex review)."""
    from netbbs.net.chat_flow import channel_name_gate_unmet

    gated = create_channel(db, "verified-only", creator=sysop, name_requirement="verified")
    open_one = create_channel(db, "lobby", creator=sysop)

    assert channel_name_gate_unmet(db, caller, gated) is True
    assert channel_name_gate_unmet(db, caller, open_one) is False

    attest_name(db, caller, "Real Person", verifier=sysop)
    assert channel_name_gate_unmet(db, caller, gated) is False


# -- where truncation cannot reach it ----------------------------------


def test_the_note_rides_with_the_name(db, sysop):
    """`pick_item` composes a row as selector, reference, name,
    description -- and then truncates the whole thing. A note anywhere
    in the description sits behind an unbounded channel name and is the
    first thing a 40-column terminal loses, so the gated channel looks
    ungated again (Codex review, twice)."""
    from netbbs.net.chat_flow import channel_name_segments

    channel = create_channel(db, "verified-only", creator=sysop, name_requirement="verified")
    segments = channel_name_segments(channel, {channel.id})

    # The note leads: `colored_truncate` cuts from the end, so anything
    # behind an unbounded channel name can be cut away entirely, and a
    # 42-character name takes a 40-column row by itself.
    assert _NOTE in segments[0][0]
    assert segments[0][1] is not None, "and carries its own colour"
    assert segments[-1][0] == "verified-only"


def test_an_ungated_channel_is_just_its_name(db, sysop):
    from netbbs.net.chat_flow import channel_name_segments

    channel = create_channel(db, "lobby", creator=sysop)
    assert channel_name_segments(channel, set()) == [("lobby", None)]


def test_a_narrow_row_keeps_the_note_and_drops_the_prose(db, sysop):
    """The claim in one line: at 40 columns the gate survives and the
    description is what goes."""
    from netbbs.rendering import colored_truncate
    from netbbs.rendering.ansi import strip_ansi
    from netbbs.net.chat_flow import channel_name_segments

    channel = create_channel(
        db, "verified-only", creator=sysop,
        description="A long description that will not fit on a narrow terminal",
        name_requirement="verified",
    )
    segments = [("  01. ", None), ("(#1) ", None)]
    segments += channel_name_segments(channel, {channel.id})
    segments += [(" - " + _channel_description(ChatHub(), channel), None)]

    row = strip_ansi(colored_truncate(segments, 40))
    assert _NOTE in row, "the gate survives a narrow row"
    assert "A long description" not in row, "and the prose is what goes"


def test_even_a_name_wider_than_the_row_cannot_hide_the_note(db, sysop):
    """The case that decided the order: a 42-character channel name
    takes a 40-column row by itself, so a note behind it is gone
    whatever its length."""
    from netbbs.rendering import colored_truncate
    from netbbs.rendering.ansi import strip_ansi
    from netbbs.net.chat_flow import channel_name_segments

    channel = create_channel(
        db, "verified-only-channel-for-attested-callers", creator=sysop,
        name_requirement="verified",
    )
    segments = [("  01. ", None), ("(#1) ", None)]
    segments += channel_name_segments(channel, {channel.id})

    row = strip_ansi(colored_truncate(segments, 40))
    assert _NOTE in row
