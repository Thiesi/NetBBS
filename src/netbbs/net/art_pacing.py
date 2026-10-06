"""
Paced SysOp art: an opening animation at an emulated line speed (issue
#929, step 6; design doc §3.2, "Paced art").

A SysOp can give a banner a speed -- 2400, 9600 or 38400 bps. NetBBS then
sends that art in small chunks at the speed a modem of the day would have
drawn it, so ANSI art that moves the cursor plays as the animation it was
drawn to be, and still art builds up top to bottom. The pacing is NetBBS's
own, not the terminal's (CTerm's `CSI Ps1 ; Ps2 * r`): it works in every
terminal, and a key can end it, which bytes already handed to a terminal
can't.

The rules, all here:

- any key ends the animation and the rest of the art is drawn at once. The
  key is swallowed (`Session.take_waiting_key`), so an Enter pressed to
  skip the welcome art doesn't submit an empty username;
- a draw never takes longer than `MAX_PACED_SECONDS`; past that, the rest
  goes out at once;
- each art plays once per connection (`once`): never again on a redraw,
  after a notice, or when a break-in hands the screen back;
- no pacing for a session without a live terminal (`Session.paces_art`),
  a caller who turned animations off, a plain-ASCII caller, or during a
  break-in.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Awaitable, Callable

from netbbs.config import get_config, set_config
from netbbs.net.session import Session, prepare_art_text, preformatted_rows, send_art_text
from netbbs.rendering.charset import ASCII
from netbbs.storage.database import Database

#: The art that can be paced, each with its own speed setting.
WELCOME_ART = "welcome"
MAIN_MENU_ART = "main_menu"

#: The speeds a SysOp can choose, in bits per second; 0 is off, the default.
ART_SPEEDS: tuple[int, ...] = (0, 2400, 9600, 38400)

#: The longest one paced draw may take. Past it, the rest goes out at once.
MAX_PACED_SECONDS = 5.0

#: How often a chunk goes out. Small enough to look smooth, large enough not
#: to send a packet per character.
_CHUNKS_PER_SECOND = 30

#: A line's ten bits per character: start bit, eight data bits, stop bit.
_BITS_PER_CHARACTER = 10

#: What a chunk may never split: an escape sequence, a CR LF pair, or one
#: character.
_ATOM = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|\r\n|.", re.DOTALL)


def _speed_key(kind: str) -> str:
    return f"{kind}_art_speed"


def art_speed(db: Database, kind: str) -> int:
    """The speed set for the `kind` banner (`welcome`, `main_menu`, ...),
    in bps; 0, off, unless the SysOp chose one of `ART_SPEEDS`."""
    value = get_config(db, _speed_key(kind))
    try:
        speed = int(value) if value is not None else 0
    except ValueError:
        return 0
    return speed if speed in ART_SPEEDS else 0


def set_art_speed(db: Database, kind: str, speed: int) -> None:
    if speed not in ART_SPEEDS:
        raise ValueError(f"unknown art speed {speed!r}")
    set_config(db, _speed_key(kind), str(speed))


def will_pace(session: Session, speed: int, once: str | None) -> bool:
    """Whether art drawn now at `speed` plays as an animation. `once` is
    the play it counts as, once per session; `None` (a SysOp's preview)
    plays every time and uses up no caller's play."""
    if speed <= 0 or not getattr(session, "paces_art", False):
        return False
    if not getattr(session, "animations_enabled", True):
        return False
    if getattr(session, "output_charset", None) == ASCII:
        return False
    if getattr(session, "in_break_in", False):
        return False
    return once is None or once not in _played(session)


def _played(session: Session) -> set[str]:
    played = getattr(session, "art_played", None)
    if played is None:
        played = set()
        session.art_played = played  # type: ignore[attr-defined]
    return played


async def write_paced_art(session: Session, text: str, *, speed: int, once: str) -> None:
    """`write_preformatted_line` for art that may be paced: `text` is laid
    out the same way, then sent at `speed` if `will_pace` says so."""
    await write_paced_art_text(session, preformatted_rows(session, text), speed=speed, once=once)


async def write_paced_art_text(session: Session, text: str, *, speed: int, once: str) -> None:
    """`write_art_text` for laid-out art that may be paced, such as slot art
    drawn into cursor-positioned rows (`render_slot_art`), revealed top to
    bottom. The whole art is prepared once (`prepare_art_text`: iCE colours,
    CTerm's bright backgrounds), so no chunk loses a colour state an earlier
    chunk set."""
    prepared = prepare_art_text(session, text)
    await _write_paced(session, prepared, speed=speed, once=once, write=lambda part: send_art_text(session, part))


async def write_preview_art(session: Session, text: str, *, speed: int) -> None:
    """A SysOp's `[P]review` of art (issue #1083 finding 9): laid out as
    `write_paced_art` lays it out, and played at `speed` every time, so the
    preview shows the animation callers will see. The skip key and the
    five-second cap apply as for callers."""
    await write_preview_art_text(session, preformatted_rows(session, text), speed=speed)


async def write_preview_art_text(session: Session, text: str, *, speed: int) -> None:
    """`write_preview_art` for laid-out art, such as slot art."""
    prepared = prepare_art_text(session, text)
    await _write_paced(session, prepared, speed=speed, once=None, write=lambda part: send_art_text(session, part))


async def _write_paced(
    session: Session,
    text: str,
    *,
    speed: int,
    once: str | None,
    write: Callable[[str], Awaitable[None]],
    clock: Callable[[], float] = time.monotonic,
) -> None:
    if not will_pace(session, speed, once):
        await write(text)
        return
    if once is not None:
        _played(session).add(once)
    await pace(session, text, speed=speed, write=write, clock=clock)


async def pace(
    session: Session,
    text: str,
    *,
    speed: int,
    write: Callable[[str], Awaitable[None]],
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Send `text` through `write` at `speed` bps, checking for a key
    between chunks. A key, a break-in, or `MAX_PACED_SECONDS` sends the
    rest at once. `clock` is injectable so tests never sleep."""
    atoms = _ATOM.findall(text)
    per_second = speed / _BITS_PER_CHARACTER
    per_chunk = max(1, math.ceil(per_second / _CHUNKS_PER_SECOND))
    start = clock()
    sent = 0
    index = 0
    while index < len(atoms):
        if session.in_break_in:
            await write("".join(atoms[index:]))
            return
        chunk: list[str] = []
        size = 0
        while index < len(atoms) and size < per_chunk:
            chunk.append(atoms[index])
            size += len(atoms[index])
            index += 1
        await write("".join(chunk))
        sent += size
        if index >= len(atoms):
            return
        elapsed = clock() - start
        if elapsed >= MAX_PACED_SECONDS:
            await write("".join(atoms[index:]))
            return
        wait = min(sent / per_second, MAX_PACED_SECONDS) - elapsed
        if wait > 0 and await session.take_waiting_key(wait):
            await write("".join(atoms[index:]))
            return
