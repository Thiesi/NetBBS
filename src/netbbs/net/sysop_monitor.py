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

Snoop (#764) and break-in chat (#765) join the action bar when they land.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
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
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls, format_remaining_seconds
from netbbs.rendering import sanitize_text
from netbbs.rendering.ansi import clear_line, colored, move_cursor, strip_ansi
from netbbs.rendering.screen_buffer import ScreenBuffer
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
from netbbs.rendering.width import display_width
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import resolve_display_preferences

#: Seconds between refreshes. Idle times count in seconds, and two seconds
#: keeps them visibly moving without making the screen restless.
REFRESH_SECONDS = 2.0

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


def fit_doing(trail: tuple[str, ...], width: int, *, authenticated: bool) -> str:
    """The activity trail in `width` columns, keeping its most specific
    end: "… › Boards › Retro" says more than "Communities › Boar"."""
    text = describe(trail, authenticated=authenticated)
    parts = list(trail)
    while display_width(text) > width and len(parts) > 1:
        parts.pop(0)
        text = "… › " + " › ".join(parts)
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

    def say(self, text: str, color: int = MUTED_COLOR) -> None:
        self.outcome, self.outcome_color = text, color


def _name_cell(entry: SessionSummary, viewer: Session) -> tuple[str, int]:
    if entry.username is None:
        return "(login)", MUTED_COLOR
    if entry.session is viewer:
        return sanitize_text(entry.username), SELF_COLOR
    return sanitize_text(entry.username), PRIVILEGE_COLOR if entry.is_sysop else ACCENT_COLOR


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


def _fit(text: str, column: Column) -> str:
    if len(text) > column.width:
        # Marked, so two long names sharing a prefix don't look identical.
        text = text[: column.width - 1] + "…"
    return text.rjust(column.width) if column.right else text.ljust(column.width)


def _paint_header(buffer: ScreenBuffer, state: MonitorState, controls: NodeControls, count: int, uptime: float) -> None:
    col = paint_text(buffer, 0, 0, sanitize_text(state.viewer.node_display_name), fg=HEADER_COLOR, bold=True)
    callers = "caller" if count == 1 else "callers"
    col = paint_text(buffer, 0, col, f" · up {short_duration(uptime)} · {count} {callers}", fg=METADATA_COLOR)
    flags: list[tuple[str, int]] = []
    if controls.maintenance.is_lockdown_active():
        flags.append(("MAINTENANCE", ALERT_COLOR))
    if controls.drain_scheduler.is_scheduled():
        flags.append((f"drain in {format_remaining_seconds(controls.drain_scheduler.remaining_seconds())}", ALERT_COLOR))
    if controls.shutdown_scheduler.is_scheduled():
        flags.append(
            (f"shutdown in {format_remaining_seconds(controls.shutdown_scheduler.remaining_seconds())}", ALERT_COLOR)
        )
    bridge = controls.mrc_bridge
    if bridge is not None:
        mrc_state = bridge.status().state.value
        if mrc_state != "disabled":
            flags.append((f"MRC {mrc_state}", SUCCESS_COLOR if mrc_state == "connected" else ERROR_COLOR))
    for text, color in flags:
        col = paint_text(buffer, 0, col, " · ", fg=METADATA_COLOR)
        col = paint_text(buffer, 0, col, text, fg=color, bold=color == ALERT_COLOR)


def _paint_action_bar(buffer: ScreenBuffer, row: int, state: MonitorState) -> None:
    """The keys, in full when they fit and shortened when they don't, so
    [Q]uit is never the part a narrow terminal cuts off."""
    full = [("M", "essage"), ("K", "ick"), ("U", "nwind"), ("O", f"rder: {state.order}"), ("Q", "uit")]
    short = [("M", "sg"), ("K", "ick"), ("U", "nwind"), ("O", "rder"), ("Q", "uit")]
    hint = "↑↓ select"

    def width_of(items: list[tuple[str, str]], gap: int) -> int:
        return sum(len(key) + len(rest) + 2 + gap for key, rest in items)

    items, gap = full, 2
    if width_of(items, gap) + len(hint) > buffer.width:
        hint = ""
    if width_of(items, gap) > buffer.width:
        items, gap = short, 1
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
    outcome_row = height - 2
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
                name, color = _name_cell(entry, state.viewer)
                text, bold = _fit(name, column), entry.is_sysop
            else:
                text, color, bold = _fit(_cell_text(column, entry), column), VALUE_COLOR, False
                if column.key == "id":
                    color = METADATA_COLOR
            col = paint_text(buffer, row, col, text + " ", fg=color, bg=bg, bold=bold)
        doing = fit_doing(entry.activity, doing_width, authenticated=entry.username is not None)
        paint_text(buffer, row, col, doing, width=doing_width, fg=EMPHASIS_COLOR if selected else VALUE_COLOR, bg=bg)
    hidden_below = len(entries) - state.top - len(visible)
    if hidden_below > 0 and capacity:
        marker = f" ↓ {hidden_below} more "
        paint_text(buffer, table_bottom - 1, max(0, buffer.width - len(marker)), marker, fg=METADATA_COLOR)

    if event_rows:
        paint_text(buffer, table_bottom, 0, "─" * buffer.width, fg=RULE_COLOR)
        paint_text(buffer, table_bottom, 2, " recent ", fg=METADATA_COLOR)
        for offset, event in enumerate(registry.recent_events()[-event_rows:]):
            stamp = event.at.astimezone(state.timezone).strftime("%H:%M")
            col = paint_text(buffer, events_top + offset, 0, stamp + " ", fg=METADATA_COLOR)
            paint_text(buffer, events_top + offset, col, sanitize_text(event.text), fg=MUTED_COLOR)

    if state.outcome:
        paint_text(buffer, outcome_row, 0, state.outcome, fg=state.outcome_color)
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
    state = MonitorState(viewer=session, timezone=timezone)

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
        if choice not in ("m", "k", "u"):
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
        if choice == "m":
            await _message(session, state, controls, entry)
        else:
            await disconnect(entry)
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
