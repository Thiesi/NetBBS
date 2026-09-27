"""
SysOp break-in chat (issue #765, tracker #761): the SysOp takes over a
caller's screen for a two-pane chat, and afterwards the caller is back
exactly where they were.

The caller's own task is never cancelled or told anything. For the length
of the chat `Session.begin_break_in` diverts their keystrokes to this
module and holds their screen's output (kept in the session's screen
copy, not sent). The chat is drawn on their terminal with
`Session.write_through`, and `Session.end_break_in` repaints their
terminal from the copy -- including anything their screen wrote in the
meantime -- before giving input and output back. A half-typed line is
simply still in their screen's own line state, and still on the
repainted screen.

Both terminals show the same layout, each at its own size: the SysOp's
pane above, the caller's below, each side's typing appearing as it is
typed, the classic two-pane SysOp chat. Only the SysOp ends it (Esc).
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import time
from dataclasses import dataclass, field

from netbbs.auth.users import User
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.live_screen import KeyOutcome, fill_row, paint_text, run_live_screen
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.rendering.ansi import CSI
from netbbs.rendering.screen_buffer import ScreenBuffer, Snapshot, diff_ansi, full_render_ansi
from netbbs.rendering.theme import ACCENT_COLOR, ERROR_COLOR, HEADER_COLOR, PRIVILEGE_COLOR, VALUE_COLOR
from netbbs.rendering.width import wrap_to_width

_logger = logging.getLogger(__name__)

#: How often the SysOp's side redraws, to show the caller's typing.
REFRESH_SECONDS = 0.1

#: The most either side's scrollback keeps; older lines scroll away.
_MAX_LINES = 200

_SELECTED_BG = 238


@dataclass
class Pane:
    """One side's conversation: finished lines and the line being typed."""

    lines: list[str] = field(default_factory=list)
    typing: str = ""

    def type(self, text: str) -> None:
        self.typing = (self.typing + text)[:500]

    def backspace(self) -> None:
        self.typing = self.typing[:-1]

    def enter(self) -> None:
        self.lines.append(self.typing)
        del self.lines[:-_MAX_LINES]
        self.typing = ""


@dataclass
class ChatState:
    sysop_name: str
    caller_name: str
    sysop: Pane = field(default_factory=Pane)
    caller: Pane = field(default_factory=Pane)
    caller_gone: bool = False


def _paint_pane(buffer: ScreenBuffer, top: int, rows: int, label: str, color: int, pane: Pane) -> None:
    fill_row(buffer, top, bg=_SELECTED_BG)
    paint_text(buffer, top, 1, label, fg=color, bg=_SELECTED_BG, bold=True)
    body = rows - 1
    if body <= 0:
        return
    width = max(1, buffer.width - 1)
    wrapped: list[str] = []
    for line in pane.lines:
        wrapped.extend(wrap_to_width(line, width) or [""])
    typing = wrap_to_width(pane.typing + "_", width) or ["_"]
    wrapped.extend(typing)
    for offset, line in enumerate(wrapped[-body:]):
        paint_text(buffer, top + 1 + offset, 1, line, fg=VALUE_COLOR)


def paint_chat(buffer: ScreenBuffer, state: ChatState, *, for_sysop: bool) -> None:
    """The chat as either side sees it: a title row, the SysOp's pane,
    then the caller's."""
    if for_sysop:
        # ASCII only: neither side's Unicode preference is known here.
        title = f"Break-in chat with {state.caller_name} - Esc ends"
        if state.caller_gone:
            title = f"{state.caller_name} has disconnected - any key returns"
    else:
        title = f"The SysOp ({state.sysop_name}) has opened a chat with you"
    paint_text(buffer, 0, 0, title, fg=ERROR_COLOR if state.caller_gone else HEADER_COLOR, bold=True)
    body = buffer.height - 1
    upper = body // 2
    _paint_pane(buffer, 1, upper, f"SysOp: {state.sysop_name}", PRIVILEGE_COLOR, state.sysop)
    _paint_pane(buffer, 1 + upper, body - upper, state.caller_name, ACCENT_COLOR, state.caller)


class _CallerScreen:
    """Draws the chat on the caller's terminal, past the hold, with the
    same diff-only repaint the live screen uses on the SysOp's."""

    def __init__(self, session: Session, state: ChatState) -> None:
        self.session = session
        self.state = state
        self.previous: Snapshot | None = None
        self.size: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

    async def render(self) -> None:
        async with self.lock:
            size = (self.session.terminal_width, self.session.terminal_height)
            buffer = ScreenBuffer(*size)
            paint_chat(buffer, self.state, for_sysop=False)
            snapshot = buffer.snapshot()
            if self.previous is None or size != self.size:
                frame = f"{CSI}r{CSI}?25l" + full_render_ansi(snapshot)
            else:
                frame = diff_ansi(self.previous, snapshot)
            if frame:
                await self.session.write_through(frame)
            self.previous, self.size = snapshot, size


class _CallerKeys:
    """Turns the caller's diverted bytes into typing: UTF-8 text, Enter,
    Backspace; escape sequences (arrows and such) are skipped whole."""

    def __init__(self, pane: Pane, session: Session) -> None:
        self.pane = pane
        self.session = session
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.in_escape = False
        self.in_csi = False

    def feed(self, value: int) -> None:
        for ch in self.decoder.decode(bytes([value])):
            if self.in_csi:
                if "\x40" <= ch <= "\x7e":
                    self.in_csi = False
                continue
            if self.in_escape:
                self.in_escape = False
                self.in_csi = ch in "[O"
                continue
            if ch == "\x1b":
                self.in_escape = True
            elif ch == "\r":
                self.pane.enter()
            elif ch in "\x08\x7f":
                self.pane.backspace()
            elif ch.isprintable():
                # The caller's own screen is reading a password: whatever
                # they type goes to the chat, and is never shown there.
                self.pane.type("*" if self.session.reading_secret else ch)


def _still_connected(registry: ActiveSessionRegistry, session: Session) -> bool:
    return any(entry.session is session for entry in registry.list_entries())


async def run_break_in(
    sysop_session: Session, actor: User, registry: ActiveSessionRegistry, target: Session, caller_name: str,
) -> None:
    """The whole break-in: take over `target`, chat until the SysOp presses
    Esc (or either side goes), then put `target` back as it was.

    The caller must be refused beforehand for a running binary transfer;
    see `refusal`."""
    state = ChatState(sysop_name=actor.username, caller_name=caller_name)
    started = time.monotonic()
    keys = target.begin_break_in()
    _logger.info("break-in: %s opened a chat with %s", actor.username, caller_name)
    registry.note_event(f"chat by {actor.username}: {caller_name}")
    caller_screen = _CallerScreen(target, state)
    caller_keys = _CallerKeys(state.caller, target)
    changed = asyncio.Event()

    async def pump_caller() -> None:
        while True:
            caller_keys.feed(await keys.get())
            changed.set()

    async def draw_caller() -> None:
        while True:
            # A resize arrives with no keystroke; the half-second check
            # catches it, and costs nothing when the frame is unchanged.
            try:
                await asyncio.wait_for(changed.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            changed.clear()
            try:
                await caller_screen.render()
            except SessionClosedError:
                state.caller_gone = True
                return

    async def on_key(key: EditorKey) -> KeyOutcome:
        if state.caller_gone or key.kind is EditorKeyKind.ESCAPE:
            return KeyOutcome.EXIT
        if key.kind is EditorKeyKind.ENTER:
            state.sysop.enter()
        elif key.kind in (EditorKeyKind.BACKSPACE, EditorKeyKind.DELETE):
            state.sysop.backspace()
        elif key.kind is EditorKeyKind.CTRL and key.char == "h":
            state.sysop.backspace()
        elif key.kind is EditorKeyKind.CHAR and key.char:
            if key.char in "\r\n":
                state.sysop.enter()
            elif key.char in "\x08\x7f":
                state.sysop.backspace()
            elif key.char.isprintable():
                state.sysop.type(key.char)
        changed.set()
        return KeyOutcome.CONTINUE

    def paint(buffer: ScreenBuffer) -> None:
        if not state.caller_gone and not _still_connected(registry, target):
            state.caller_gone = True
        paint_chat(buffer, state, for_sysop=True)

    helpers = [asyncio.create_task(pump_caller()), asyncio.create_task(draw_caller())]
    try:
        await target.break_in_began()
        await caller_screen.render()
        await run_live_screen(
            sysop_session, paint=paint, on_key=on_key, on_notice=lambda text: None, interval=REFRESH_SECONDS,
        )
    finally:
        for task in helpers:
            task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        try:
            await target.end_break_in()
        except (SessionClosedError, OSError):
            pass  # the caller is gone; nothing to put back
        _logger.info(
            "break-in: %s closed the chat with %s after %d s",
            actor.username, caller_name, int(time.monotonic() - started),
        )


def refusal(target: Session) -> str | None:
    """Why `target` can't be broken into right now, or `None`."""
    if target.binary_transfer_active:
        return "is in the middle of a file transfer; a chat would corrupt it"
    if target.in_break_in:
        return "is already in a break-in chat"
    if target.reading_secret:
        return "is typing a password; try again in a moment"
    return None
