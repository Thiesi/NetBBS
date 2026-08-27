"""
Integration tests for netbbs.net.picker — the generic paginated item
picker shared by boards, chat channels, and (once built) file areas.

Uses real TelnetServer/TelnetSession over loopback sockets, same as
test_telnet.py, since picker.py has no PyNaCl dependency and this is
exactly the kind of interaction-heavy protocol code worth verifying for
real rather than trusting from a read-through.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.net.session import Session
from netbbs.net.telnet import IAC, NAWS, SB, SE, WILL, TelnetServer
from netbbs.net.picker import pick_item
from netbbs.rendering import ERROR_COLOR, MUTED_COLOR, fg
from tests.test_telnet import skip_initial_negotiation

_ANSI_ESCAPE_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def _visible(data: bytes) -> bytes:
    """Strips SGR color codes -- issue #104 colorizes each list row field
    by field, so a raw byte scan for a line spanning multiple fields
    (e.g. "01. (#1) item1") would otherwise miss it: reset/color-on
    escapes now sit between the fields, even though each field's own
    text is still contiguous."""
    return _ANSI_ESCAPE_RE.sub(b"", data)


async def _run_server(session_handler):
    server = TelnetServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


async def _read_until_quiet(reader, quiet_time: float = 0.2) -> bytes:
    """
    Read whatever's available until the connection goes quiet for
    `quiet_time`, rather than a single fixed-size read — a single read
    can race ahead of the server still processing input and generating
    its response, especially across multiple round trips (a command
    letter, then a free-text follow-up prompt). Established as the
    reliable pattern after a single-read version of an early test in
    this file caught only a partial response and failed misleadingly.
    """
    chunks = []
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=quiet_time)
            if not chunk:
                break
            chunks.append(chunk)
        except asyncio.TimeoutError:
            break
    return b"".join(chunks)


def _naws_subneg(width: int, height: int) -> bytes:
    raw = bytes([(width >> 8) & 0xFF, width & 0xFF, (height >> 8) & 0xFF, height & 0xFF])
    escaped = bytearray()
    for b in raw:
        escaped.append(b)
        if b == 0xFF:
            escaped.append(0xFF)
    return bytes([IAC, SB, NAWS]) + bytes(escaped) + bytes([IAC, SE])


# -- empty list / basic selection / back --------------------------------


def test_empty_list_shows_message_and_returns_none():
    result = {}

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, [], name_of=lambda x: x, stable_id_of=lambda x: 0, title="Test", empty_message="Nothing here."
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = await _read_until_quiet(reader)
            assert b"Nothing here." in data
            assert fg(MUTED_COLOR).encode() in data
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_name_segments_of_colors_each_field_independently():
    """Dogfood report (the admin audit log's own timestamp/action/actor
    fields wanting independent colors): `name_segments_of`, when given,
    replaces the single-colored `name_of` string in the row with its own
    `(text, color)` segments, each colored separately -- confirms both
    colors actually reach the wire, and that the plain visible text is
    still exactly what a plain `name_of` row would have shown."""
    items = ["x"]

    def segments(_item):
        return [("2026-08-27", ERROR_COLOR), (" ", None), ("promote", MUTED_COLOR)]

    async def handler(session: Session):
        await pick_item(
            session, items, name_of=lambda x: "2026-08-27 promote", stable_id_of=lambda x: 1,
            name_segments_of=segments, title="Test", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = await _read_until_quiet(reader)
            assert fg(ERROR_COLOR).encode() in data
            assert fg(MUTED_COLOR).encode() in data
            assert b"2026-08-27 promote" in _visible(data)

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_search_and_goto_are_rejected_when_the_list_is_empty_but_refreshable():
    """
    Dogfood report, issue #155: an empty list with a `refresh` callback
    (Who's Online's own use, so Ctrl-R can revive a list that goes
    stale while you're looking at it) stays in the interactive loop
    instead of the plain early-return a refresh-less empty list gets --
    [S]earch and [G]oto # must not be silently functional there just
    because that loop is still running, when neither is even shown on
    the empty-state prompt.
    """
    result = {}

    async def _refresh():
        return []

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, [], name_of=lambda x: x, stable_id_of=lambda x: 0,
            title="Who's online", empty_message="No one else is online right now.",
            refresh=_refresh,
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            first = await _read_until_quiet(reader)
            assert b"No one else is online right now." in first
            assert b"Search" not in first
            assert b"Goto" not in first

            writer.write(b"s")
            await writer.drain()
            after_search = await _read_until_quiet(reader)
            assert b"\a" in after_search
            assert b"Search:" not in after_search

            writer.write(b"g")
            await writer.drain()
            after_goto = await _read_until_quiet(reader)
            assert b"\a" in after_goto
            assert b"Go to #:" not in after_goto

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_ctrl_c_is_an_alias_for_back():
    """Dogfood feature request, issue #157: an incremental Ctrl-C
    alias for this screen's own [B]ack action."""
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"\x03")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_ctrl_h_shows_real_navigation_help():
    # Dogfood feature request: this shared picker (boards/channels/file
    # areas/users) previously had no on-demand help at all -- only the
    # terse inline `brief` shown when menu descriptions are on.
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"\x08")
            await writer.drain()
            help_text = _visible(await _read_until_quiet(reader))
            writer.write(b" ")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"\x03")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()
        return help_text

    help_text = asyncio.run(scenario())
    assert b"permanent '(#N)' reference" in help_text
    assert b"Order" not in help_text  # no on_sort given to this picker


def test_select_by_two_digit_number():
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"02")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "beta"


def test_selection_ends_with_its_own_newline_before_whatever_comes_next():
    """Real dogfood-reported bug: a valid 2-digit selection used to
    `return` without ever writing a newline first (unlike every other
    state-changing branch here -- `[B]ack`/`[N]ext`/`[P]rev` all do), so
    a caller's own very next prompt landed directly after the echoed
    digits on the same line (e.g. "Choice: 02Disconnect 'test'? [y/N]:
    ", no separation at all)."""
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        selected = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )
        result["value"] = selected
        # Simulates a caller's own very next prompt, immediately after
        # pick_item returns -- exactly the shape of the reported bug.
        await session.write("AFTER")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"02")
            await writer.drain()
            data = await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
            assert b"02AFTER" not in data
            assert b"\r\nAFTER" in data or b"\nAFTER" in data
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "beta"


def test_back_returns_none():
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_invalid_two_digit_selection_sounds_bell_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"99")  # only 2 items exist
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no error message -- just a bell (design doc:
            # "no redraw on invalid single-keystroke menu input").
            assert b"\a" in data
            assert b"Invalid selection." not in data
            assert b"page " not in data  # the page/nav block wasn't redrawn
            writer.write(b"01")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "a"


def test_unknown_command_letter_sounds_bell_and_stays_in_picker():
    result = {}
    items = ["a"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"z")
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no error message -- just a bell.
            assert b"\a" in data
            assert b"Unknown command." not in data
            assert b"page " not in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_repeated_invalid_keys_produce_nothing_but_an_echo_and_a_bell():
    """
    An invalid keystroke gets genuinely *nothing* beyond the bell --
    no reprinted "Choice: " prompt, no synthetic newline. Reprinting
    the prompt after the bell would add no value (the prompt is
    already visible, and reprinting it communicates nothing new).
    """
    result = {}
    items = ["a"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"z")
            await writer.drain()
            first = await _read_until_quiet(reader)
            writer.write(b"y")
            await writer.drain()
            second = await _read_until_quiet(reader)
            # The echoed character, immediately erased, plus a bell --
            # nothing else, each time, regardless of how many invalid
            # keys precede it (echo happens inside read_key before
            # pick_item ever sees the key, so rejecting it also erases
            # the already-echoed character via reject_keystroke()).
            assert first == b"z\b \b\a"
            assert second == b"y\b \b\a"
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


# -- search ----------------------------------------------------------------


def test_search_unique_match_auto_selects():
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"gam\r\n")  # matches only "gamma"
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "gamma"


def test_search_multiple_matches_then_select():
    result = {}
    items = ["apple", "apricot", "banana"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"ap\r\n")  # matches apple + apricot
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"02")  # 2nd of the filtered results -> apricot
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "apricot"


def test_search_no_matches_reports_and_stays_in_picker():
    result = {}
    items = ["apple", "banana"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"zzz\r\n")
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"No matches." in data
            assert fg(ERROR_COLOR).encode() in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_empty_search_clears_active_filter():
    result = {}
    items = ["apple", "banana", "cherry"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"\r\n")  # empty search -> back to full unfiltered list
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"3 total" in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_search_matches_name_case_insensitively():
    result = {}
    items = ["Apple", "Banana"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"APPLE\r\n")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "Apple"


# -- search: Tab completion --------------------------------------------------


def test_search_tab_completes_a_single_matching_candidate():
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"al\t\r\n")  # Tab-complete "al" to "alpha ", then Enter
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "alpha"


def test_search_tab_with_no_matching_candidates_does_not_change_the_query():
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"zz\t\r\n")  # no candidate starts with "zz"
            data = await _read_until_quiet(reader)
            assert b"No matches." in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_search_tab_completion_reflects_the_current_working_set_not_the_full_list():
    # Candidates for Tab are drawn from `working_set` -- confirms a
    # completion offered mid-search doesn't ever suggest an item already
    # filtered out by an earlier search.
    result = {}
    items = ["alpha", "alligator", "amber"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            # Narrow to alpha/alligator via a substring search that
            # "amber" doesn't match at all.
            writer.write(b"al\r\n")
            await _read_until_quiet(reader)

            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"a\t")  # Tab, scoped to the narrowed working_set
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"amber" not in data  # excluded by the earlier search, not just prefix
            assert b"alpha" in data
            assert b"alligator" in data

            writer.write(b"pha\r\n")  # finish typing "alpha" -> unique substring match
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "alpha"


# -- goto --------------------------------------------------------------


def test_goto_absolute_index():
    result = {}
    items = [f"item{i}" for i in range(1, 21)]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"15\r\n")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "item15"


def test_goto_out_of_range_reports_and_stays_in_picker():
    result = {}
    items = ["a", "b", "c"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"999\r\n")
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"Out of range." in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_goto_non_numeric_input_reports_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"notanumber\r\n")
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"Not a number." in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


# -- pagination, adaptive to negotiated terminal height ---------------------


def test_pagination_adapts_to_negotiated_terminal_height():
    """
    20 items, a negotiated 12-row terminal (page size = 12 - 6 reserved
    = 6 items/page), next-page navigation, then a page-relative 2-digit
    selection on the second page.
    """
    result = {}
    items = [f"item{i:02d}" for i in range(1, 21)]

    async def handler(session: Session):
        # Mirrors realistic usage: NAWS has already resolved by the time
        # a picker is shown, since login always happens first — this
        # dummy read is what makes that true in the test too.
        await session.read_line()
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(_naws_subneg(80, 12))
            writer.write(b"x\r\n")
            await writer.drain()

            text1 = (await _read_until_quiet(reader)).decode()
            assert "item01" in text1 and "item06" in text1
            assert "item07" not in text1

            writer.write(b"n")
            await writer.drain()
            text2 = (await _read_until_quiet(reader)).decode()
            assert "item07" in text2 and "item12" in text2

            writer.write(b"02")  # 2nd item on page 2 -> item08
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "item08"


def test_description_level_brief_shows_nav_descriptions():
    """Issue #160's rollout to this screen: the nav row renders through
    `menu_grid` when `description_level="brief"` is passed, showing each
    nav command's own short description underneath its hotkey.

    Only two items (a single page) deliberately, so [N]ext/[P]rev are
    both hidden (issue #169 dogfood report -- neither is usable with
    nothing to page to) and only [S]earch/[G]oto/[B]ack remain to
    assert the descriptive rendering against."""
    result = {}
    items = ["item1", "item2"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", description_level="brief",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            text = (await _read_until_quiet(reader)).decode()
            assert "Search by name" in text
            assert "Return without picking" in text
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_description_level_brief_reserves_extra_lines_for_the_taller_nav_block():
    """The nav row grows taller once descriptions are on (issue #160's
    own flat-section column-splitting packs the 5 Next/Prev/Search/
    Goto/Back entries into 2 columns of 6 lines total at this width,
    rather than 1 column of 10), so `_page_size` must reserve more
    lines than the `description_level="off"` case -- otherwise the item
    list plus the now-taller nav block would overflow a real terminal
    of this height. At a negotiated 80x20 terminal: off reserves 6
    lines (page size 14), brief reserves 11 (page size 9) -- verified
    here by checking exactly 9 of 20 items appear on page 1."""
    result = {}
    items = [f"item{i:02d}" for i in range(1, 21)]

    async def handler(session: Session):
        await session.read_line()
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", description_level="brief",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(_naws_subneg(80, 20))
            writer.write(b"x\r\n")
            await writer.drain()

            text = (await _read_until_quiet(reader)).decode()
            assert "item01" in text and "item09" in text
            assert "item10" not in text

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_description_level_brief_falls_back_to_compact_nav_below_the_page_size_floor():
    """`menu_description_level`'s real default is "brief", not "off" --
    every caller who has never touched the setting gets it. Without a
    floor, a short-but-real terminal (here: a negotiated 80x14) would
    reserve so many lines for the now-multi-line nav that the item list
    itself would collapse to almost nothing -- descriptions are a nice-
    to-have, being able to actually browse the list is the point of
    this screen. Below the floor, the nav silently reverts to the
    compact single-line form regardless of preference: verified here by
    checking more than 5 of 20 items appear on page 1 (the floor is 5;
    the pre-fix collapse at 14 rows was down to 2)."""
    result = {}
    items = [f"item{i:02d}" for i in range(1, 21)]

    async def handler(session: Session):
        await session.read_line()
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", description_level="brief",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(_naws_subneg(80, 14))
            writer.write(b"x\r\n")
            await writer.drain()

            text = (await _read_until_quiet(reader)).decode()
            assert "item06" in text
            # Compact form: nav descriptions are absent from this page.
            assert "Next page" not in text

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_prev_on_first_page_sounds_bell_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"p")
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no notice message -- just a bell (nothing about
            # the page actually changed).
            assert b"\a" in data
            assert b"Already on the first page." not in data
            assert b"page " not in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_next_on_last_page_sounds_bell_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"n")  # already on (only) last page with default-size terminal
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no notice message -- just a bell (nothing about
            # the page actually changed).
            assert b"\a" in data
            assert b"Already on the last page." not in data
            assert b"page " not in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_next_and_prev_are_hidden_on_a_single_page_list():
    """Dogfood report (issue #169): [N]ext/[P]rev used to be shown even
    with nothing to page to -- pressing either just bell-rejected
    (still true, see the two tests above), but the nav row implied both
    were live options. Matches the precedent already set by
    `netbbs.net.login_flow`'s board-post pager, which only shows its own
    [O]lder/[N]ewer when there actually is more to page to."""
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            text = (await _read_until_quiet(reader)).decode()
            assert "ext" not in text  # the "ext" tail of "[N]ext" -- see menu_key
            assert "rev" not in text  # the "rev" tail of "[P]rev"
            assert "earch" in text  # [S]earch stays -- unaffected by paging state
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_next_is_hidden_but_prev_shown_on_the_last_page_of_a_multi_page_list():
    result = {}
    items = [f"item{i:02d}" for i in range(1, 41)]  # spans several pages

    def _current_and_total_page(text: str) -> tuple[int, int]:
        match = re.search(r"page (\d+)/(\d+)", text)
        assert match is not None, text
        return int(match.group(1)), int(match.group(2))

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="I", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            text = (await _read_until_quiet(reader)).decode()
            assert "ext" in text  # first page: a next page exists
            assert "rev" not in text  # first page: no previous page yet

            current, total = _current_and_total_page(text)
            assert total > 1  # the scenario is only meaningful with several pages
            for _ in range(total - current):
                writer.write(b"n")
                await writer.drain()
                text = (await _read_until_quiet(reader)).decode()

            current, total = _current_and_total_page(text)
            assert current == total
            assert "rev" in text  # last page: a previous page exists
            assert "ext" not in text  # last page: no next page

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


# -- arrow-select (issue #171) -----------------------------------------

_UP = b"\x1b[A"
_DOWN = b"\x1b[B"
_ENTER = b"\r\n"


def test_no_row_is_highlighted_until_the_first_arrow_press():
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = _visible(await _read_until_quiet(reader))
            assert b"  01. " in data
            assert b"> 01." not in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_arrow_down_moves_the_highlight_and_bells_past_the_last_row():
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(_DOWN)  # unhighlighted -> row 1 (alpha)
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 01." in data

            writer.write(_DOWN)  # row 1 -> row 2 (beta)
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 02." in data
            assert b"> 01." not in data

            writer.write(_DOWN)  # row 2 -> row 3 (gamma, the last row)
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 03." in data

            writer.write(_DOWN)  # already on the last row -- bell, no redraw
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"\a" in data
            assert b"gamma" not in data

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_arrow_up_from_unhighlighted_lands_on_the_last_row_and_bells_past_the_first():
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(_UP)  # unhighlighted -> lands on the last row (gamma), not wrapping
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 03." in data

            writer.write(_UP)  # row 3 -> row 2
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 02." in data

            writer.write(_UP)  # row 2 -> row 1
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 01." in data

            writer.write(_UP)  # already on the first row -- bell, no wraparound
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"\a" in data
            assert b"alpha" not in data

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_enter_selects_the_highlighted_row():
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(_DOWN + _DOWN)  # highlight row 2 (beta)
            await writer.drain()
            await _read_until_quiet(reader)

            writer.write(_ENTER)
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "beta"


def test_enter_with_nothing_highlighted_bells_and_stays_in_picker():
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(_ENTER)
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"\a" in data

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_two_digit_selection_is_unaffected_by_an_active_highlight():
    """The two paths coexist without interfering: highlighting a row via
    the arrows doesn't change what a typed 2-digit number selects."""
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(_DOWN)  # highlight row 1 (alpha)
            await writer.drain()
            await _read_until_quiet(reader)

            writer.write(b"03")  # typed selection still jumps straight to row 3 (gamma)
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "gamma"


def test_highlight_resets_to_unhighlighted_after_paging():
    result = {}
    items = [f"item{i:02d}" for i in range(1, 21)]  # 2 pages at the default 18-per-page size

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(_UP)  # unhighlighted -> lands on page 1's last row (item18)
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 18." in data

            writer.write(b"n")  # page to page 2 (item19, item20)
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 01." not in data  # no stale highlight carried onto the new page

            writer.write(_DOWN)  # fresh highlight on page 2's own first row
            await writer.drain()
            data = _visible(await _read_until_quiet(reader))
            assert b"> 01." in data
            assert b"item19" in data

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


# -- start_stable_id (dogfood report: preview-then-decline lost your place) -


def test_start_stable_id_reopens_already_highlighted_on_the_matching_row():
    result = {}
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", start_stable_id=2,  # "beta"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = _visible(await _read_until_quiet(reader))
            assert b"> 02." in data  # highlighted on first render, no arrow press needed
            assert b"beta" in data

            writer.write(_ENTER)  # Enter immediately selects the pre-highlighted row
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "beta"


def test_start_stable_id_opens_directly_on_the_page_containing_that_item():
    result = {}
    items = [f"item{i:02d}" for i in range(1, 21)]  # 2 pages at the default 18-per-page size

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", start_stable_id=19,  # item19, page 2's first row
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = _visible(await _read_until_quiet(reader))
            assert re.search(rb"page 2/2", data)
            assert b"> 01." in data
            assert b"item19" in data

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_start_stable_id_with_no_match_falls_back_to_ordinary_start():
    """The item a caller last looked at can legitimately be gone by the
    time it re-enters the picker (deleted, filtered out) -- an unmatched
    `start_stable_id` must not crash or leave the picker in a broken
    state, just behave exactly as if it had been omitted."""
    items = ["alpha", "beta", "gamma"]

    async def handler(session: Session):
        await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", start_stable_id=999,
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = _visible(await _read_until_quiet(reader))
            assert b"  01. " in data
            assert b">" not in data.split(b"Choice:")[0]
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


# -- description column & truncation ---------------------------------------


def test_description_shown_alongside_name():
    result = {}
    items = [("general", "General discussion")]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session,
            items,
            name_of=lambda x: x[0],
            stable_id_of=lambda x: items.index(x) + 1,
            description_of=lambda x: x[1],
            title="Boards",
            empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = await _read_until_quiet(reader)
            assert b"general" in data
            assert b"General discussion" in data
            writer.write(b"01")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == ("general", "General discussion")


# -- goto stability across search filtering (regression tests) --------


def test_goto_after_search_uses_stable_original_index_not_filtered_position():
    """
    Regression test for a real bug found while reviewing this module:
    `goto` used to index into `working_set` (whatever a prior search had
    narrowed the view to), not the original unfiltered list — so "goto
    #3" after searching could silently return a different item than
    "goto #3" would with no search active. Confirmed with this exact
    scenario before the fix (searching "item1" against item1..item20,
    then "goto 3", incorrectly returned "item11" — the 3rd search match
    — instead of "item3", the 3rd item overall).
    """
    result = {}
    items = [f"item{i}" for i in range(1, 21)]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"item1\r\n")  # matches item1, item10-item19
            await writer.drain()
            await _read_until_quiet(reader)

            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"3\r\n")
            await writer.drain()
            await _read_until_quiet(reader)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "item3"


def test_stable_absolute_index_is_displayed_alongside_page_relative_number():
    """
    Without displaying an item's stable absolute index somewhere on
    screen, `goto` would be nearly undiscoverable — nothing else
    reveals what number to type for it. Confirms the "(#N)" annotation
    is actually present and correct, not just that goto works when a
    caller already happens to know the right number.
    """
    result = {}
    items = [f"item{i}" for i in range(1, 21)]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = await _read_until_quiet(reader)
            # Issue #171 dogfood report: stable_id_of references now
            # right-pad to the widest id on the page (item10's "(#10)"
            # here), so single-digit ids get one extra trailing space --
            # "(#1)  item1", not "(#1) item1" -- see picker.py's own
            # comment on `max_id_width` for why.
            assert b"01. (#1)  item1" in _visible(data)
            assert b"02. (#2)  item2" in _visible(data)
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_stable_index_correct_on_second_page():
    """The stable "(#N)" shown for an item on page 2+ must be its true
    absolute position, not restarted per page the way the 2-digit
    selector correctly is."""
    result = {}
    items = [f"item{i}" for i in range(1, 21)]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(b"n")
            await writer.drain()
            data = await _read_until_quiet(reader)
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
            return data
        finally:
            await server.stop()

    data = asyncio.run(scenario())
    # Default terminal height (80x24, no NAWS sent) gives page_size=18,
    # so page 2 starts at item19: absolute index 19, not restarted at 1.
    assert b"01. (#19) item19" in _visible(data)
    assert b"02. (#20) item20" in _visible(data)


# -- genuine stable-ID/position decoupling (not just index-based IDs) -----


def test_goto_uses_caller_supplied_stable_id_not_list_position():
    """
    Real proof of decoupling, not just re-confirming index-based IDs
    still work: items here have deliberately non-sequential,
    non-positional stable IDs (as real database IDs would be), and goto
    must resolve by that ID, never by position in the list.
    """
    result = {}
    # (stable_id, name) pairs, stable IDs deliberately out of order and
    # non-sequential -- position 1 has ID 205, position 2 has ID 7, etc.
    items = [(205, "gamma"), (7, "alpha"), (999, "delta"), (42, "beta")]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session,
            items,
            name_of=lambda x: x[1],
            stable_id_of=lambda x: x[0],
            title="Items",
            empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"42\r\n")  # goto stable ID 42, which is "beta", at position 4
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == (42, "beta")


def test_display_shows_caller_supplied_stable_id_not_position():
    result = {}
    items = [(205, "gamma"), (7, "alpha")]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session,
            items,
            name_of=lambda x: x[1],
            stable_id_of=lambda x: x[0],
            title="Items",
            empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            data = await _read_until_quiet(reader)
            # Position 1 on screen ("01.") shows stable ID 205, not "1" —
            # and position 2 ("02.") shows stable ID 7, not "2". "(#7)"
            # gets two extra trailing spaces (issue #171) so "alpha"
            # still starts at the same column "gamma" does, despite its
            # id being two digits narrower than "(#205)".
            assert b"01. (#205) gamma" in _visible(data)
            assert b"02. (#7)   alpha" in _visible(data)
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_goto_ignores_current_search_filter_with_non_positional_ids():
    """Combines both properties at once: goto by permanent stable ID,
    unaffected by an active search filter, using IDs that don't match
    position — the real-world shape of the scenario this whole redesign
    was for."""
    result = {}
    items = [(205, "gamma"), (7, "alpha widget"), (999, "delta"), (42, "beta widget")]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session,
            items,
            name_of=lambda x: x[1],
            stable_id_of=lambda x: x[0],
            title="Items",
            empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            # Search narrows to the two "widget" items first.
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"widget\r\n")
            await writer.drain()
            await _read_until_quiet(reader)

            # goto 205 ("gamma") isn't even among the search matches --
            # must still resolve correctly against the full original list.
            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"205\r\n")
            await writer.drain()
            await _read_until_quiet(reader)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == (205, "gamma")


# -- mixed-type lists (category + item sharing one picker call) -----------


def test_mixed_category_and_item_list_disambiguates_colliding_ids():
    """
    Real usage pattern from netbbs.net.login_flow/chat_flow: categories
    and boards/channels come from different database tables, so their
    raw IDs can collide (both start at 1). Mixed into one picker call
    (so a user can pick either a category to drill into, or a board/
    channel directly), that collision would make `goto` ambiguous
    between two different things showing the same number, unless
    disambiguated — verified here with genuinely colliding IDs (a
    category id=1 and a board id=1 both present), using the actual
    disambiguation scheme login_flow.py/chat_flow.py use: negate the
    category's ID for picker purposes only.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakeCategory:
        id: int
        name: str

    @dataclass(frozen=True)
    class FakeBoard:
        id: int
        name: str

    result = {}
    categories = [FakeCategory(id=1, name="Vintage Computing"), FakeCategory(id=2, name="Politics")]
    boards = [FakeBoard(id=1, name="general"), FakeBoard(id=2, name="offtopic")]
    mixed = [*categories, *boards]

    def render_name(item):
        return f"[{item.name}]" if isinstance(item, FakeCategory) else item.name

    def stable_id(item):
        return item.id if isinstance(item, FakeBoard) else -item.id

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, mixed, name_of=render_name, stable_id_of=stable_id,
            title="Mixed", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"g")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"-1\r\n")  # goto the category with raw id=1
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert isinstance(result["value"], FakeCategory)
    assert result["value"].id == 1  # the category, not the board sharing the same raw id


def test_mixed_list_two_digit_selection_unaffected_by_id_disambiguation():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakeCategory:
        id: int
        name: str

    @dataclass(frozen=True)
    class FakeBoard:
        id: int
        name: str

    result = {}
    categories = [FakeCategory(id=1, name="Vintage Computing")]
    boards = [FakeBoard(id=1, name="general"), FakeBoard(id=2, name="offtopic")]
    mixed = [*categories, *boards]  # page positions: 01=category, 02=general, 03=offtopic

    def render_name(item):
        return f"[{item.name}]" if isinstance(item, FakeCategory) else item.name

    def stable_id(item):
        return item.id if isinstance(item, FakeBoard) else -item.id

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, mixed, name_of=render_name, stable_id_of=stable_id,
            title="Mixed", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)
            writer.write(b"02")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == FakeBoard(id=1, name="general")


# -- issue #102: Ctrl-L redraw / Ctrl-R refresh --------------------------


def test_ctrl_l_redraws_the_current_page():
    result = {}
    items = [f"item{i}" for i in range(1, 4)]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            first = await _read_until_quiet(reader)
            assert first.count(b"page ") == 1

            writer.write(b"\x0c")
            await writer.drain()
            second = await _read_until_quiet(reader)
            # A second full page/nav block, not a bell -- Ctrl-L is a
            # deliberate redraw request, not a rejected keystroke.
            assert second.count(b"page ") == 1
            assert b"\a" not in second

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_ctrl_r_without_a_refresh_callback_sounds_a_bell():
    result = {}
    items = ["only"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: 1,
            title="Items", empty_message="none",
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(b"\x12")
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"\a" in data
            assert data.count(b"page ") == 0  # no redraw happened either

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_ctrl_r_refetches_and_displays_the_refreshed_list():
    result = {}
    initial_items = ["old1", "old2"]
    refreshed_items = ["new1", "new2", "new3"]
    stable_ids = {"old1": 1, "old2": 2, "new1": 3, "new2": 4, "new3": 5}

    async def refresh():
        return refreshed_items

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, initial_items, name_of=lambda x: x, stable_id_of=lambda x: stable_ids[x],
            title="Items", empty_message="none", refresh=refresh,
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            first = await _read_until_quiet(reader)
            assert b"old1" in first
            assert b"2 total" in first

            writer.write(b"\x12")
            await writer.drain()
            second = await _read_until_quiet(reader)
            assert b"new1" in second
            assert b"new3" in second
            assert b"old1" not in second
            assert b"3 total" in second

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_ctrl_r_refresh_resets_page_index_and_clears_search_filter():
    """A refresh means "show me current reality" -- not "reapply my old
    search filter to it". Also confirms the page index resets even if
    it was sitting on page 2+ of the old (larger) working set."""
    result = {}
    initial_items = [f"item{i}" for i in range(1, 21)]  # 20 -> 2 pages at page_size 18
    refreshed_items = ["fresh"]
    stable_ids = {f"item{i}": i for i in range(1, 21)}
    stable_ids["fresh"] = 100

    async def refresh():
        return refreshed_items

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, initial_items, name_of=lambda x: x, stable_id_of=lambda x: stable_ids[x],
            title="Items", empty_message="none", refresh=refresh,
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await _read_until_quiet(reader)

            writer.write(b"n")  # page 2
            await writer.drain()
            await _read_until_quiet(reader)

            writer.write(b"\x12")
            await writer.drain()
            data = await _read_until_quiet(reader)
            assert b"fresh" in data
            assert b"page 1/1" in data

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_nav_trailer_line_never_exceeds_the_terminal_width():
    """Dogfood-reported regression: with sort mode and refresh both
    active (the shape login_flow.py's board-browsing screen actually
    uses), the nav row plus its trailing "or type a 2-digit number...;
    Sort: ..." suffix could run past the real negotiated width -- every
    other line on this screen is already deterministically cut
    (colored_truncate/menu_grid), but this combined line wasn't clamped
    at all, so a client would wrap it wherever it happened to land,
    sometimes mid-word. At a negotiated 40-column terminal with a long
    sort label, the uncut line would be roughly 90+ columns; verified
    here that no line in the actual rendered output exceeds 40."""
    result = {}
    items = ["item1", "item2"]

    async def refresh():
        return items

    async def on_sort():
        return None

    def sort_label():
        return "A Rather Long Descriptive Sort Mode Name"

    async def handler(session: Session):
        # Mirrors realistic usage (see test_pagination_adapts_to_
        # negotiated_terminal_height): NAWS negotiation races the
        # client's next write, so a dummy read lets it resolve first.
        await session.read_line()
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none",
            refresh=refresh, on_sort=on_sort, sort_label=sort_label,
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(_naws_subneg(40, 24))
            writer.write(b"x\r\n")
            await writer.drain()

            data = await _read_until_quiet(reader)
            for line in _visible(data).decode().split("\r\n"):
                assert len(line) <= 40, f"line exceeds 40 columns: {line!r}"

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_nav_trailer_wraps_instead_of_losing_text_on_an_ordinary_terminal():
    """Dogfood report: the fix above (clamp the combined nav+trailer
    line to the negotiated width) was itself achieved by *cutting* the
    trailer to whatever room remained on the shared `action_bar` line --
    on an perfectly ordinary 80-column terminal with a sort label
    active (exactly `netbbs.net.login_flow`'s board-browsing screen,
    `description_level="off"`), that budget is under 40 columns,
    nowhere near enough for "or type a 2-digit number to select;
    Ctrl-L: redraw, Ctrl-H: help" -- silently deleting real
    instructions, including the Ctrl-H hint pointing at the one screen
    that explains all of this, on every single page render, not some
    rare edge case. Confirms the full boilerplate now always survives
    somewhere in the rendered output (wrapped onto its own line(s)
    instead), while every line individually still respects the
    negotiated width."""
    items = ["item1", "item2"]

    async def on_sort():
        return None

    def sort_label():
        return "Activity"

    async def handler(session: Session):
        await session.read_line()
        await pick_item(
            session, items, name_of=lambda x: x, stable_id_of=lambda x: items.index(x) + 1,
            title="Items", empty_message="none", on_sort=on_sort, sort_label=sort_label,
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(_naws_subneg(80, 24))
            writer.write(b"x\r\n")
            await writer.drain()

            data = await _read_until_quiet(reader)
            text = _visible(data).decode()
            for line in text.split("\r\n"):
                assert len(line) <= 80, f"line exceeds 80 columns: {line!r}"
            assert "Ctrl-H: help" in text, "trailer boilerplate was lost, not wrapped"
            assert "or type a 2-digit number to select" in text

            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
