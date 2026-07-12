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
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

# Control byte values relevant to character-mode line building.
_CR = 0x0D
_LF = 0x0A
_NUL = 0x00
_BS = 0x08  # Backspace
_DEL = 0x7F  # Delete — many terminals send this for the Backspace key
_ESC = 0x1B

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


async def read_line(source: ByteSource, write: WriteFunc, echo: bool = True) -> str:
    """
    Read one line of input, character by character, echoing (or masking,
    if `echo=False`) each character via `write` as it arrives.

    `echo=False` masks each typed character with `*` instead of showing
    it as typed, matching common modern password-field UX (revealing
    length, not content) rather than the more conservative "reveal
    nothing" alternative.
    """
    line: list[str] = []
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

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
            await _discard_escape_sequence(source)
            continue

        if b < 0x20:
            # Any other control byte (Tab, Ctrl+C, Ctrl+D, etc.) — not
            # supported in this pass; discard rather than corrupt the
            # line or echo something meaningless.
            continue

        if b < 0x80:
            char = chr(b)
        else:
            char = await _read_utf8_continuation(source, b)
            if char is None:
                continue  # malformed/interrupted multi-byte sequence

        if len(line) < _MAX_LINE_LENGTH:
            line.append(char)
            await write(char if echo else "*")
        # else: silently drop the character but keep reading — Backspace
        # and Enter still work normally past the cap.

    await write("\r\n")
    return "".join(line)


async def read_key(source: ByteSource, write: WriteFunc, echo: bool = True) -> str:
    """
    Read a single character and return immediately — the character-mode
    equivalent of a classic BBS hotkey menu: intended for genuine
    single-choice menu selections, not free-text input, which should keep
    using `read_line`.

    Control bytes with no meaning as a standalone "key" — Backspace/
    Delete, CR/LF, unsupported escape sequences — are silently skipped
    and reading continues, rather than being returned as a key in their
    own right: there's no line being built here to backspace within, and
    Enter doesn't mean anything special when we're already responding to
    the very next keystroke, immediately.
    """
    while True:
        b = await _read_byte(source)
        if b is None:
            continue  # pure transport-level action, no data produced

        if b in (_CR, _LF, _BS, _DEL):
            continue

        if b == _ESC:
            await _discard_escape_sequence(source)
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


async def _discard_escape_sequence(source: ByteSource) -> None:
    """
    Consume and discard a terminal escape sequence following an ESC byte
    (arrow keys, function keys, Home/End, etc.) as a complete unit — not
    supported in this pass. Handles the two common shapes real terminals
    use for special keys:

    - CSI sequences: ESC [ ... <final byte in 0x40-0x7E> (the vast
      majority of arrow/function/navigation keys)
    - SS3 sequences: ESC O <single letter> (some terminals' "application
      cursor key mode" encoding for arrow keys)

    Anything else after a lone ESC (a real Escape keypress with nothing
    following, or a shape we don't recognize) is left alone after
    discarding just the ESC itself, on a short timeout — so we can never
    hang waiting for bytes that aren't coming, the same reasoning as
    `_consume_optional_lf_or_nul`.
    """
    next_byte = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    if next_byte is None:
        return

    if next_byte == 0x5B:  # '[' — CSI sequence
        while True:
            b = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
            if b is None:
                return
            if 0x40 <= b <= 0x7E:
                return  # final byte of the CSI sequence
    elif next_byte == 0x4F:  # 'O' — SS3 sequence, always exactly one more byte
        await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    # else: some other/unrecognized shape — just the ESC itself was
    # consumed; nothing more to do.
