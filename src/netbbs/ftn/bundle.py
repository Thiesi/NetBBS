"""
Mail bundles: archives of packets exchanged with a hub (FTS-5005 naming).

A hub usually sends echomail as ZIP archives named for the day of the
week (`0000fff8.mo0` ... `.su9`, then `.moa` ...), and sometimes bare
`.pkt` files. The name says nothing reliable about the format, so an
archive is recognised by its first bytes (`archive_kind`).

ZIP is unpacked in-process (`unpack_bundle`). LHA, ARC, 7-Zip, RAR and
ARJ are unpacked by the sandboxed external tools `FILE_ID.DIZ` reading
uses (`netbbs.files.extract`, issue #1477): `open_bundle` takes any of
them. Without an installed tool it raises `BundleToolMissing`, naming the
tool to install.

Both are bounded against a hostile or broken archive: at most
`MAX_MEMBERS` members, and `MAX_UNPACKED_BYTES` unpacked in total,
counted while reading rather than trusted from the archive's own
directory, since a zip bomb lies there. They return the `*.pkt` members
and, apart, every other member, so the caller can hold those rather than
drop them; an archive with no packet at all is not a mail bundle and is
not read. Members are returned by base name. Nothing is extracted to
disk: a member's path cannot escape anywhere, and a name that could be
read as a wildcard or an option makes the tools refuse the archive.

Outbound, a network can bundle its packets as one ZIP per session
(`build_bundle`, named by `bundle_name`); see `netbbs.ftn.outbound`.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import zipfile

from netbbs.files.extract import KIND_LABELS, ArchiveError, ArchiveToolMissing, Member, unpack_with_tools
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress

MAX_MEMBERS = 256
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
TOOL_BUDGET_SECONDS = 180
"""Wall-clock ceiling for unpacking one bundle with external tools: the
listing and one run per member, each also under its own timeout."""
TOOL_KINDS = tuple(KIND_LABELS)
"""Archive kinds the external tools open: lha, arc, 7z, rar and arj."""
_CHUNK = 64 * 1024


class BundleToolMissing(FtnFormatError):
    """No unpacker for the bundle's format is installed; the message names
    what to install."""

_DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_SEQUENCE = "0123456789abcdefghijklmnopqrstuvwxyz"

_SIGNATURES = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),  # an empty archive
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x60\xea", "arj"),
)


def archive_kind(data: bytes) -> str | None:
    """`zip`, `rar`, `7z`, `arj`, `lha`, `arc`, `pkt`, or None if unknown."""
    for signature, kind in _SIGNATURES:
        if data.startswith(signature):
            return kind
    if len(data) >= 7 and data[2:4] == b"-l" and data[6:7] == b"-":
        return "lha"
    if len(data) >= 2 and data[0] == 0x1A and 1 <= data[1] <= 0x14:
        return "arc"
    if len(data) >= 20 and data[18:20] == b"\x02\x00":
        return "pkt"
    return None


def extract_packets(data: bytes) -> list[tuple[str, bytes]]:
    """The `.pkt` members of a ZIP bundle as `(base name, bytes)`, in archive
    order. Raises `FtnFormatError` for another archive kind, a damaged ZIP,
    or one over the bounds."""
    return unpack_bundle(data)[0]


def unpack_bundle(data: bytes) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]]]:
    """`(packets, others)`: the `.pkt` members of a ZIP bundle and every other
    member, each as `(base name, bytes)` in archive order. A ZIP with no
    `.pkt` member gives `([], [])` without reading a member: it is not a
    bundle, and the caller keeps it whole. Every member read counts against
    the bounds. Raises `FtnFormatError` for another archive kind, a damaged
    ZIP, or one over the bounds."""
    kind = archive_kind(data)
    if kind != "zip":
        raise FtnFormatError(f"bundle is {kind or 'not a recognised archive'}; only ZIP bundles can be unpacked")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FtnFormatError(f"damaged ZIP bundle: {exc}") from exc
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_MEMBERS:
            raise FtnFormatError(f"bundle holds {len(members)} members, more than {MAX_MEMBERS}")
        named = [(info.filename.replace("\\", "/").rsplit("/", 1)[-1], info) for info in members]
        if not any(_is_packet(name) for name, _ in named):
            return [], []
        packets, others = [], []
        total = 0
        for name, info in named:
            content = bytearray()
            try:
                with archive.open(info) as member:
                    while chunk := member.read(_CHUNK):
                        total += len(chunk)
                        if total > MAX_UNPACKED_BYTES:
                            raise FtnFormatError(f"bundle unpacks to more than {MAX_UNPACKED_BYTES} bytes")
                        content += chunk
            except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError) as exc:
                raise FtnFormatError(f"cannot unpack {name!r} from the bundle: {exc}") from exc
            (packets if _is_packet(name) else others).append((name, bytes(content)))
        return packets, others


async def open_bundle(data: bytes) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]]]:
    """`unpack_bundle` for every archive kind this node can open: ZIP
    in-process (off the event loop), the others through the sandboxed
    external tools. Raises `BundleToolMissing` when no tool for the kind is
    installed, and `FtnFormatError` for an unknown kind, a damaged
    archive, a tool that failed, or one over the bounds."""
    kind = archive_kind(data)
    if kind == "zip":
        return await asyncio.to_thread(unpack_bundle, data)
    if kind not in TOOL_KINDS:
        raise FtnFormatError(f"bundle is {kind or 'not a recognised archive'}")

    def choose(members: list[Member]) -> list[Member]:
        # Not a mail bundle without a packet: nothing is read.
        return members if any(_is_packet(member.name) for member in members) else []

    try:
        read = await unpack_with_tools(data, kind, choose=choose, max_members=MAX_MEMBERS,
                                       max_bytes=MAX_UNPACKED_BYTES, budget_seconds=TOOL_BUDGET_SECONDS,
                                       label="FTN bundle")
    except ArchiveToolMissing as exc:
        raise BundleToolMissing(str(exc)) from None
    except ArchiveError as exc:
        raise FtnFormatError(f"{KIND_LABELS[kind]} bundle: {exc}") from None
    packets, others = [], []
    for member, content in read:
        (packets if _is_packet(member.name) else others).append((member.name, content))
    return packets, others


def is_openable_archive(data: bytes) -> bool:
    """True for content `open_bundle` would try to open."""
    return archive_kind(data) in ("zip", *TOOL_KINDS)


def _is_packet(name: str) -> bool:
    return name.lower().endswith(".pkt")


def build_bundle(packets: list[tuple[str, bytes]]) -> bytes:
    """A ZIP bundle holding `packets`, each under its own name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in packets:
            archive.writestr(name, content)
    return buffer.getvalue()


def bundle_name(orig: FtnAddress, dest: FtnAddress, day: datetime.date, sequence: int) -> str:
    """`nnnnnnnn.dd#`: the net and node differences in hex (FTS-5005), the
    weekday, and a sequence character 0-9 then a-z."""
    if not 0 <= sequence < len(_SEQUENCE):
        raise ValueError(f"bundle sequence {sequence} is outside 0-{len(_SEQUENCE) - 1}")
    base = f"{(orig.net - dest.net) & 0xFFFF:04x}{(orig.node - dest.node) & 0xFFFF:04x}"
    return f"{base}.{_DAYS[day.weekday()]}{_SEQUENCE[sequence]}"


def packet_name(serial: int) -> str:
    """`xxxxxxxx.pkt`, eight hex digits, unique as long as `serial` is."""
    return f"{serial & 0xFFFFFFFF:08x}.pkt"
