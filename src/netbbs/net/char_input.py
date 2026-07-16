"""
Transport-agnostic character-mode line/key reading, shared by
`netbbs.net.telnet` and `netbbs.net.ssh`.

Extracted from what was originally Telnet-only logic once SSH connectivity
needed the exact same behavior for the exact same reason: read raw bytes
one at a time, with the *server* doing echo, Backspace/Delete handling,
Enter detection, UTF-8 decoding, and discarding unsupported terminal
escape sequences as a complete unit — see `netbbs.net.telnet`'s module
docstring for why relying on a client's own local line editing was
abandoned there in the first place. SSH has an equivalent reason: by
default `asyncssh` provides its own client-visible line editing for PTY
sessions, and disabling it (`channel.set_line_mode(False)` +
`set_echo(False)`) hands over exactly the same kind of raw, unprocessed
byte stream Telnet's character-mode negotiation does — nothing client-side
to lean on, same problem, same solution.

A transport supplies raw bytes via the `ByteSource` protocol below; the
line/key-reading logic itself (backspace handling, UTF-8 continuation
bytes, escape-sequence discarding, the CR/LF line-ending dance, the
max-length cap) is verbatim-identical regardless of which transport sits
underneath — so it lives here once, not duplicated per transport.

Cursor-addressable line editing, command history, and Tab completion
(design doc §15 Phase 2, sign-off rounds 47/49, Tracks 5f/5g):
`move_cursor`/`redraw_tail`, `InputHistory`, and `apply_tab_completion`
below are written with no dependency on bytes or `ByteSource` at all
(pure `list[str]`/cursor-integer/`WriteFunc` manipulation) specifically
so `netbbs.net.web.WebSession` — which decodes a browser's `onData`
events into whole characters itself and deliberately does not share
this module's byte-oriented reading (round 25) — can still reuse this
*editing* logic instead of duplicating it a second time. Only the
raw-byte/UTF-8/`ByteSource` half stays genuinely separate between the
two.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Awaitable, Callable, Protocol, Sequence

from netbbs.net.session import SessionClosedError

# Control byte values relevant to character-mode line building.
_CR = 0x0D
_LF = 0x0A
_NUL = 0x00
_BS = 0x08  # Backspace
_DEL = 0x7F  # Delete — many terminals send this for the Backspace key
_ESC = 0x1B
_TAB = 0x09

# Bounded wait used when peeking for a byte that might not be coming (a
# following LF after a lone CR; the rest of an escape sequence) — short
# enough to be imperceptible when the byte does arrive (which happens
# essentially instantly for a real client sending a CRLF pair or a real
# escape sequence in one write), long enough to never falsely time out on
# a real, if slightly slow, connection.
_FOLLOWUP_BYTE_TIMEOUT = 0.05

# Defensive cap on a single line's length. Not a meaningful limit for any
# real use (post subjects/bodies, chat messages, usernames are all far
# shorter), just cheap insurance against a broken or malicious client
# sending unbounded data with no Enter — without this, the line buffer
# would grow without bound. Once hit, further characters are silently not
# appended (but Backspace and Enter still work normally).
_MAX_LINE_LENGTH = 4096

# One-byte lookahead pushback is stored on the source itself so both Telnet
# and SSH get identical behavior without duplicating buffering machinery in
# each transport. The source implementations are ordinary mutable session
# objects, and only this module reads/writes the private attribute.
_PUSHBACK_ATTR = "_netbbs_char_input_pushback"

# Escape sequences are terminal-emulator control messages, not bulk data —
# same reasoning as netbbs.net.telnet's subnegotiation bounds (issue #5). A
# CSI sequence's parameter bytes are capped in count, and the whole sequence
# (the initial peek plus the CSI parameter loop) is bounded by one total
# deadline rather than relying on _FOLLOWUP_BYTE_TIMEOUT resetting on every
# legitimately-arriving byte — a client that keeps a CSI sequence "alive" by
# continuously sending parameter bytes just under that per-byte timeout would
# otherwise never trip either individual read's own bound. 32 bytes is
# generous headroom for any real terminal's CSI sequences (even a modified
# key combo like Ctrl+Up, `ESC[1;5A`, is under 10 bytes); 1 second matches
# the subnegotiation deadline, keeping both "protocol control message"
# bounds consistent with each other.
_MAX_ESCAPE_SEQUENCE_LENGTH = 32
_ESCAPE_SEQUENCE_TIMEOUT = 1.0

# Recognized CSI final bytes with no parameter bytes -- plain arrow keys
# plus the (less universal, but real) direct Home/End forms some
# terminals send. Anything else -- modified combos like Ctrl+Up
# (`ESC[1;5A`), function keys, etc. -- stays unrecognized/discarded,
# same "not supported in this pass" scope this module has always had,
# just narrower now that *something* is recognized.
_CSI_FINAL_TO_KEY: dict[int, str] = {
    0x41: "UP",
    0x42: "DOWN",
    0x43: "RIGHT",
    0x44: "LEFT",
    0x48: "HOME",
    0x46: "END",
}

# Recognized CSI "tilde" forms: ESC [ <param> ~ -- the alternate Home/
# End encoding some terminals use, plus Delete/Insert and Page Up/Down
# (design doc -- welcome banner round B1, for netbbs.net.ansi_editor),
# none of which have a plain-letter CSI form at all.
_CSI_TILDE_TO_KEY: dict[bytes, str] = {
    b"1": "HOME",
    b"4": "END",
    b"3": "DELETE",
    b"2": "INSERT",
    b"5": "PAGE_UP",
    b"6": "PAGE_DOWN",
}

# SS3 forms (ESC O <letter>) -- some terminals' "application cursor key
# mode" encoding, seen for arrows and occasionally Home/End.
_SS3_TO_KEY: dict[str, str] = {
    "A": "UP",
    "B": "DOWN",
    "C": "RIGHT",
    "D": "LEFT",
    "H": "HOME",
    "F": "END",
}


class ByteSource(Protocol):
    """What a transport must supply for `read_line`/`read_key` below to
    work — everything transport-specific (Telnet IAC negotiation, SSH
    terminal-size-changed notifications) is resolved *inside* these two
    methods, so the reading logic here never needs to know which
    transport it's running on."""

    async def read_byte(self) -> int | None:
        """
        Return the next real data byte, blocking until one arrives.

        Returns `None` if what was read was a pure transport-level action
        with no data significance (a Telnet negotiation sequence, an SSH
        terminal-resize notification) — callers should just loop and call
        this again. Raises `netbbs.net.session.SessionClosedError` if the
        connection closes while waiting.
        """
        ...

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        """
        Like `read_byte`, but give up and return `None` after `timeout`
        seconds if nothing arrives, or if the connection closes — used
        for peeking at a byte that might not be coming (the LF half of a
        CRLF pair; the rest of an escape sequence). Never raises
        `SessionClosedError`: an EOF encountered while merely peeking
        isn't itself an error the caller needs to react to here, unlike
        `read_byte`, which is always waiting for data that's actually
        needed.
        """
        ...


WriteFunc = Callable[[str], Awaitable[None]]


# -- cursor-addressable editing primitives (transport/byte agnostic) --------


def move_cursor(count: int, *, forward: bool) -> str:
    """The raw ANSI cursor-movement sequence to shift the terminal
    cursor `count` columns left or right within the current line, or
    `""` if `count <= 0` — callers can unconditionally call this without
    checking for the no-op case themselves."""
    if count <= 0:
        return ""
    return f"\x1b[{count}{'C' if forward else 'D'}"


async def redraw_tail(
    write: WriteFunc, *, terminal_col: int, edit_pos: int, line: list[str], new_cursor: int
) -> None:
    """
    The one redraw primitive every mid-line edit (insert, Backspace,
    Delete, full-line history recall) goes through: reposition the
    terminal cursor from wherever it currently is (`terminal_col`) to
    `edit_pos`, erase to the end of the visible line (`ESC[K`), reprint
    `line[edit_pos:]` — whatever the edit left there — then reposition
    to `new_cursor`.

    Reprinting only the tail (not the whole line) keeps this cheap for
    the common case of editing near the end of a short line, and erasing
    via `ESC[K` rather than manually overwriting with spaces means the
    terminal — not this code — is responsible for knowing how much
    trailing whitespace to clear, which is simpler and can't drift out
    of sync with the actual old line length.
    """
    if terminal_col > edit_pos:
        await write(move_cursor(terminal_col - edit_pos, forward=False))
    elif terminal_col < edit_pos:
        await write(move_cursor(edit_pos - terminal_col, forward=True))
    await write("\x1b[K")
    await write("".join(line[edit_pos:]))
    await write(move_cursor(len(line) - new_cursor, forward=False))


@dataclass
class InputHistory:
    """
    Bounded, in-memory command history for one connected session (design
    doc round 44/Track 5f) — Up/Down recall in `read_line`.

    Deliberately not tied to any one channel: constructed once per
    connection (alongside `hub`/`presence`/`mailbox` — see
    `netbbs.net.login_flow.handle_session`) and threaded down to every
    `read_line()` call that wants recall, so history persists across a
    `/join` channel switch within the same session rather than resetting.
    In-memory only, no persistence, same ephemeral posture as chat itself
    — nothing here needs to survive a disconnect.

    Bounded size (`max_entries`), matching this project's consistent
    bounded-not-unbounded philosophy elsewhere (chat scrollback's 100-
    event cap, the picker's 99-item page cap).
    """

    max_entries: int = 50
    _entries: list[str] = field(default_factory=list)

    def record(self, line: str) -> None:
        """Appends `line` if non-blank — an empty Enter press isn't
        worth recalling later."""
        if not line:
            return
        self._entries.append(line)
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, index_from_most_recent: int) -> str:
        """`entry(1)` is the most recently recorded line, `entry(2)` the
        one before that, and so on — matches how `read_line`'s recall
        state (`history_index`) naturally counts "how many Ups back from
        the in-progress line.\""""
        return self._entries[-index_from_most_recent]


@dataclass
class LiveInputBuffer:
    """
    A live, externally-observable snapshot of an in-progress `read_line`
    edit — the text typed so far and the cursor position within it,
    refreshed once per keystroke, right before the next blocking byte
    read (design doc round 79). Exists specifically so a concurrently
    running task (`netbbs.net.chat_flow`'s `receive_loop`) can redraw a
    pinned input row from real state after printing new content above
    it, instead of guessing or leaving stale/corrupted text on screen —
    `read_line`'s `line`/`cursor` state is otherwise entirely private to
    its own call frame (see `_read_line_editable`'s own docstring for
    why this couldn't be solved any other way without exposing it).

    A plain dataclass, not independently synchronized on its own — only
    ever *written* by whichever task currently owns the read (chat's
    `send_loop`, via `read_line`'s own internals) and *read* from
    another task purely as a snapshot. Safe without a lock of its own
    because CPython/asyncio's cooperative scheduling means an ordinary
    attribute write is never torn by a concurrent reader; the actual
    *terminal writes* representing this state are a separate concern,
    guarded instead by the `lock` parameter `read_line` also accepts.
    """

    text: str = ""
    cursor: int = 0

    def update(self, line: list[str], cursor: int) -> None:
        self.text = "".join(line)
        self.cursor = cursor


# -- tab completion (transport/byte agnostic, design doc round 49/Track 5g) -

# `Completer` is deliberately generic: given the text of the current
# line up to the cursor, return every full-word replacement candidate
# for whatever's being typed. This module has no idea what a "command"
# or a "username" is — that domain knowledge lives entirely in the
# closure a caller supplies (see `netbbs.net.chat_flow`'s per-read_line
# completer, or `netbbs.net.picker.pick_item`'s name-based one); this
# module only knows the generic notion of "word" (split on a literal
# space) needed to know how much of the buffer to replace.
Completer = Callable[[str], Sequence[str]]


def _current_word_start(line: list[str], cursor: int) -> int:
    """Start index of the whitespace-delimited token ending at
    `cursor` — the region Tab completion replaces."""
    i = cursor
    while i > 0 and line[i - 1] != " ":
        i -= 1
    return i


def _common_prefix(candidates: Sequence[str]) -> str:
    if not candidates:
        return ""
    prefix = candidates[0]
    for candidate in candidates[1:]:
        while not candidate.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


async def apply_tab_completion(
    write: WriteFunc, completer: Completer, line: list[str], cursor: int
) -> int:
    """
    Handle one Tab keypress in an editable line buffer: ask `completer`
    for candidates completing the word ending at `cursor`, apply the
    result to `line` in place, and return the new cursor position.

    Zero candidates: does nothing (not even a bell — an empty Tab press
    while composing free text is common and not itself an error, unlike
    an actually-invalid menu keystroke elsewhere in this codebase). One
    candidate: replaces the current word with it plus a trailing space,
    ready to type the next word. Multiple candidates: extends the word
    to their longest shared prefix (bash-style), if that's longer than
    what's already typed, then lists every candidate on its own line
    below and reprints the in-progress line.

    Deliberately reprints only the raw line content after showing a
    candidate list, not any caller-side prompt label ("Choice: ",
    "Search: ") — this module has no idea such a label exists, the same
    way it has no idea what a command or username is. Callers with a
    static prompt (`pick_item`'s `"Search: "`) accept that the label
    itself doesn't reappear alongside a multi-candidate list; callers
    without one (chat's `send_loop`, which has no prompt string at all)
    are unaffected.
    """
    text_before_cursor = "".join(line[:cursor])
    candidates = list(completer(text_before_cursor))
    if not candidates:
        return cursor

    word_start = _current_word_start(line, cursor)

    if len(candidates) == 1:
        replacement = list(candidates[0]) + [" "]
        terminal_col = cursor
        line[word_start:cursor] = replacement
        new_cursor = word_start + len(replacement)
        await redraw_tail(
            write, terminal_col=terminal_col, edit_pos=word_start, line=line, new_cursor=new_cursor
        )
        return new_cursor

    prefix = list(_common_prefix(candidates))
    if prefix != line[word_start:cursor]:
        terminal_col = cursor
        line[word_start:cursor] = prefix
        cursor = word_start + len(prefix)
        await redraw_tail(
            write, terminal_col=terminal_col, edit_pos=word_start, line=line, new_cursor=cursor
        )

    await write("\r\n" + "  ".join(candidates) + "\r\n")
    await write("".join(line))
    await write(move_cursor(len(line) - cursor, forward=False))
    return cursor


def _push_back(source: ByteSource, byte: int) -> None:
    pending = getattr(source, _PUSHBACK_ATTR, None)
    if pending is None:
        pending = []
        setattr(source, _PUSHBACK_ATTR, pending)
    pending.append(byte)


def _pop_pushed_back(source: ByteSource) -> int | None:
    pending = getattr(source, _PUSHBACK_ATTR, None)
    if not pending:
        return None
    return pending.pop()


async def _read_byte(source: ByteSource) -> int | None:
    pushed = _pop_pushed_back(source)
    if pushed is not None:
        return pushed
    return await source.read_byte()


async def _read_byte_with_timeout(source: ByteSource, timeout: float) -> int | None:
    pushed = _pop_pushed_back(source)
    if pushed is not None:
        return pushed
    return await source.read_byte_with_timeout(timeout)


async def read_line(
    source: ByteSource,
    write: WriteFunc,
    echo: bool = True,
    history: InputHistory | None = None,
    completer: Completer | None = None,
    *,
    live_buffer: LiveInputBuffer | None = None,
    lock: asyncio.Lock | None = None,
) -> str:
    """
    Read one line of input, echoing (or masking, if `echo=False`) as it
    arrives, with cursor-addressable editing (design doc round 44/
    Track 5f): Left/Right move within the line, Home/End jump to its
    start/end, Backspace/Delete remove from either side of the cursor,
    Insert toggles overwrite mode, and Up/Down cycle through `history`
    if one is supplied. Tab triggers completion via `completer` (design
    doc round 49/Track 5g), if one is supplied — see
    `apply_tab_completion`'s docstring for its exact behavior.

    `echo=False` (password prompts) deliberately keeps the original,
    simpler append/Backspace-from-the-end-only behavior with no cursor
    movement, history, or completion — a masked field doesn't
    meaningfully benefit from any of that, and it avoids needing a
    parallel masked-display buffer just to support cases nothing asks
    for.

    `live_buffer`/`lock` (design doc round 79) are the pinned-chat-
    input-row hooks `netbbs.net.chat_flow` needs and nothing else does
    — both default to `None`, a complete no-op for every other call
    site in the codebase. `live_buffer`, if given, is kept up to date
    with the in-progress `line`/`cursor` state after every keystroke's
    own edit; `lock`, if given, is held for the duration of handling
    each keystroke's own writes, so a concurrently-running task holding
    the same lock (to redraw a pinned row elsewhere on screen) can
    never interleave with an in-progress echo/edit and corrupt it.
    Silently ignored for `echo=False` masked reads — a password prompt
    has no legitimate reason to be visible to a concurrently-redrawing
    pinned row.
    """
    if not echo:
        return await _read_line_masked(source, write)
    return await _read_line_editable(source, write, history, completer, live_buffer=live_buffer, lock=lock)


async def _read_line_masked(source: ByteSource, write: WriteFunc) -> str:
    """The original simple behavior, preserved as-is for masked
    (password) reads — see `read_line`'s docstring for why."""
    line: list[str] = []
    while True:
        b = await _read_byte(source)
        if b is None:
            continue

        if b in (_CR, _LF):
            if b == _CR:
                await _consume_optional_lf_or_nul(source)
            break

        if b in (_BS, _DEL):
            if line:
                line.pop()
                await write("\b \b")
            continue

        if b == _ESC:
            await _read_escape_sequence(source)
            continue

        if b < 0x20:
            continue

        if b < 0x80:
            char = chr(b)
        else:
            char = await _read_utf8_continuation(source, b)
            if char is None:
                continue

        if len(line) < _MAX_LINE_LENGTH:
            line.append(char)
            await write("*")

    await write("\r\n")
    return "".join(line)


async def _read_line_editable(
    source: ByteSource,
    write: WriteFunc,
    history: InputHistory | None,
    completer: Completer | None = None,
    *,
    live_buffer: LiveInputBuffer | None = None,
    lock: asyncio.Lock | None = None,
) -> str:
    line: list[str] = []
    cursor = 0
    overwrite = False
    history_index = 0  # 0 == "not recalling", editing the in-progress line
    saved_in_progress: list[str] | None = None

    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        # The whole per-keystroke reaction (every write() call one byte
        # can trigger) is one atomic critical section under `lock`, if
        # given — design doc round 79. `live_buffer` is refreshed in the
        # `finally` so it happens exactly once per keystroke regardless
        # of which branch below was taken (several `continue`/`break`
        # out of here, all of which still need the buffer updated
        # before this iteration ends), and *while still holding the
        # lock* — the buffer's own state and the writes that produced it
        # must never be observed out of sync with each other by a
        # concurrent redraw.
        async with (lock if lock is not None else contextlib.nullcontext()):
            try:
                if b in (_CR, _LF):
                    if b == _CR:
                        await _consume_optional_lf_or_nul(source)
                    break

                if b in (_BS, _DEL):
                    if cursor > 0:
                        terminal_col = cursor
                        del line[cursor - 1]
                        cursor -= 1
                        await redraw_tail(
                            write, terminal_col=terminal_col, edit_pos=cursor, line=line, new_cursor=cursor
                        )
                    continue

                if b == _TAB:
                    if completer is not None:
                        cursor = await apply_tab_completion(write, completer, line, cursor)
                    continue

                if b == _ESC:
                    key = await _read_escape_sequence(source)
                    if key == "LEFT":
                        if cursor > 0:
                            cursor -= 1
                            await write(move_cursor(1, forward=False))
                    elif key == "RIGHT":
                        if cursor < len(line):
                            cursor += 1
                            await write(move_cursor(1, forward=True))
                    elif key == "HOME":
                        if cursor > 0:
                            await write(move_cursor(cursor, forward=False))
                            cursor = 0
                    elif key == "END":
                        if cursor < len(line):
                            await write(move_cursor(len(line) - cursor, forward=True))
                            cursor = len(line)
                    elif key == "DELETE":
                        if cursor < len(line):
                            terminal_col = cursor
                            del line[cursor]
                            await redraw_tail(
                                write, terminal_col=terminal_col, edit_pos=cursor, line=line, new_cursor=cursor
                            )
                    elif key == "INSERT":
                        overwrite = not overwrite
                    elif key in ("UP", "DOWN") and history is not None:
                        recalled = None
                        if key == "UP" and history_index < len(history):
                            if history_index == 0:
                                saved_in_progress = list(line)
                            history_index += 1
                            recalled = list(history.entry(history_index))
                        elif key == "DOWN" and history_index > 0:
                            history_index -= 1
                            recalled = list(saved_in_progress) if history_index == 0 else list(
                                history.entry(history_index)
                            )
                        if recalled is not None:
                            terminal_col = cursor
                            line = recalled
                            cursor = len(line)
                            await redraw_tail(
                                write, terminal_col=terminal_col, edit_pos=0, line=line, new_cursor=cursor
                            )
                    continue

                if b < 0x20:
                    # Any other control byte (Tab, Ctrl+C, Ctrl+D, etc.) —
                    # not supported in this pass; discard rather than
                    # corrupt the line or echo something meaningless.
                    continue

                if b < 0x80:
                    char = chr(b)
                else:
                    char = await _read_utf8_continuation(source, b)
                    if char is None:
                        continue  # malformed/interrupted multi-byte sequence

                if overwrite and cursor < len(line):
                    line[cursor] = char
                    cursor += 1
                    await write(char)
                    continue

                if len(line) >= _MAX_LINE_LENGTH:
                    # silently drop the character but keep reading —
                    # Backspace, movement, and Enter still work normally
                    # past the cap.
                    continue

                terminal_col = cursor
                line.insert(cursor, char)
                cursor += 1
                if cursor == len(line):
                    # Appending at the end -- the common case while
                    # typing normally -- needs only the one character
                    # written, not a full (empty) tail reprint.
                    await write(char)
                else:
                    await redraw_tail(
                        write, terminal_col=terminal_col, edit_pos=terminal_col, line=line, new_cursor=cursor
                    )
            finally:
                if live_buffer is not None:
                    live_buffer.update(line, cursor)

    if live_buffer is not None:
        # The line was just submitted (Enter) -- a fresh, empty buffer
        # is what the pinned input row should show from here, not
        # whatever was last typed (the caller, netbbs.net.chat_flow's
        # send_loop, redraws from this immediately after read_line
        # returns).
        live_buffer.update([], 0)
    await write("\r\n")
    result = "".join(line)
    if history is not None:
        history.record(result)
    return result


async def read_key(source: ByteSource, write: WriteFunc, echo: bool = True) -> str:
    """
    Read a single character and return immediately — the character-mode
    equivalent of a classic BBS hotkey menu: intended for genuine
    single-choice menu selections, not free-text input, which should keep
    using `read_line`.

    Control bytes with no meaning as a standalone "key" — Backspace/
    Delete, CR/LF, escape sequences (recognized or not; there's no line
    here for Left/Right/Home/End/Delete to act within, and Up/Down have
    no history to recall in a single-keystroke menu) — are silently
    skipped and reading continues, rather than being returned as a key
    in their own right.
    """
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        if b in (_CR, _LF, _BS, _DEL):
            continue

        if b == _ESC:
            await _read_escape_sequence(source)
            continue

        if b < 0x20:
            continue

        if b < 0x80:
            char = chr(b)
        else:
            char = await _read_utf8_continuation(source, b)
            if char is None:
                continue

        await write(char if echo else "*")
        return char


class EditorKeyKind(Enum):
    """Design doc -- welcome banner round B1: the structured key-event
    vocabulary a full-screen editor (`netbbs.net.ansi_editor`) needs,
    which neither `read_line` (line-oriented, returns a finished `str`)
    nor `read_key` (discards every escape sequence outright, since a
    single-keystroke menu has no line for a cursor to move within) can
    provide."""

    CHAR = auto()
    ENTER = auto()
    BACKSPACE = auto()
    DELETE = auto()
    TAB = auto()
    ESCAPE = auto()
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    HOME = auto()
    END = auto()
    PAGE_UP = auto()
    PAGE_DOWN = auto()
    CTRL = auto()


@dataclass(frozen=True)
class EditorKey:
    kind: EditorKeyKind
    char: str | None = None  # the literal character for CHAR/CTRL, else None


_SYMBOLIC_TO_EDITOR_KIND: dict[str, EditorKeyKind] = {
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "LEFT": EditorKeyKind.LEFT,
    "RIGHT": EditorKeyKind.RIGHT,
    "HOME": EditorKeyKind.HOME,
    "END": EditorKeyKind.END,
    "DELETE": EditorKeyKind.DELETE,
    "PAGE_UP": EditorKeyKind.PAGE_UP,
    "PAGE_DOWN": EditorKeyKind.PAGE_DOWN,
    # INSERT has no meaning for the ANSI editor in this round's scope
    # (typing always overwrites the cell at the cursor already, no
    # separate overwrite-mode toggle) -- a real INSERT keypress simply
    # isn't surfaced as anything by read_editor_key below.
}


async def read_editor_key(source: ByteSource) -> EditorKey:
    """
    Read one structured key event for a full-screen editor.

    Unlike `read_key` (which discards every escape sequence outright)
    or `read_line` (line-oriented, returns a finished `str` only on
    Enter), this surfaces arrows, Home/End, Page Up/Down, and a real
    standalone Escape press as first-class events, alongside ordinary
    characters, Enter, Backspace, Delete, Tab, and Ctrl+letter combos
    (returned as `EditorKeyKind.CTRL` with the lowercase letter, e.g.
    Ctrl+S -> `char="s"`) -- everything a screen editor needs that
    `read_line`'s line-oriented model has no use for.
    """
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        if b in (_CR, _LF):
            if b == _CR:
                await _consume_optional_lf_or_nul(source)
            return EditorKey(EditorKeyKind.ENTER)

        if b in (_BS, _DEL):
            return EditorKey(EditorKeyKind.BACKSPACE)

        if b == _TAB:
            return EditorKey(EditorKeyKind.TAB)

        if b == _ESC:
            # _read_escape_sequence's `None` is ambiguous on its own --
            # it means both "nothing followed ESC" (a real standalone
            # Escape press) and "something followed but wasn't in our
            # recognized table" (discard, not an Escape press at all).
            # read_line/read_key never needed to tell these apart (both
            # cases are already "not a match, keep going" for them) but
            # a real standalone Escape is a first-class, meaningful key
            # here (typically "exit the editor"), so it's peeked
            # explicitly first, using the same pushback mechanism
            # _consume_optional_lf_or_nul relies on for an analogous
            # lookahead-then-replay need.
            peek = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
            if peek is None:
                return EditorKey(EditorKeyKind.ESCAPE)
            _push_back(source, peek)
            key = await _read_escape_sequence(source)
            if key is not None:
                kind = _SYMBOLIC_TO_EDITOR_KIND.get(key)
                if kind is not None:
                    return EditorKey(kind)
            continue  # an unrecognized/unsupported escape shape -- keep reading

        if b < 0x20:
            return EditorKey(EditorKeyKind.CTRL, char=chr(b + 0x60))  # Ctrl+A -> 'a', etc.

        if b < 0x80:
            char = chr(b)
        else:
            char = await _read_utf8_continuation(source, b)
            if char is None:
                continue  # malformed/interrupted multi-byte sequence

        return EditorKey(EditorKeyKind.CHAR, char=char)


async def _read_utf8_continuation(source: ByteSource, lead_byte: int) -> str | None:
    """
    Given a UTF-8 multi-byte lead byte already read, read the appropriate
    number of continuation bytes (per the UTF-8 encoding scheme's
    lead-byte ranges) and decode the complete character.

    Matters concretely for this project: umlauts and other non-ASCII
    characters are everyday input, not an edge case, and a naive
    byte-at-a-time decode would corrupt every one of them. Returns `None`
    (discarding the partial character) if the sequence is malformed or
    interrupted by a transport-level action rather than risking a wrong
    decode.
    """
    if 0xC2 <= lead_byte <= 0xDF:
        extra = 1
    elif 0xE0 <= lead_byte <= 0xEF:
        extra = 2
    elif 0xF0 <= lead_byte <= 0xF4:
        extra = 3
    else:
        return None  # not a valid UTF-8 lead byte

    raw = bytearray([lead_byte])
    for _ in range(extra):
        cb = await _read_byte(source)
        if cb is None or not (0x80 <= cb <= 0xBF):
            return None
        raw.append(cb)

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def _consume_optional_lf_or_nul(source: ByteSource) -> None:
    """
    After a CR, consume a following LF or NUL if present — both are valid
    line-ending continuations (CRLF or CR-NUL).

    Bounded by a short timeout rather than an unbounded read: a client in
    true character mode may send a lone CR with nothing immediately
    following it, and blocking indefinitely for a byte that isn't coming
    would hang the whole session. If the lookahead is ordinary input, it
    is saved for the next logical read instead of being discarded.
    """
    peek = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    if peek is not None and peek not in (_LF, _NUL):
        _push_back(source, peek)


def _decode_csi(params: bytes, final_byte: int) -> str | None:
    if not params:
        return _CSI_FINAL_TO_KEY.get(final_byte)
    if final_byte == 0x7E:
        return _CSI_TILDE_TO_KEY.get(params)
    return None


async def _read_escape_sequence(source: ByteSource) -> str | None:
    """
    Consume a terminal escape sequence following an ESC byte as a
    complete unit and return a symbolic key name for the small set this
    project recognizes — `"UP"`/`"DOWN"`/`"LEFT"`/`"RIGHT"`/`"HOME"`/
    `"END"`/`"DELETE"`/`"INSERT"` — or `None` for a real Escape keypress
    with nothing following, or any shape not in that set (still
    discarded as a complete unit either way — "recognize a few, discard
    the rest" replaces round 13/14's original "discard everything"
    scope, it doesn't loosen the discarding itself). Handles the two
    common shapes real terminals use for special keys:

    - CSI sequences: ESC [ ... <final byte in 0x40-0x7E>
    - SS3 sequences: ESC O <single letter>

    Bounded by both a maximum CSI parameter length and one total deadline
    covering the whole CSI parameter loop — see the module-level
    constants above. Tracked via an explicit `time.monotonic()` deadline
    checked once per loop iteration, deliberately *not* an
    `asyncio.wait_for(...)` wrapped around the whole function: that was
    the first approach tried when this function was still
    `_discard_escape_sequence` (round 13/14), and direct testing against
    a real socket (not just an in-memory fake source) surfaced a genuine
    race — this function's own per-byte `_read_byte_with_timeout` calls
    are already each individually wrapped in their own `wait_for` by the
    underlying transport, and an *outer* `wait_for` cancelling an *inner*
    one at nearly the same moment the inner one would have timed out
    anyway is timing-sensitive in a way that isn't reliably reproducible.
    An explicit deadline check has no such ambiguity. Either limit being
    exceeded raises `SessionClosedError`, closing the session the same
    way an oversized/stalled Telnet subnegotiation does: a client that
    won't stop sending what claims to be a single escape sequence is a
    protocol-level violation serious enough to end the connection, not
    something to just silently keep discarding forever.
    """
    next_byte = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    if next_byte is None:
        return None

    if next_byte == 0x5B:  # '[' — CSI sequence
        deadline = time.monotonic() + _ESCAPE_SEQUENCE_TIMEOUT
        consumed = 0
        params = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise SessionClosedError("terminal escape sequence timed out")
            b = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
            if b is None:
                return None
            consumed += 1
            if consumed > _MAX_ESCAPE_SEQUENCE_LENGTH:
                raise SessionClosedError("terminal escape sequence is too long")
            if 0x40 <= b <= 0x7E:
                return _decode_csi(bytes(params), b)  # final byte of the CSI sequence
            params.append(b)
    elif next_byte == 0x4F:  # 'O' — SS3 sequence, always exactly one more byte
        letter = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
        if letter is None or not (0x20 <= letter < 0x7F):
            return None
        return _SS3_TO_KEY.get(chr(letter))
    # else: some other/unrecognized shape — just the ESC itself was
    # consumed; nothing more to do.
    return None
