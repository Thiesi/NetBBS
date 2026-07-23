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

from netbbs.net.session import Session
from netbbs.net.telnet import IAC, NAWS, SB, SE, WILL, TelnetServer
from netbbs.net.picker import pick_item


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
            await reader.readexactly(9)
            data = await _read_until_quiet(reader)
            assert b"Nothing here." in data
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
            await _read_until_quiet(reader)
            writer.write(b"99")  # only 2 items exist
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no error message -- just a bell (design doc:
            # "no redraw on invalid single-keystroke menu input").
            assert b"\a" in data
            assert b"Invalid selection." not in data
            assert b"(page " not in data  # the page/nav block wasn't redrawn
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
            await reader.readexactly(9)
            await _read_until_quiet(reader)
            writer.write(b"z")
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no error message -- just a bell.
            assert b"\a" in data
            assert b"Unknown command." not in data
            assert b"(page " not in data
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
            await _read_until_quiet(reader)
            writer.write(b"s")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.write(b"zzz\r\n")
            await writer.drain()
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
            await _read_until_quiet(reader)
            writer.write(b"p")
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no notice message -- just a bell (nothing about
            # the page actually changed).
            assert b"\a" in data
            assert b"Already on the first page." not in data
            assert b"(page " not in data
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
            await reader.readexactly(9)
            await _read_until_quiet(reader)
            writer.write(b"n")  # already on (only) last page with default-size terminal
            await writer.drain()
            data = await _read_until_quiet(reader)
            # No redraw, no notice message -- just a bell (nothing about
            # the page actually changed).
            assert b"\a" in data
            assert b"Already on the last page." not in data
            assert b"(page " not in data
            writer.write(b"b")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
            data = await _read_until_quiet(reader)
            assert b"01. (#1) item1" in data
            assert b"02. (#2) item2" in data
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
            await reader.readexactly(9)
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
    assert b"01. (#19) item19" in data
    assert b"02. (#20) item20" in data


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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
            data = await _read_until_quiet(reader)
            # Position 1 on screen ("01.") shows stable ID 205, not "1" —
            # and position 2 ("02.") shows stable ID 7, not "2".
            assert b"01. (#205) gamma" in data
            assert b"02. (#7) alpha" in data
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
            await reader.readexactly(9)
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
