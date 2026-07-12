"""
Transport-agnostic character-mode line/key reading shared by Telnet and SSH.

A transport supplies raw bytes through the ByteSource protocol. This module
handles echo, Backspace/Delete, line endings, UTF-8 reconstruction, and
unsupported terminal escape sequences consistently across transports.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

_CR = 0x0D
_LF = 0x0A
_NUL = 0x00
_BS = 0x08
_DEL = 0x7F
_ESC = 0x1B

_FOLLOWUP_BYTE_TIMEOUT = 0.05
_MAX_LINE_LENGTH = 4096
_PUSHBACK_ATTR = "_netbbs_char_input_pushback"


class ByteSource(Protocol):
    async def read_byte(self) -> int | None:
        """Return the next data byte, or None for a transport-only action."""
        ...

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        """Return one byte within timeout, or None when no byte is available."""
        ...


WriteFunc = Callable[[str], Awaitable[None]]


def _push_back(source: ByteSource, byte: int) -> None:
    """Save one consumed byte for the next logical read.

    CR handling needs a bounded lookahead to consume CRLF and CR-NUL. When the
    lookahead is ordinary input, it belongs to the next line and must not be
    discarded. Keeping the tiny buffer here makes the fix transport-agnostic
    and avoids duplicating unread logic in TelnetSession and SSHSession.
    """
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
    """Read one line with server-side echo and simple end-of-line editing."""
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

        if len(line) < _MAX_LINE_LENGTH:
            line.append(char)
            await write(char if echo else "*")

    await write("\r\n")
    return "".join(line)


async def read_key(source: ByteSource, write: WriteFunc, echo: bool = True) -> str:
    """Read and return the next printable character immediately."""
    while True:
        b = await _read_byte(source)
        if b is None:
            continue
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
    if 0xC2 <= lead_byte <= 0xDF:
        extra = 1
    elif 0xE0 <= lead_byte <= 0xEF:
        extra = 2
    elif 0xF0 <= lead_byte <= 0xF4:
        extra = 3
    else:
        return None

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
    peek = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    if peek is not None and peek not in (_LF, _NUL):
        _push_back(source, peek)


async def _discard_escape_sequence(source: ByteSource) -> None:
    next_byte = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
    if next_byte is None:
        return

    if next_byte == 0x5B:  # '[' — CSI sequence
        while True:
            b = await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
            if b is None:
                return
            if 0x40 <= b <= 0x7E:
                return
    elif next_byte == 0x4F:  # 'O' — SS3 sequence
        await _read_byte_with_timeout(source, _FOLLOWUP_BYTE_TIMEOUT)
