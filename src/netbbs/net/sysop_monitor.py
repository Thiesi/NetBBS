"""
Operations → Node → Monitor (issue #763, tracker #761): a live,
self-refreshing table of every connected session, in the spirit of
`top`/`btop`, with the actions a SysOp takes on one of them.

Built on `netbbs.net.live_screen`. Everything a tick paints comes from
memory: the session registry (who, since when, idle time and activity,
issue #762), the maintenance and shutdown schedulers, and the MRC bridge's
status snapshot. The database is touched only on entry (display
preferences) and by an action the SysOp takes (Kick is the Who screen's
own disconnect draft, audit log entry included), never by the refresh.

[S]noop (#764) shows the selected caller's screen, live, from the
server-side copy every session keeps (`Session.screen_copy`). It is
silent for the caller by decision (tracker #761), disclosed in the
caller-facing help, and every snoop is written to the node log.
Break-in chat (#765) joins the action bar when it lands.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Awaitable, Callable

from netbbs.auth.users import User
from netbbs.net import notices
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.live_screen import (
    HIDE_CURSOR,
    SHOW_CURSOR,
    KeyOutcome,
    fill_row,
    paint_text,
    run_live_screen,
    write_quietly,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.session_activity import describe, records_activity
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls, format_remaining_seconds
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import sanitize_text
from netbbs.rendering.gradient import gradient_color
from netbbs.rendering.ansi import clear_line, colored, move_cursor, strip_ansi
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer
from netbbs.rendering.theme import (
    ACCENT_COLOR,
    ALERT_COLOR,
    EMPHASIS_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    MENU_KEY_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    PRIVILEGE_COLOR,
    RULE_COLOR,
    SELF_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
)
from netbbs.rendering.width import cut_to_width, display_width, wrap_to_width
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import resolve_display_preferences

#: Seconds between refreshes. Idle times count in seconds, and two seconds
#: keeps them visibly moving without making the screen restless.
REFRESH_SECONDS = 2.0

#: The snoop view follows typing, so it refreshes much faster. Each tick
#: only copies cells the caller's own output already produced.
SNOOP_REFRESH_SECONDS = 0.25

_logger = logging.getLogger(__name__)

#: The selected row's background.
_SELECTED_BG = 238

#: The "doing" column never gets narrower than this while another column
#: can still be dropped to make room for it.
DOING_MIN_WIDTH = 20


@dataclass(frozen=True)
class Column:
    key: str
    heading: str
    width: int
    right: bool = False


#: Left to right. "doing" takes whatever width is left over.
COLUMNS = (
    Column("id", "#", 3, right=True),
    Column("user", "USER", 12),
    Column("via", "VIA", 4),
    Column("peer", "FROM", 15),
    Column("on", "ON", 5, right=True),
    Column("idle", "IDLE", 5, right=True),
    Column("term", "TERM", 7),
)

#: Which columns give way first on a narrow terminal (maintainer decision,
#: 2026-09-27): whole columns are dropped, rows never wrap. User, idle and
#: doing always stay.
DROP_ORDER = ("peer", "term", "via")

_VIA = {"telnet": "tel", "ssh": "ssh", "web": "web", "local": "loc"}

ORDERS = ("time on", "idle", "user")


@dataclass(frozen=True)
class Glyphs:
    """The screen's decorative characters, in Unicode or plain ASCII for a
    SysOp who turned Unicode styling off."""

    separator: str
    ellipsis: str
    dot: str
    more: str
    rule: str
    select_hint: str


UNICODE_GLYPHS = Glyphs(" › ", "…", " · ", "↓", "─", "↑↓ select")
ASCII_GLYPHS = Glyphs(" > ", "...", " - ", "v", "-", "Up/Dn select")


def layout_columns(width: int, *, id_width: int = 3) -> tuple[list[Column], int]:
    """The columns that fit `width`, and the width left for "doing".

    The id column grows to `id_width`: session ids are never reused within
    a run, so after a thousand connections they need four digits, and a
    cut id would make two rows look like one. Columns in `DROP_ORDER` are
    dropped one at a time until "doing" has `DOING_MIN_WIDTH`. On a
    terminal too narrow even then, "doing" gets what is left, possibly
    nothing."""
    columns = [
        Column(c.key, c.heading, max(c.width, id_width), c.right) if c.key == "id" else c for c in COLUMNS
    ]
    dropping = list(DROP_ORDER)

    def doing_width() -> int:
        # One space after every fixed column.
        return width - sum(column.width + 1 for column in columns)

    while doing_width() < DOING_MIN_WIDTH and dropping:
        key = dropping.pop(0)
        columns = [column for column in columns if column.key != key]
    return columns, max(0, doing_width())


def short_duration(seconds: float) -> str:
    """At most five characters: `m:ss` under an hour, then `12h05`, then
    `3d04h`, and whole days from ten days on."""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"
    days = seconds // 86400
    if days >= 10:
        return f"{min(days, 9999)}d"
    return f"{days}d{seconds % 86400 // 3600:02d}h"


def fit_doing(
    trail: tuple[str, ...], width: int, *, authenticated: bool, glyphs: Glyphs = UNICODE_GLYPHS
) -> str:
    """The activity trail in `width` columns, keeping its most specific
    end: "… › Boards › Retro" says more than "Communities › Boar"."""
    text = describe(trail, authenticated=authenticated, separator=glyphs.separator)
    parts = list(trail)
    while display_width(text) > width and len(parts) > 1:
        parts.pop(0)
        text = glyphs.ellipsis + glyphs.separator + glyphs.separator.join(parts)
    return text


def sort_entries(entries: list[SessionSummary], order: str) -> list[SessionSummary]:
    if order == "idle":
        return sorted(entries, key=lambda e: (-e.idle_seconds, e.session_id))
    if order == "user":
        return sorted(entries, key=lambda e: ((e.username or "￿").lower(), e.session_id))
    return sorted(entries, key=lambda e: e.session_id)


@dataclass
class MonitorState:
    """What the SysOp has chosen on this screen; everything else is read
    fresh on each tick."""

    viewer: Session
    order: str = ORDERS[0]
    selected_id: int | None = None
    top: int = 0
    outcome: str = ""
    outcome_color: int = MUTED_COLOR
    timezone: datetime.tzinfo = datetime.timezone.utc
    visible_ids: list[int] = field(default_factory=list)
    # Resolved once on entry, so ticks stay database-free: the node's
    # branding for the name in the header, and the SysOp's Unicode choice.
    header_color: int = HEADER_COLOR
    accent_color: int = ACCENT_COLOR
    name_gradient: str | None = None
    glyphs: Glyphs = UNICODE_GLYPHS

    def say(self, text: str, color: int = MUTED_COLOR) -> None:
        self.outcome, self.outcome_color = text, color


def _name_cell(entry: SessionSummary, state: MonitorState) -> tuple[str, int]:
    if entry.username is None:
        return "(login)", MUTED_COLOR
    if entry.session is state.viewer:
        return sanitize_text(entry.username), SELF_COLOR
    return sanitize_text(entry.username), PRIVILEGE_COLOR if entry.is_sysop else state.accent_color


def _cell_text(column: Column, entry: SessionSummary) -> str:
    session = entry.session
    if column.key == "id":
        return str(entry.session_id)
    if column.key == "via":
        name = getattr(session, "transport_name", "")
        return _VIA.get(name, name[:4])
    if column.key == "peer":
        return entry.peer_address or "-"
    if column.key == "on":
        return short_duration(entry.connected_seconds)
    if column.key == "idle":
        return short_duration(entry.idle_seconds)
    if column.key == "term":
        return f"{getattr(session, 'terminal_width', 0)}x{getattr(session, 'terminal_height', 0)}"
    raise KeyError(column.key)


def _fit(text: str, column: Column, ellipsis: str = "…") -> str:
    if len(text) > column.width:
        # Marked, so two long names sharing a prefix don't look identical.
        text = text[: max(0, column.width - len(ellipsis))] + ellipsis
        text = text[: column.width]
    return text.rjust(column.width) if column.right else text.ljust(column.width)


def _paint_header(buffer: ScreenBuffer, state: MonitorState, controls: NodeControls, count: int, uptime: float) -> None:
    """Node name, uptime and caller count, then the operational flags.
    The flags are what the header is for, so on a narrow terminal the
    name is cut and the uptime dropped before any flag is."""
    glyphs = state.glyphs
    flags = _header_flags(controls)
    flags_width = sum(display_width(glyphs.dot) + display_width(text) for text, _color in flags)
    room = buffer.width - flags_width
    name = sanitize_text(state.viewer.node_display_name)
    if display_width(name) > max(1, room):
        # Display columns, not characters: a name of wide glyphs takes two
        # columns each and would otherwise still push the flags off screen.
        name = cut_to_width(name, max(1, room - display_width(glyphs.ellipsis))) + glyphs.ellipsis
    col = _paint_node_name(buffer, name, state)
    callers = "caller" if count == 1 else "callers"
    for part in (f"{glyphs.dot}{count} {callers}", f"{glyphs.dot}up {short_duration(uptime)}"):
        if col + display_width(part) <= room:
            col = paint_text(buffer, 0, col, part, fg=METADATA_COLOR)
    for text, color in flags:
        col = paint_text(buffer, 0, col, glyphs.dot, fg=METADATA_COLOR)
        col = paint_text(buffer, 0, col, text, fg=color, bold=color == ALERT_COLOR)


def _paint_node_name(buffer: ScreenBuffer, name: str, state: MonitorState) -> int:
    """The node name with the node's own branding, as every screen's
    title shows it: its gradient if one is set, otherwise its header
    colour."""
    if not state.name_gradient or len(name) < 2:
        return paint_text(buffer, 0, 0, name, fg=state.header_color, bold=True)
    col = 0
    for index, ch in enumerate(name):
        color = gradient_color(state.name_gradient, index / (len(name) - 1), truecolor=False)
        col = paint_text(buffer, 0, col, ch, fg=color, bold=True)
    return col


def _header_flags(controls: NodeControls) -> list[tuple[str, int]]:
    """Most urgent first, so that on a terminal too narrow for all of them
    the one that clips is the least urgent."""
    flags: list[tuple[str, int]] = []
    if controls.shutdown_scheduler.is_scheduled():
        flags.append(
            (f"shutdown in {format_remaining_seconds(controls.shutdown_scheduler.remaining_seconds())}", ALERT_COLOR)
        )
    if controls.drain_scheduler.is_scheduled():
        flags.append((f"drain in {format_remaining_seconds(controls.drain_scheduler.remaining_seconds())}", ALERT_COLOR))
    if controls.maintenance.is_lockdown_active():
        flags.append(("MAINTENANCE", ALERT_COLOR))
    bridge = controls.mrc_bridge
    if bridge is not None:
        mrc_state = bridge.status().state.value
        if mrc_state != "disabled":
            flags.append((f"MRC {mrc_state}", SUCCESS_COLOR if mrc_state == "connected" else ERROR_COLOR))
    return flags


def _paint_action_bar(buffer: ScreenBuffer, row: int, state: MonitorState) -> None:
    """The keys, in full when they fit and shortened when they don't, so
    [Q]uit is never the part a narrow terminal cuts off."""
    full = [("S", "noop"), ("M", "essage"), ("K", "ick"), ("U", "nwind"), ("O", f"rder: {state.order}"), ("Q", "uit")]
    short = [("S", "noop"), ("M", "sg"), ("K", "ick"), ("U", "nwind"), ("O", "rder"), ("Q", "uit")]
    hint = state.glyphs.select_hint

    def width_of(items: list[tuple[str, str]], gap: int) -> int:
        return sum(len(key) + len(rest) + 2 + gap for key, rest in items)

    items, gap = full, 2
    if width_of(items, gap) + len(hint) > buffer.width:
        hint = ""
    if width_of(items, gap) > buffer.width:
        items, gap = short, 1
    if width_of(items, gap) > buffer.width:
        # The keys alone, still every one of them.
        items, gap = [(key, "") for key, _rest in short], 1
    col = 0
    for key, rest in items:
        col = paint_text(buffer, row, col, "[", fg=VALUE_COLOR)
        col = paint_text(buffer, row, col, key, fg=MENU_KEY_COLOR, bold=True)
        col = paint_text(buffer, row, col, "]" + rest + " " * gap, fg=VALUE_COLOR)
    if hint:
        paint_text(buffer, row, col, hint, fg=METADATA_COLOR)


def paint_monitor(buffer: ScreenBuffer, state: MonitorState, controls: NodeControls) -> None:
    """One frame. Pure apart from reading the in-memory node state."""
    registry = controls.session_registry
    entries = sort_entries(registry.list_entries(), state.order)
    height = buffer.height

    uptime = time.monotonic() - registry.started_monotonic
    _paint_header(buffer, state, controls, len(entries), uptime)

    # Bottom up: the action bar, the outcome line, then the event tail
    # under a rule when there is room for one.
    bar_row = height - 1
    # A long outcome or broadcast wraps upward, up to three rows, rather
    # than losing its tail at the screen edge.
    outcome_lines = wrap_to_width(state.outcome, buffer.width)[:3] if state.outcome else []
    outcome_row = height - 1 - max(1, len(outcome_lines))
    event_rows = 3 if height >= 18 else 1 if height >= 12 else 0
    events_top = outcome_row - event_rows
    table_bottom = events_top - (1 if event_rows else 0)  # the rule
    first_row = 2
    capacity = max(0, table_bottom - first_row)

    id_width = max((len(str(entry.session_id)) for entry in entries), default=1)
    columns, doing_width = layout_columns(buffer.width, id_width=id_width)
    col = 0
    for column in columns:
        col = paint_text(buffer, 1, col, _fit(column.heading, column) + " ", fg=LABEL_COLOR, bold=True)
    paint_text(buffer, 1, col, "DOING", width=doing_width, fg=LABEL_COLOR, bold=True)

    # Keep the selection on the same session as the list changes under it.
    ids = [entry.session_id for entry in entries]
    if state.selected_id not in ids:
        state.selected_id = ids[0] if ids else None
    index = ids.index(state.selected_id) if state.selected_id is not None else 0
    if capacity:
        if index < state.top:
            state.top = index
        elif index >= state.top + capacity:
            state.top = index - capacity + 1
        state.top = max(0, min(state.top, max(0, len(entries) - capacity)))
    visible = entries[state.top : state.top + capacity]
    state.visible_ids = [entry.session_id for entry in visible]

    if not entries:
        paint_text(buffer, first_row, 0, "No one is connected.", fg=MUTED_COLOR)
    for offset, entry in enumerate(visible):
        row = first_row + offset
        selected = entry.session_id == state.selected_id
        bg = _SELECTED_BG if selected else None
        if selected:
            fill_row(buffer, row, bg=bg)
        col = 0
        for column in columns:
            if column.key == "user":
                name, color = _name_cell(entry, state)
                text, bold = _fit(name, column, state.glyphs.ellipsis), entry.is_sysop
            else:
                text, color, bold = _fit(_cell_text(column, entry), column), VALUE_COLOR, False
                if column.key == "id":
                    color = METADATA_COLOR
            col = paint_text(buffer, row, col, text + " ", fg=color, bg=bg, bold=bold)
        doing = fit_doing(entry.activity, doing_width, authenticated=entry.username is not None, glyphs=state.glyphs)
        paint_text(buffer, row, col, doing, width=doing_width, fg=EMPHASIS_COLOR if selected else VALUE_COLOR, bg=bg)
    hidden_below = len(entries) - state.top - len(visible)
    if hidden_below > 0 and capacity:
        marker = f" {state.glyphs.more} {hidden_below} more "
        paint_text(buffer, table_bottom - 1, max(0, buffer.width - len(marker)), marker, fg=METADATA_COLOR)

    if event_rows:
        paint_text(buffer, table_bottom, 0, state.glyphs.rule * buffer.width, fg=RULE_COLOR)
        paint_text(buffer, table_bottom, 2, " recent ", fg=METADATA_COLOR)
        for offset, event in enumerate(registry.recent_events()[-event_rows:]):
            stamp = event.at.astimezone(state.timezone).strftime("%H:%M")
            col = paint_text(buffer, events_top + offset, 0, stamp + " ", fg=METADATA_COLOR)
            paint_text(buffer, events_top + offset, col, sanitize_text(event.text), fg=MUTED_COLOR)

    for offset, line in enumerate(outcome_lines):
        paint_text(buffer, outcome_row + offset, 0, line, fg=state.outcome_color)
    _paint_action_bar(buffer, bar_row, state)


def _selected_entry(state: MonitorState, controls: NodeControls) -> SessionSummary | None:
    for entry in controls.session_registry.list_entries():
        if entry.session_id == state.selected_id:
            return entry
    return None


def _label(entry: SessionSummary) -> str:
    if entry.username is not None:
        return sanitize_text(entry.username)
    return f"the caller at {entry.peer_address or 'an unknown address'}"


async def _ask_on_bottom_row(session: Session, prompt: str) -> str:
    """A one-line prompt on the last row, over the action bar. The caller
    repaints the whole screen afterwards."""
    await session.write(move_cursor(session.terminal_height, 1) + clear_line() + SHOW_CURSOR)
    await write_prompt(session, prompt)
    try:
        return (await session.read_line()).strip()
    finally:
        await write_quietly(session, HIDE_CURSOR)


async def _message(session: Session, state: MonitorState, controls: NodeControls, entry: SessionSummary) -> None:
    text = await _ask_on_bottom_row(session, f"Message to {_label(entry)} (Enter to cancel): ")
    if not text:
        state.say("No message sent.")
        return
    delivered = await controls.session_registry.notify_one(
        entry.session,
        colored(f"\r\n*** Message from the SysOp: {sanitize_text(text)} ***", fg_color=ALERT_COLOR, bold=True),
    )
    if delivered:
        state.say(f"Message sent to {_label(entry)}.", SUCCESS_COLOR)
    else:
        state.say(f"{_label(entry)} is no longer connected.", ERROR_COLOR)


#: How an outcome line reads, from how it starts (`admin_flow._announce_line`
#: colours the same way).
_NEUTRAL_OUTCOMES = ("Cancelled", "No change", "Not ")


def _take_outcome(session: Session, state: MonitorState) -> None:
    """Show the last outcome another screen announced (the disconnect
    draft's), on this screen's outcome line."""
    lines = [strip_ansi(line).strip() for line in notices.take_notices(session)]
    lines = [line for line in lines if line]
    if not lines:
        return
    text = lines[-1]
    if "gone" in text or "no longer" in text:
        state.say(text, ERROR_COLOR)
    elif text.startswith(_NEUTRAL_OUTCOMES):
        state.say(text, MUTED_COLOR)
    else:
        state.say(text, SUCCESS_COLOR)


def _unwind(state: MonitorState, controls: NodeControls, entry: SessionSummary) -> None:
    name = _label(entry)
    if entry.username is None:
        state.say(f"{name} has not logged in yet.", ERROR_COLOR)
    elif controls.session_registry.request_level_unwind(entry.session):
        state.say(f"{name} is on their way back to the main menu.", SUCCESS_COLOR)
    else:
        # Only the main menu catches an unwind; outside it (logging in or
        # off) the cancellation would end the session, so it is refused.
        state.say(f"{name} can't be sent back right now. Try again in a moment.", ERROR_COLOR)


def paint_snoop(
    buffer: ScreenBuffer,
    entry: SessionSummary,
    controls: NodeControls,
    *,
    glyphs: Glyphs = UNICODE_GLYPHS,
    notice: str = "",
) -> None:
    """One frame of the snoop view: a header row naming the caller, and
    under it their screen as the copy has it, cropped to this terminal,
    with their cursor shown in reverse video."""
    name = _label(entry)
    live = any(e.session is entry.session for e in controls.session_registry.list_entries())
    if not live:
        fill_row(buffer, 0, bg=_SELECTED_BG)
        paint_text(buffer, 0, 0, f"{name} has disconnected. Any key returns.", fg=ERROR_COLOR, bg=_SELECTED_BG)
        return
    copy = entry.session.screen_copy()
    cropped = copy.width > buffer.width or copy.height > buffer.height - 1
    header = f"Watching {name}{glyphs.dot}{copy.width}x{copy.height}"
    if cropped:
        header += " (cropped)"
    header += f"{glyphs.dot}any key stops"
    fill_row(buffer, 0, bg=_SELECTED_BG)
    if notice:
        # A message or broadcast for the SysOp takes the header row: it
        # matters more than the caption.
        paint_text(buffer, 0, 0, notice, fg=ALERT_COLOR, bg=_SELECTED_BG, bold=True)
    else:
        paint_text(buffer, 0, 0, header, fg=EMPHASIS_COLOR, bg=_SELECTED_BG, bold=True)
    snapshot = copy.snapshot()
    rows = min(copy.height, buffer.height - 1)
    cols = min(copy.width, buffer.width)
    for row in range(rows):
        source = snapshot[row]
        for col in range(cols):
            buffer.put_cell(row + 1, col, source[col])
        if cols < copy.width and cols and source[cols - 1].char and not source[cols].char:
            # A wide glyph cut by the crop would wrap on the SysOp's
            # terminal: its visible half is blanked instead.
            buffer.put_cell(row + 1, cols - 1, Cell())
    if copy.cursor_visible and copy.row < rows and copy.col < cols:
        cell = snapshot[copy.row][copy.col]
        buffer.put_cell(copy.row + 1, copy.col, replace(cell, char=cell.char or " ", reverse=not cell.reverse))


async def snoop_screen(
    session: Session, actor: User, controls: NodeControls, entry: SessionSummary, *, glyphs: Glyphs = UNICODE_GLYPHS
) -> None:
    """Watch `entry`'s screen until any key. Silent for the caller; the
    node log and the Monitor's event tail record who watched whom, and
    for how long."""
    name = _label(entry)
    started = time.monotonic()
    _logger.info("snoop: %s started watching session %d (%s)", actor.username, entry.session_id, name)
    controls.session_registry.note_event(f"snoop by {actor.username}: {name}")

    async def on_key(key: EditorKey) -> KeyOutcome:
        return KeyOutcome.EXIT

    # Only the latest notice is shown, so only the latest is kept: a
    # stream of messages must not grow memory for as long as this is open.
    notices: list[str] = []

    def on_notice(text: str) -> None:
        notices[:] = [" ".join(strip_ansi(text).split())]

    try:
        await run_live_screen(
            session,
            paint=lambda buffer: paint_snoop(
                buffer, entry, controls, glyphs=glyphs, notice=notices[-1] if notices else "",
            ),
            on_key=on_key,
            on_notice=on_notice,
            interval=SNOOP_REFRESH_SECONDS,
        )
    finally:
        _logger.info(
            "snoop: %s stopped watching session %d (%s) after %d s",
            actor.username, entry.session_id, name, int(time.monotonic() - started),
        )


def _move(state: MonitorState, controls: NodeControls, step: int) -> None:
    ids = [e.session_id for e in sort_entries(controls.session_registry.list_entries(), state.order)]
    if not ids:
        return
    index = ids.index(state.selected_id) if state.selected_id in ids else 0
    state.selected_id = ids[max(0, min(len(ids) - 1, index + step))]


@records_activity("Monitor")
async def monitor_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    controls: NodeControls,
    *,
    disconnect: Callable[[SessionSummary], Awaitable[None]],
) -> None:
    """Show the live monitor until the SysOp leaves it.

    `disconnect` is the Who screen's disconnect draft
    (`admin_flow.disconnect_session_draft`): an optional message and an
    explicit Disconnect, not a chain of questions (design doc §3.5), and
    one audit-log path for both screens."""
    _format, timezone_name = await lane.run(resolve_display_preferences)
    try:
        timezone: datetime.tzinfo = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = datetime.timezone.utc
    state = MonitorState(
        viewer=session,
        timezone=timezone,
        header_color=await lane.run(effective_header_color_256),
        accent_color=await lane.run(effective_accent_color_256),
        name_gradient=session.node_name_gradient,
        glyphs=UNICODE_GLYPHS if await lane.run(unicode_style_enabled, actor) else ASCII_GLYPHS,
    )

    async def on_key(key: EditorKey) -> KeyOutcome:
        state.say("")
        if key.kind is EditorKeyKind.UP:
            _move(state, controls, -1)
            return KeyOutcome.CONTINUE
        if key.kind is EditorKeyKind.DOWN:
            _move(state, controls, 1)
            return KeyOutcome.CONTINUE
        if key.kind is EditorKeyKind.PAGE_UP:
            _move(state, controls, -max(1, len(state.visible_ids)))
            return KeyOutcome.CONTINUE
        if key.kind is EditorKeyKind.PAGE_DOWN:
            _move(state, controls, max(1, len(state.visible_ids)))
            return KeyOutcome.CONTINUE
        if key.kind is EditorKeyKind.ESCAPE:
            return KeyOutcome.EXIT
        if key.kind is not EditorKeyKind.CHAR or not key.char:
            return KeyOutcome.CONTINUE
        choice = key.char.lower()
        if choice in ("q", "b"):
            return KeyOutcome.EXIT
        if choice == "o":
            state.order = ORDERS[(ORDERS.index(state.order) + 1) % len(ORDERS)]
            return KeyOutcome.CONTINUE
        if choice not in ("s", "m", "k", "u"):
            return KeyOutcome.CONTINUE
        entry = _selected_entry(state, controls)
        if entry is None:
            state.say("That session is gone.", ERROR_COLOR)
            return KeyOutcome.CONTINUE
        if entry.session is session:
            state.say("That's your own session.", MUTED_COLOR)
            return KeyOutcome.CONTINUE
        if choice == "u":
            _unwind(state, controls, entry)
            return KeyOutcome.CONTINUE
        if choice == "s":
            await snoop_screen(session, actor, controls, entry, glyphs=state.glyphs)
            return KeyOutcome.REPAINT
        if choice == "m":
            await _message(session, state, controls, entry)
        else:
            # The draft is an ordinary screen: it needs the cursor back.
            await session.write(SHOW_CURSOR)
            try:
                await disconnect(entry)
            finally:
                await write_quietly(session, HIDE_CURSOR)
            _take_outcome(session, state)
        return KeyOutcome.REPAINT

    def on_notice(text: str) -> None:
        # A message or broadcast for the SysOp: shown on the outcome line
        # rather than written over the table.
        state.say(" ".join(strip_ansi(text).split()), ALERT_COLOR)

    await run_live_screen(
        session,
        paint=lambda buffer: paint_monitor(buffer, state, controls),
        on_key=on_key,
        on_notice=on_notice,
        interval=REFRESH_SECONDS,
    )
