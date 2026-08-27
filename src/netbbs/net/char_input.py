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
(design doc §15 Phase 2):
`move_cursor`/`redraw_tail`, `InputHistory`, and `apply_tab_completion`
below are written with no dependency on bytes or `ByteSource` at all
(pure `list[str]`/cursor-integer/`WriteFunc` manipulation) specifically
so `netbbs.net.web.WebSession` — which decodes a browser's `onData`
events into whole characters itself and deliberately does not share
this module's byte-oriented reading — can still reuse this
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
from netbbs.rendering.ansi import reject_keystroke
from netbbs.rendering.width import char_width, display_width

# Control byte values relevant to character-mode line building.
_CR = 0x0D
_LF = 0x0A
_NUL = 0x00
_BS = 0x08  # Backspace
_DEL = 0x7F  # Delete — many terminals send this for the Backspace key
_ESC = 0x1B
_TAB = 0x09

# Issue #102: the control bytes `read_key()` recognizes and returns as
# their own distinct keys, rather than the generic "no meaning as a
# standalone key, skip it and keep reading" treatment every other
# control byte still gets below. Exported (not `_`-prefixed) since a
# menu loop comparing `key == REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY`/
# `CANCEL_KEY` needs these same values — a raw `"\x0c"`/`"\x12"`/
# `"\x08"`/`"\x03"` literal at every call site would be both unreadable
# and easy to typo.
#
# Deliberately narrow: only these bytes get this treatment, not a
# blanket "pass every control byte through" -- CR/LF/DEL/ESC above keep
# their own existing special handling, and every other control byte
# remains silently swallowed exactly as before, matching this
# function's own documented "not a standalone key" contract.
REDRAW_KEY = "\x0c"  # Ctrl-L
REFRESH_KEY = "\x12"  # Ctrl-R
# Issue #150: Ctrl-H, same byte (0x08) as Backspace. Safe specifically
# because read_key() already treats Backspace as "no meaning, discard"
# in a single-keystroke menu (there's no in-progress typed text for it
# to delete) -- reusing that already-inert byte for a real, useful
# action here doesn't take anything away from a client whose Backspace
# key happens to send it. Unlike REDRAW_KEY/REFRESH_KEY, read_line()'s
# own editable path must keep treating 0x08 as real backspace-editing
# (see _read_line_editable's unchanged _BS handling) -- this carve-out
# is read_key()-only, the same way REDRAW_KEY/REFRESH_KEY already are.
HELP_KEY = "\x08"  # Ctrl-H
# Issue #157: Ctrl-C, adopted incrementally -- confirmed with Thiesi as
# the same "return a distinct sentinel, let call sites opt in" shape
# REDRAW_KEY/REFRESH_KEY/HELP_KEY already use, not a single sweeping
# change. read_key()-only for now, same reasoning as HELP_KEY: a
# single-keystroke menu has no in-progress typed text for Ctrl-C to
# interrupt, so returning it here takes nothing away. Deliberately
# does *not* touch read_line()'s editable path in this pass -- unlike
# Backspace's byte, Ctrl-C during real free-text entry has no single
# safe universal meaning across every caller (e.g. composition.py's
# line editor already gives a bare blank line an entirely different
# meaning -- "finish and review," not "cancel" -- so silently
# reinterpreting Ctrl-C as "submit blank" there would be actively
# wrong for that caller). Extending real-text-entry cancellation is
# left for a later, separately-scoped increment.
CANCEL_KEY = "\x03"  # Ctrl-C


def reject_unhandled_key(key: str, *, count: int = 1) -> str:
    """
    Like `netbbs.rendering.ansi.reject_keystroke`, but aware that
    `REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY`/`CANCEL_KEY` are returned
    *unechoed* by `read_key()` above (a real dogfood-reported bug this
    fixes): `reject_keystroke()` unconditionally erases "the last
    echoed character" before ringing the bell, an assumption that's
    wrong for these -- since nothing was echoed for this particular
    keystroke, that erase instead deletes whatever real character was
    last drawn on screen, once per press. A menu loop that doesn't
    specifically support one of these just bells for it instead; every
    other unrecognized key keeps today's erase-and-bell behavior
    unchanged.
    """
    if key in (REDRAW_KEY, REFRESH_KEY, HELP_KEY, CANCEL_KEY):
        return "\a"
    return reject_keystroke(count)


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
# (design doc -- welcome banner, for netbbs.net.ansi_editor),
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
    write: WriteFunc, *, move_back: int, edit_pos: int, line: list[str], new_cursor: int
) -> None:
    """
    The one redraw primitive every mid-line edit (insert, Backspace,
    Delete, full-line history recall) goes through: move the terminal
    cursor back `move_back` display columns to `edit_pos`, erase to the
    end of the visible line (`ESC[K`), reprint `line[edit_pos:]` —
    whatever the edit left there — then reposition to `new_cursor`.

    `move_back` (design doc, dogfood feature request: international
    users found non-ASCII handling poor) is display columns, supplied
    by the caller rather than derived here from `edit_pos` alone: by
    the time this runs, `line` already reflects the edit (e.g.
    Backspace's deleted character is already gone from it), so the
    width of whatever changed has to be captured by the caller before
    mutating `line`, against `netbbs.rendering.width.display_width` —
    an East Asian Wide character moves the real terminal cursor 2
    columns, not 1. Every real call site only ever needs to move
    backward or not at all (the terminal cursor is always at or right
    of `edit_pos` when a redraw is triggered), so there's no forward
    case to support.

    Reprinting only the tail (not the whole line) keeps this cheap for
    the common case of editing near the end of a short line, and erasing
    via `ESC[K` rather than manually overwriting with spaces means the
    terminal — not this code — is responsible for knowing how much
    trailing whitespace to clear, which is simpler and can't drift out
    of sync with the actual old line length.
    """
    if move_back > 0:
        await write(move_cursor(move_back, forward=False))
    await write("\x1b[K")
    await write("".join(line[edit_pos:]))
    await write(move_cursor(display_width("".join(line[new_cursor:])), forward=False))


@dataclass
class InputHistory:
    """
    Bounded, in-memory command history for one connected session (design
    doc) — Up/Down recall in `read_line`.

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
    read (design doc). Exists specifically so a concurrently
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


# -- tab completion (transport/byte agnostic, design doc) -

# `Completer` is deliberately generic: given the text of the current
# line up to the cursor, return every full-word replacement candidate
# for whatever's being typed. This module has no idea what a "command"
# or a "username" is — that domain knowledge lives entirely in the
# closure a caller supplies (see `netbbs.net.chat_flow`'s per-read_line
# completer, or `netbbs.net.picker.pick_item`'s name-based one); this
# module only knows the generic notion of "word" (split on a literal
# space) needed to know how much of the buffer to replace.
Completer = Callable[[str], Sequence[str]]

# Hook for the multi-candidate branch of `apply_tab_completion` (see that
# function's docstring for why this exists): given the candidate list, the
# full current line text, and the cursor position, the hook is responsible
# for displaying the candidates and leaving the terminal ready for further
# editing. `None` (the default, and every caller except `netbbs.net.
# chat_flow`) keeps the original plain-scrolling behavior of writing the
# list with raw newlines.
CandidateListPrinter = Callable[[Sequence[str], str, int], Awaitable[None]]


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


@dataclass
class LastCandidateList:
    """Tracks whether the *immediately preceding* keystroke was a Tab
    press that printed a multi-candidate list, within one `read_line()`
    call — lets a repeated Tab press with nothing typed or edited in
    between suppress a redundant identical reprint, rather than
    permanently scrolling another copy of the same list into the
    caller's output every single time it's pressed.

    The caller (`_read_line_editable`, in this module and mirrored in
    `netbbs.net.web.WebSession`) is responsible for clearing `shown`
    back to `False` at the top of every keystroke that *isn't* Tab,
    before dispatching to that keystroke's own handling — an ordinary
    typed character, Backspace, an arrow key, anything. That's what
    makes this "the previous keystroke, not just the previous Tab
    press": pressing Tab, then some other key, then Tab again is a
    genuine new completion attempt and must print again, even in the
    edge case where it happens to land back on an identical word
    (Thiesi's own example: backspacing a fully-typed name back to
    nothing, then pressing Tab again — the completion engine's own
    common-prefix auto-extension can reconstruct the exact same
    characters, so comparing the resulting *word* alone can't tell
    this apart from a true no-op repeat; only "did some other keystroke
    happen in between" can).

    A plain mutable holder, not a return value threaded back through
    every caller — `_read_line_editable` creates one per `read_line()`
    call and passes it in by reference, the same lifecycle
    `LiveInputBuffer` already uses for its own per-call state.
    """

    shown: bool = False


async def apply_tab_completion(
    write: WriteFunc, completer: Completer, line: list[str], cursor: int,
    *, list_candidates: CandidateListPrinter | None = None,
    last_candidates: LastCandidateList | None = None,
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
    what's already typed, then lists every candidate — unless
    `last_candidates` shows the immediately preceding keystroke already
    printed a list with nothing typed or edited since, in which case
    this press is also a complete no-op (`LastCandidateList`'s own
    docstring).

    Deliberately reprints only the raw line content after showing a
    candidate list, not any caller-side prompt label ("Choice: ",
    "Search: ") — this module has no idea such a label exists, the same
    way it has no idea what a command or username is. Callers with a
    static prompt (`pick_item`'s `"Search: "`) accept that the label
    itself doesn't reappear alongside a multi-candidate list; callers
    without one (chat's `send_loop`, which has no prompt string at all)
    are unaffected.

    `list_candidates`, if given, takes over displaying the multi-
    candidate list and redrawing the line afterward, instead of this
    function writing a bare `"\\r\\n" + "  ".join(candidates) + "\\r\\n"`
    itself. Needed by callers with pinned/reserved screen rows (chat's
    status and input rows, design doc's scroll region): an
    unconditional `"\\r\\n"` from here has no idea that the terminal's
    cursor sits on a row outside the scrolling content region, and lands
    the candidate list on — and overwrites — whatever's pinned there
    instead of scrolling normally above it. `None` (every caller besides
    `netbbs.net.chat_flow`) keeps the original behavior exactly.
    """
    text_before_cursor = "".join(line[:cursor])
    candidates = list(completer(text_before_cursor))
    if not candidates:
        return cursor

    word_start = _current_word_start(line, cursor)

    if len(candidates) == 1:
        move_back = display_width("".join(line[word_start:cursor]))
        replacement = list(candidates[0]) + [" "]
        line[word_start:cursor] = replacement
        new_cursor = word_start + len(replacement)
        await redraw_tail(
            write, move_back=move_back, edit_pos=word_start, line=line, new_cursor=new_cursor
        )
        return new_cursor

    prefix = list(_common_prefix(candidates))
    if prefix != line[word_start:cursor]:
        move_back = display_width("".join(line[word_start:cursor]))
        line[word_start:cursor] = prefix
        cursor = word_start + len(prefix)
        await redraw_tail(
            write, move_back=move_back, edit_pos=word_start, line=line, new_cursor=cursor
        )

    if last_candidates is not None:
        if last_candidates.shown:
            return cursor
        last_candidates.shown = True

    if list_candidates is not None:
        await list_candidates(candidates, "".join(line), cursor)
    else:
        await write("\r\n" + "  ".join(candidates) + "\r\n")
        await write("".join(line))
        await write(move_cursor(display_width("".join(line[cursor:])), forward=False))
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
    list_candidates: CandidateListPrinter | None = None,
) -> str:
    """
    Read one line of input, echoing (or masking, if `echo=False`) as it
    arrives, with cursor-addressable editing (design doc):
    Left/Right move within the line, Home/End jump to its
    start/end, Backspace/Delete remove from either side of the cursor,
    Insert toggles overwrite mode, and Up/Down cycle through `history`
    if one is supplied. Tab triggers completion via `completer` (design
    doc), if one is supplied — see
    `apply_tab_completion`'s docstring for its exact behavior.

    `echo=False` (password prompts) deliberately keeps the original,
    simpler append/Backspace-from-the-end-only behavior with no cursor
    movement, history, or completion — a masked field doesn't
    meaningfully benefit from any of that, and it avoids needing a
    parallel masked-display buffer just to support cases nothing asks
    for.

    `live_buffer`/`lock` (design doc) are the pinned-chat-
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

    `list_candidates`, also chat-only and also `None` everywhere else,
    passes straight through to `apply_tab_completion` — see that
    function's docstring.
    """
    if not echo:
        return await _read_line_masked(source, write)
    return await _read_line_editable(
        source, write, history, completer, live_buffer=live_buffer, lock=lock, list_candidates=list_candidates
    )


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
    list_candidates: CandidateListPrinter | None = None,
) -> str:
    line: list[str] = []
    cursor = 0
    overwrite = False
    history_index = 0  # 0 == "not recalling", editing the in-progress line
    saved_in_progress: list[str] | None = None
    submitted = ""  # set from `line` the moment Enter is handled, below
    last_candidates = LastCandidateList()
    line_limit_warned = False  # bell once per read_line() call, not once per dropped character

    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        # The whole per-keystroke reaction (every write() call one byte
        # can trigger) is one atomic critical section under `lock`, if
        # given — design doc. `live_buffer` is refreshed in the
        # `finally` so it happens exactly once per keystroke regardless
        # of which branch below was taken (several `continue`/`break`
        # out of here, all of which still need the buffer updated
        # before this iteration ends), and *while still holding the
        # lock* — the buffer's own state and the writes that produced it
        # must never be observed out of sync with each other by a
        # concurrent redraw.
        async with (lock if lock is not None else contextlib.nullcontext()):
            try:
                # Any keystroke other than Tab itself invalidates a
                # pending "the last thing that happened was an unresolved
                # multi-candidate Tab press" -- see `LastCandidateList`'s
                # own docstring for why this has to be tracked as "did a
                # different keystroke happen", not by comparing the
                # completed word before/after, which a Tab press's own
                # common-prefix auto-extension can make look unchanged
                # even after a real edit.
                if b != _TAB:
                    last_candidates.shown = False

                if b in (_CR, _LF):
                    if b == _CR:
                        await _consume_optional_lf_or_nul(source)
                    # GitHub issue #45: the submitted-line capture,
                    # live_buffer reset, and final CRLF write must all
                    # happen inside this same per-keystroke critical
                    # section, not after the lock has already been
                    # released below -- otherwise a concurrently
                    # redrawing task (netbbs.net.chat_flow's
                    # receive_loop) can acquire the lock in the gap and
                    # redraw a pinned input row from state that doesn't
                    # yet match the terminal (or race the final "\r\n"
                    # write itself). Clearing `line`/`cursor` here means
                    # the `finally` below's existing live_buffer.update
                    # call does the reset as part of the same mechanism
                    # every other keystroke already uses, rather than a
                    # second, separately-timed update after the loop.
                    submitted = "".join(line)
                    line = []
                    cursor = 0
                    await write("\r\n")
                    break

                if b in (_BS, _DEL):
                    if cursor > 0:
                        move_back = char_width(line[cursor - 1])
                        del line[cursor - 1]
                        cursor -= 1
                        await redraw_tail(
                            write, move_back=move_back, edit_pos=cursor, line=line, new_cursor=cursor
                        )
                    continue

                if b == _TAB:
                    if completer is not None:
                        cursor = await apply_tab_completion(
                            write, completer, line, cursor,
                            list_candidates=list_candidates, last_candidates=last_candidates,
                        )
                    continue

                if b == _ESC:
                    key = await _read_escape_sequence(source)
                    if key == "LEFT":
                        if cursor > 0:
                            cursor -= 1
                            await write(move_cursor(char_width(line[cursor]), forward=False))
                    elif key == "RIGHT":
                        if cursor < len(line):
                            width = char_width(line[cursor])
                            cursor += 1
                            await write(move_cursor(width, forward=True))
                    elif key == "HOME":
                        if cursor > 0:
                            await write(move_cursor(display_width("".join(line[:cursor])), forward=False))
                            cursor = 0
                    elif key == "END":
                        if cursor < len(line):
                            await write(move_cursor(display_width("".join(line[cursor:])), forward=True))
                            cursor = len(line)
                    elif key == "DELETE":
                        if cursor < len(line):
                            del line[cursor]
                            await redraw_tail(
                                write, move_back=0, edit_pos=cursor, line=line, new_cursor=cursor
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
                            move_back = display_width("".join(line[:cursor]))
                            line = recalled
                            cursor = len(line)
                            await redraw_tail(
                                write, move_back=move_back, edit_pos=0, line=line, new_cursor=cursor
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
                    same_width = char_width(line[cursor]) == char_width(char)
                    edit_pos = cursor
                    line[cursor] = char
                    cursor += 1
                    if same_width:
                        # The common case (an ASCII overwrite, or any
                        # same-width replacement) needs only the literal
                        # character written -- the terminal's own
                        # rendering advances the cursor correctly on its
                        # own, same reasoning as the plain-append case
                        # below.
                        await write(char)
                    else:
                        # A width mismatch (e.g. overwriting a CJK
                        # character with an ASCII one) can leave a stale
                        # column from the old, wider glyph un-erased if
                        # just the literal character is written --
                        # dogfood feature request, international users
                        # found non-ASCII handling poor -- so fall back
                        # to a full tail redraw, the same primitive a
                        # mid-line insert already uses.
                        await redraw_tail(
                            write, move_back=0, edit_pos=edit_pos, line=line, new_cursor=cursor
                        )
                    continue

                if len(line) >= _MAX_LINE_LENGTH:
                    # Drop the character but keep reading — Backspace,
                    # movement, and Enter still work normally past the
                    # cap. A bell once (not once per dropped character,
                    # which would turn a long paste into a bell storm)
                    # is the same "rejected, but the prompt stays active"
                    # signal invalid-key rejection already uses elsewhere
                    # (e.g. `netbbs.net.confirm.read_confirmation_
                    # choice`) -- silent truncation with zero feedback
                    # previously let a caller paste a letter far past
                    # this cap with no indication the tail was lost.
                    if not line_limit_warned:
                        await write("\a")
                        line_limit_warned = True
                    continue

                edit_pos = cursor
                line.insert(cursor, char)
                cursor += 1
                if cursor == len(line):
                    # Appending at the end -- the common case while
                    # typing normally -- needs only the one character
                    # written, not a full (empty) tail reprint.
                    await write(char)
                else:
                    await redraw_tail(
                        write, move_back=0, edit_pos=edit_pos, line=line, new_cursor=cursor
                    )
            finally:
                if live_buffer is not None:
                    live_buffer.update(line, cursor)

    # The buffer reset and final CRLF write already happened above,
    # inside the lock, at the moment Enter was handled (GitHub issue
    # #45) -- nothing left to do here but finish up with `submitted`.
    if history is not None:
        history.record(submitted)
    return submitted


async def read_key(source: ByteSource, write: WriteFunc, echo: bool = True) -> str:
    """
    Read a single character and return immediately — the character-mode
    equivalent of a classic BBS hotkey menu: intended for genuine
    single-choice menu selections, not free-text input, which should keep
    using `read_line`.

    Control bytes with no meaning as a standalone "key" — Delete, CR/LF,
    escape sequences (recognized or not; there's no line here for
    Left/Right/Home/End/Delete to act within, and Up/Down have no
    history to recall in a single-keystroke menu) — are silently
    skipped and reading continues, rather than being returned as a key
    in their own right. Four narrow exceptions: Ctrl-L and Ctrl-R
    (issue #102) are returned as `REDRAW_KEY`/`REFRESH_KEY`, unechoed
    (unlike every other returned key below) — echoing a raw Ctrl-L byte
    back to a real terminal risks triggering its own local
    form-feed/clear behavior, fighting whatever redraw the caller is
    about to do on purpose. Ctrl-H (issue #150) is returned as
    `HELP_KEY` and Ctrl-C (issue #157) as `CANCEL_KEY`, also unechoed,
    for the same reason -- see each constant's own docstring for why
    reusing Backspace's/a still-unclaimed byte here is safe. Every
    *other* control byte, Backspace included, keeps the plain "no
    meaning, keep reading" treatment unchanged.
    """
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        if b in (_CR, _LF, _DEL):
            continue

        if b == _ESC:
            await _read_escape_sequence(source)
            continue

        if b == ord(REDRAW_KEY):
            return REDRAW_KEY
        if b == ord(REFRESH_KEY):
            return REFRESH_KEY
        if b == ord(HELP_KEY):
            return HELP_KEY
        if b == ord(CANCEL_KEY):
            return CANCEL_KEY

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


async def read_any_key(source: ByteSource, write: WriteFunc, echo: bool = True) -> str:
    """
    Wait for literally one keystroke -- Enter included -- and return.

    Dogfood report: every "Press any key to continue..." pause in this
    codebase (`netbbs.net.help_overlay.show_help` and its many
    `admin_flow` siblings) calls `read_key` above to wait for the
    dismissal. `read_key`'s own CR/LF-skip is exactly correct for a
    hotkey menu -- Enter alone has no meaning as a choice there -- but
    that same skip means Enter silently does *nothing* at a "press any
    key" pause, arguably the single most natural key to reach for. This
    function is `read_key`'s sibling for that different context: no key
    is treated as meaningless here, and the return value carries no
    reliable meaning of its own (every real caller discards it) -- it
    exists only so this can share `read_key`'s call shape.

    Still drains a full escape sequence after ESC and an optional paired
    LF/NUL after a bare CR (the same helpers `read_key`/`read_editor_key`
    already rely on for this), so an arrow-key press or a CRLF pair
    doesn't leak a stray byte into whatever this screen redraws into
    next.
    """
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        if b == _CR:
            await _consume_optional_lf_or_nul(source)
            return "\r"
        if b == _ESC:
            await _read_escape_sequence(source)
            return "\x1b"
        if b < 0x20 or b == _DEL:
            return chr(b)

        if b < 0x80:
            char = chr(b)
        else:
            char = await _read_utf8_continuation(source, b)
            if char is None:
                continue  # malformed/interrupted multi-byte sequence -- keep waiting for a real key

        await write(char if echo else "*")
        return char


class EditorKeyKind(Enum):
    """Design doc -- welcome banner: the structured key-event
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
    # INSERT has no meaning for the ANSI editor
    # (typing always overwrites the cell at the cursor already, no
    # separate overwrite-mode toggle) -- a real INSERT keypress simply
    # isn't surfaced as anything by read_editor_key below.
}


async def read_editor_key(source: ByteSource, *, distinguish_ctrl_h: bool = False) -> EditorKey:
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

    `distinguish_ctrl_h` (issue #160's cursor-navigation follow-up on
    `netbbs.net.resource_editor.edit_resource_draft`) -- `False` by
    default, matching every existing caller's behavior byte-for-byte
    (`netbbs.net.prose_editor`/`ansi_editor` both genuinely need 0x08
    to keep meaning real character-deleting Backspace, since that's the
    byte most terminals actually send for it). `True` splits 0x08
    specifically off from the `_BS`/`_DEL` collapse into its own
    `EditorKeyKind.CTRL, char="h"` event instead -- safe only for a
    caller, like `edit_resource_draft`, whose own top-level dispatch
    never needs a real Backspace (any actual typing happens inside a
    field's own `read_line`-based sub-prompt), mirroring `read_key`'s
    own pre-existing `HELP_KEY` carve-out for the identical byte. 0x7F
    (`_DEL`) is unaffected either way -- it's unambiguously the "real
    Backspace key" byte on virtually every modern terminal, never
    itself repurposed as a Ctrl combo.
    """
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        if b in (_CR, _LF):
            if b == _CR:
                await _consume_optional_lf_or_nul(source)
            return EditorKey(EditorKeyKind.ENTER)

        if b == _BS and distinguish_ctrl_h:
            return EditorKey(EditorKeyKind.CTRL, char="h")

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


async def discard_buffered_enter(source: ByteSource) -> None:
    """Discard a CR/LF already queued behind a completed hotkey.

    The bounded peek happens before the next prompt is rendered, so an Enter
    arriving in this window belongs to the just-completed response. Ordinary
    input is pushed back unchanged for the next logical read. CRLF and CR-NUL
    pairs are consumed as one line ending through the existing helper.
    """
    peek = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    if peek == _CR:
        await _consume_optional_lf_or_nul(source)
    elif peek is not None and peek != _LF:
        _push_back(source, peek)


async def discard_buffered_input(source: ByteSource) -> None:
    """Discard every byte currently queued ahead of the next real read --
    the wider-scoped sibling of ``discard_buffered_enter`` above (which
    only ever looks for one trailing Enter). Loops the same bounded peek
    until nothing more arrives: a genuinely idle connection (the common
    case) returns after one short wait; a burst of already-buffered
    input (someone mid-typing when evicted) drains in a handful of
    near-instant reads before that same final wait concludes there's
    nothing left.
    """
    while True:
        peek = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
        if peek is None:
            return


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
        if cb is None:
            return None
        if not (0x80 <= cb <= 0xBF):
            # Not a continuation byte -- the lead byte's sequence is
            # malformed/incomplete (e.g. a client sending single-byte
            # Latin-1/CP1252 for extended characters instead of UTF-8),
            # but `cb` itself is a real byte the caller hasn't consumed
            # yet. Push it back so it's reprocessed as its own character
            # on the next read instead of being silently dropped, which
            # would desync every byte after it for the rest of the line.
            _push_back(source, cb)
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
    the rest" replaces an original "discard everything"
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
    `_discard_escape_sequence`, and direct testing against
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
