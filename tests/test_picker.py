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


# -- empty list / basic selection / quit -------------------------------


def test_empty_list_shows_message_and_returns_none():
    result = {}

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, [], name_of=lambda x: x, title="Test", empty_message="Nothing here."
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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


def test_quit_returns_none():
    result = {}
    items = ["alpha", "beta"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            await _read_until_quiet(reader)
            writer.write(b"q")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_invalid_two_digit_selection_shows_error_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            assert b"Invalid selection." in data
            writer.write(b"01")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] == "a"


def test_unknown_command_letter_shows_error_and_stays_in_picker():
    result = {}
    items = ["a"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            assert b"Unknown command." in data
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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


# -- goto --------------------------------------------------------------


def test_goto_absolute_index():
    result = {}
    items = [f"item{i}" for i in range(1, 21)]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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


def test_prev_on_first_page_shows_notice_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            assert b"Already on the first page." in data
            writer.write(b"q")
            await writer.drain()
            await _read_until_quiet(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert result["value"] is None


def test_next_on_last_page_shows_notice_and_stays_in_picker():
    result = {}
    items = ["a", "b"]

    async def handler(session: Session):
        result["value"] = await pick_item(
            session, items, name_of=lambda x: x, title="I", empty_message="none"
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
            assert b"Already on the last page." in data
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
        )

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            data = await _read_until_quiet(reader)
            assert b"01. (#1) item1" in data
            assert b"02. (#2) item2" in data
            writer.write(b"q")
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
            session, items, name_of=lambda x: x, title="Items", empty_message="none"
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
            writer.write(b"q")
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
