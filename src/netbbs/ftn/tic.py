"""
TIC files (FTS-5006): the control file that travels with a file-echo file.
Design doc §6.8 "File echoes".

A TIC is untrusted text from another system, read here and nowhere else:

- `parse_tic` takes at most `MAX_TIC_BYTES`, reads keyword lines (CR, LF
  or CR LF), keywords case-insensitively, and keeps only what the node
  uses. It refuses a TIC without `Area`, `File` or a hexadecimal `Crc`.
  Repeated keywords are bounded (`MAX_LDESC_LINES`, `MAX_REPLACES`).
- `name_problem` says why a name may not become a file's name, under the
  rules for a SysOp upload's name (§6.2): no folder part, no leading dot,
  no control characters or colon, at most 255 bytes. `File`, `Lfile` and
  `Replaces` values are never used as paths.
- `replaces_pattern_problem` and `matches_replaces` decide what a
  `Replaces` value may remove: a plain name where `*` and `?` match within
  it, compared case-insensitively. The match is a linear-time table, never
  a regular expression built from the value, so a hostile pattern costs no
  more than its length times the name's.
- `crc32` is FTS-5006 §3's CRC-32 (the ITU-T one `zlib` computes).

Written here as well (issue #1476, hatching): `write_tic` makes the TIC
this node sends with a file it hatches, CR LF lines in CP437 when every
character fits and UTF-8 otherwise, as outbound text is. `dos_name` gives
the 8.3 `File` name a long name travels under, beside `Lfile`.
"""

from __future__ import annotations

import datetime
import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address

MAX_TIC_BYTES = 64 * 1024
MAX_NAME_BYTES = 255
MAX_LDESC_LINES = 64
MAX_REPLACES = 16
# A forwarded TIC's history (issue #1479): what one keeps of its Path and
# Seenby lines, far more than any real echo's route.
MAX_PATH_LINES = 64
MAX_SEEN_BY = 1_000
_MAX_LINE = 1024


@dataclass(frozen=True)
class Tic:
    area: str
    file: str
    crc: int
    long_name: str | None = None  # Lfile, or its alias Fullname
    size: int | None = None
    desc: str | None = None
    ldesc: tuple[str, ...] = ()
    replaces: tuple[str, ...] = ()
    origin: FtnAddress | None = None
    from_address: FtnAddress | None = None
    password: str = ""
    # --- TIC forwarding (issue #1479): the route it came by ---
    path: tuple[str, ...] = ()  # each Path line's value, as written
    seen_by: tuple[FtnAddress, ...] = ()
    # --- end TIC forwarding ---

    @property
    def path_addresses(self) -> tuple[FtnAddress, ...]:
        """The address each Path line names first: the systems the file
        passed through (issue #1479)."""
        return tuple(address for address in (_address(line) for line in self.path) if address is not None)

    @property
    def description(self) -> str | None:
        """`LDesc` lines when there are any, else `Desc`; raw, to be
        normalised by the caller as every description is."""
        if any(line.strip() for line in self.ldesc):
            return "\n".join(self.ldesc)
        return self.desc if self.desc and self.desc.strip() else None


def parse_tic(data: bytes) -> Tic:
    """The TIC in `data`; raises `FtnFormatError` with a reason a SysOp can
    read when it is too long or lacks what a file echo needs."""
    if len(data) > MAX_TIC_BYTES:
        raise FtnFormatError(f"the TIC is {len(data)} bytes, more than {MAX_TIC_BYTES}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp437")
    values: dict[str, str] = {}
    ldesc: list[str] = []
    replaces: list[str] = []
    path: list[str] = []
    seen_by: list[FtnAddress] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw[:_MAX_LINE].strip("\x00 \t\x1a")
        if not line:
            continue
        keyword, _, value = line.partition(" ")
        keyword = keyword.strip().lower()
        value = value.strip()
        if keyword == "ldesc":
            if len(ldesc) < MAX_LDESC_LINES:
                ldesc.append(value)
        elif keyword == "replaces":
            if value and len(replaces) < MAX_REPLACES:
                replaces.append(value)
        elif keyword == "path":
            if value and len(path) < MAX_PATH_LINES:
                path.append(" ".join(value.split()))
        elif keyword == "seenby":
            for word in value.split():
                address = _address(word)
                if address is not None and len(seen_by) < MAX_SEEN_BY:
                    seen_by.append(address)
        elif keyword and keyword not in values:
            values[keyword] = value
    area, file_name, crc_text = values.get("area", ""), values.get("file", ""), values.get("crc", "")
    missing = [name for name, value in (("Area", area), ("File", file_name), ("Crc", crc_text)) if not value]
    if missing:
        raise FtnFormatError(f"the TIC has no {', '.join(missing)}")
    crc_text = crc_text.split()[0]
    if len(crc_text) > 8 or any(c not in "0123456789abcdefABCDEF" for c in crc_text):
        raise FtnFormatError(f"the TIC's Crc {crc_text[:20]!r} is not an eight-digit hex number")
    size = None
    size_text = values.get("size", "").split()
    if size_text:
        if not size_text[0].isdigit() or not size_text[0].isascii() or len(size_text[0]) > 12:
            raise FtnFormatError(f"the TIC's Size {size_text[0][:20]!r} is not a number")
        size = int(size_text[0])
    return Tic(
        area=area.split()[0],
        file=file_name,
        crc=int(crc_text, 16),
        long_name=values.get("lfile") or values.get("fullname") or None,
        size=size,
        desc=values.get("desc") or None,
        ldesc=tuple(ldesc),
        replaces=tuple(replaces),
        origin=_address(values.get("origin")),
        from_address=_address(values.get("from")),
        password=values.get("pw", ""),
        path=tuple(path),
        seen_by=tuple(seen_by),
    )


def _address(value: str | None) -> FtnAddress | None:
    if not value:
        return None
    try:
        return parse_address(value.split()[0])
    except FtnFormatError:
        return None


def name_problem(name: str) -> str | None:
    """Why `name` can't be a stored file's name, or None: the rules for a
    SysOp upload's name (§6.2)."""
    if not name:
        return "it is empty"
    if "/" in name or "\\" in name or name in (".", ".."):
        return "it has a folder part"
    if name.startswith("."):
        return "it starts with a dot"
    if any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) < 0xA0 for c in name):
        return "it has control characters"
    if ":" in name:
        return "it has a colon"
    if name != name.strip():
        return "it starts or ends with a space"
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        return f"it is longer than {MAX_NAME_BYTES} bytes"
    return None


def stored_name(tic: Tic) -> str:
    """The name the file is stored under: `Lfile` (or `Fullname`) when it
    passes `name_problem`, else `File`. Raises `FtnFormatError` when `File`
    itself doesn't pass (check 6)."""
    problem = name_problem(tic.file)
    if problem is not None:
        raise FtnFormatError(f"the TIC's File {tic.file[:80]!r} is not a safe name: {problem}")
    if tic.long_name and name_problem(tic.long_name) is None:
        return tic.long_name
    return tic.file


def replaces_pattern_problem(value: str) -> str | None:
    """Why a `Replaces` value is ignored, or None. A value with a folder
    part, a drive colon or `..` never matches anything."""
    if any(part in value for part in ("/", "\\", ":", "..")):
        return "it names a folder or a drive"
    if not value or len(value.encode("utf-8")) > MAX_NAME_BYTES:
        return "it is empty or too long"
    return None


def matches_replaces(pattern: str, name: str) -> bool:
    """Whether `name` matches a `Replaces` value: case-insensitive, `*` any
    run of characters and `?` exactly one, within the name. Linear in the
    product of the lengths."""
    pattern, name = pattern.casefold(), name.casefold()
    # reachable[j]: pattern[:i] can match name[:j]
    reachable = [True] + [False] * len(name)
    for symbol in pattern:
        if symbol == "*":
            seen = False
            for j in range(len(name) + 1):
                seen = seen or reachable[j]
                reachable[j] = seen
        else:
            for j in range(len(name), 0, -1):
                reachable[j] = reachable[j - 1] and (symbol == "?" or symbol == name[j - 1])
            reachable[0] = False
    return reachable[len(name)]


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def is_tic_name(name: str) -> bool:
    return name.lower().endswith(".tic")


# --- writing (issue #1476) ----------------------------------------------------

_DOS_PART = re.compile(r"[A-Za-z0-9_-]")
_DOS_NAME = re.compile(r"[A-Za-z0-9_-]{1,8}(\.[A-Za-z0-9_-]{1,3})?")
# What one written value may hold: the parser reads at most `_MAX_LINE`
# characters of a line, keyword included.
_MAX_VALUE = _MAX_LINE - 16


def is_dos_name(name: str) -> bool:
    """Whether `name` is already an 8.3 name of the characters BinkP sends
    as they are."""
    return _DOS_NAME.fullmatch(name) is not None


def dos_name(name: str) -> str:
    """The 8.3 `File` name `name` is sent under: itself when it is one
    already, else up to six letters and digits of its stem, two hex digits
    of the long name's CRC-32 (so two long names sharing a stem stay apart),
    and up to three of its last extension, upper case."""
    if is_dos_name(name):
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        stem, extension = name, ""
    short = "".join(_DOS_PART.findall(stem)).upper()[:6] or "FILE"
    short += f"{crc32(name.encode('utf-8')) & 0xFF:02X}"
    extension = "".join(_DOS_PART.findall(extension)).upper()[:3]
    return f"{short}.{extension}" if extension else short


def _one_line(text: str) -> str:
    """A description line as a TIC value: control characters become
    spaces, runs of spaces one, cut to what the parser reads."""
    cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) < 0xA0 else c for c in text)
    return " ".join(cleaned.split())[:_MAX_VALUE]


def _plain(value: str, what: str) -> str:
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > _MAX_VALUE:
        raise FtnFormatError(f"a TIC's {what} {value[:40]!r} is empty, too long or has control characters")
    return value


def write_tic(
    tic: Tic, *, to: FtnAddress, path: Sequence[tuple[FtnAddress, int]], seen_by: Sequence[FtnAddress],
    date: int | None = None, created: str | None = None, earlier_path: Sequence[str] = (),
) -> bytes:
    """The TIC (FTS-5006) for `tic`, from `tic.from_address` to `to`: its
    Area, Origin, From, To, File and Lfile, Size, Date, Desc and Ldesc,
    Replaces, Crc, the `path` lines (address, Unix time), the Seenby lines
    and Pw. `earlier_path` is the Path lines a forwarded TIC came with
    (issue #1479), written as they were before this node's own. `tic.origin`
    and `tic.from_address` must be set, and `File` must be an 8.3 name. Raises `FtnFormatError` for a value no TIC can
    carry."""
    if tic.origin is None or tic.from_address is None:
        raise FtnFormatError("a TIC this node writes names its Origin and From")
    if not is_dos_name(tic.file):
        raise FtnFormatError(f"a TIC's File {tic.file!r} must be an 8.3 name")
    if " " in tic.area:
        raise FtnFormatError(f"a TIC's Area {tic.area!r} has a space")
    lines = [
        f"Area {_plain(tic.area, 'Area')}",
        f"Origin {tic.origin.four_d}",
        f"From {tic.from_address.four_d}",
        f"To {to.four_d}",
        f"File {tic.file}",
    ]
    if tic.long_name and tic.long_name != tic.file:
        problem = name_problem(tic.long_name)
        if problem is not None:
            raise FtnFormatError(f"a TIC's Lfile {tic.long_name[:40]!r} is not a safe name: {problem}")
        lines.append(f"Lfile {tic.long_name}")
    if tic.size is not None:
        lines.append(f"Size {tic.size}")
    if date is not None:
        lines.append(f"Date {date}")
    desc = _one_line(tic.desc or "")
    if desc:
        lines.append(f"Desc {desc}")
    ldesc = [_one_line(line) for line in tic.ldesc][:MAX_LDESC_LINES]
    lines += [f"Ldesc {line}" if line else "Ldesc" for line in ldesc]
    for value in tic.replaces[:MAX_REPLACES]:
        lines.append(f"Replaces {_plain(value, 'Replaces')}")
    if created:
        lines.append(f"Created {_one_line(created)}")
    lines.append(f"Crc {tic.crc & 0xFFFFFFFF:08X}")
    lines += [f"Path {_one_line(value)}" for value in earlier_path[:MAX_PATH_LINES] if _one_line(value)]
    for address, when in path:
        stamp = datetime.datetime.fromtimestamp(when, datetime.timezone.utc)
        lines.append(f"Path {address.four_d} {when} {stamp:%a %b} {stamp.day} {stamp:%H:%M:%S %Y} UTC")
    for address in seen_by:
        lines.append(f"Seenby {address.four_d}")
    if tic.password:
        lines.append(f"Pw {_plain(tic.password, 'Pw')}")
    text = "\r\n".join(lines) + "\r\n"
    try:
        data = text.encode("cp437")
    except UnicodeEncodeError:
        data = text.encode("utf-8")
    if len(data) > MAX_TIC_BYTES:
        raise FtnFormatError(f"the TIC would be {len(data)} bytes, more than {MAX_TIC_BYTES}")
    return data
