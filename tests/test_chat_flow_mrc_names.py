"""
Issue #1156: each caller chooses how an MRC sender's decorated handle
(`+Nick+[TAG]`, `^Nick<tag>`) is shown -- combined into NetBBS's label
(the default), both label and handle, or the label alone. On the rig of
`tests/test_chat_flow_mrc.py`, live and on replay, for room lines and for
private and broadcast notices.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.mrc.bridge import MrcNotice
from netbbs.net import chat_flow
from netbbs.net.mrc_name_preference import mrc_name_style, set_mrc_name_style
from netbbs.rendering.ansi import strip_ansi
from netbbs.timeutil import utc_now_iso
from tests.test_chat_flow_mrc import (  # noqa: F401 -- fixtures
    _rig,
    _run,
    _text,
    _wait_for,
    alice,
    channel,
    db,
    hub,
    lane,
    presence,
    sysop,
)


def _record(db, channel, *, handle):
    return record_message(
        db, channel, kind="message", author_label="Michael Nln@Castle_of_the_Gods_V (MRC)",
        author_fingerprint=None, body="hello all", external_source="mrc", index_body="hello all",
        mrc_handle=handle,
    )


def _replayed(lane, hub, presence, channel, alice) -> str:
    async def scenario():
        session, _ = await _run(lane, hub, presence, channel, alice, ["/quit"])
        # A long label wraps at 80 columns; compare words, not rows.
        return " ".join(_text(session).split())
    return asyncio.run(scenario())


EXPECTED = {
    "combined": "[MRC] <+Michael_Nln+@Castle_of_the_Gods_V (CASTLE BBS)> hello all",
    "both": "[MRC] <Michael Nln@Castle_of_the_Gods_V> +Michael_Nln+[CASTLE BBS] hello all",
    "label": "[MRC] <Michael Nln@Castle_of_the_Gods_V> hello all",
}


def test_combined_is_the_default(db, alice):
    assert mrc_name_style(db, alice) == "combined"
    with pytest.raises(ValueError):
        set_mrc_name_style(db, alice, "fancy")


@pytest.mark.parametrize("style", sorted(EXPECTED))
def test_each_style_on_replay(db, lane, hub, presence, channel, alice, style):
    _record(db, channel, handle="|11+Michael_Nln+|08[CASTLE BBS]|07")
    set_mrc_name_style(db, alice, style)
    assert EXPECTED[style] in _replayed(lane, hub, presence, channel, alice)


@pytest.mark.parametrize("style", sorted(EXPECTED))
def test_a_line_without_a_stored_handle_looks_the_same_in_every_style(db, lane, hub, presence, channel, alice, style):
    """Lines recorded before #1156, and lines with a plain handle."""
    _record(db, channel, handle=None)
    set_mrc_name_style(db, alice, style)
    assert EXPECTED["label"] in _replayed(lane, hub, presence, channel, alice)


def test_the_tag_never_puts_label_characters_inside_the_label(db, lane, hub, presence, channel, alice):
    _record(db, channel, handle="^Michael_Nln<@evil~SysOp*>")
    text = _replayed(lane, hub, presence, channel, alice)
    assert "[MRC] <^Michael_Nln@Castle_of_the_Gods_V (evil SysOp)> hello all" in text


def test_a_live_line_keeps_its_handle_and_shows_combined(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            async def push(session):
                await rig.fake.send_line("johnny5~The_Delta_Quadrant~lobby~~~lobby~^Johnny5<grAvY> hey there~")
                await _wait_for(lambda: "hey there" in _text(session), what="the decorated line")

            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/quit"], mrc_bridge=rig.bridge, while_joined=push,
            )
            assert "[MRC] <^Johnny5@The_Delta_Quadrant (grAvY)> hey there" in _text(session)
        finally:
            await rig.close()

    asyncio.run(scenario())
    stored = [m for m in get_scrollback(db, channel) if m.body == "hey there"]
    assert stored and stored[-1].mrc_handle == "^Johnny5<grAvY>"


def test_a_handle_is_only_kept_on_an_mrc_line(db, channel):
    with pytest.raises(ValueError):
        record_message(db, channel, kind="message", author_label="alice", body="hi", mrc_handle="+alice+")


@pytest.mark.parametrize(
    "style, expected",
    [
        ("combined", "[MRC private] +Michael_Nln+@Castle_of_the_Gods_V (CASTLE BBS): psst"),
        ("both", "[MRC private] Michael Nln@Castle_of_the_Gods_V: +Michael_Nln+[CASTLE BBS] psst"),
        ("label", "[MRC private] Michael Nln@Castle_of_the_Gods_V: psst"),
    ],
)
def test_private_and_broadcast_notices_follow_the_viewer(db, alice, style, expected):
    set_mrc_name_style(db, alice, style)
    sender = "Michael Nln@Castle_of_the_Gods_V"
    for kind, badge in (("private", "[MRC private]"), ("broadcast", "[MRC broadcast]")):
        notice = MrcNotice(
            f"{sender}: psst", utc_now_iso(), kind=kind, sender=sender, from_user="Michael_Nln",
            handle="+Michael_Nln+[CASTLE BBS]", message="psst",
        )
        shown = strip_ansi(chat_flow._render_mrc_notice(db, alice, notice))
        assert expected.replace("[MRC private]", badge) in shown
