"""
Mail bundles: archives of packets exchanged with a hub (FTS-5005 naming).

A hub usually sends echomail as ZIP archives named for the day of the
week (`0000fff8.mo0` ... `.su9`, then `.moa` ...), and sometimes bare
`.pkt` files. The name says nothing reliable about the format, so an
archive is recognised by its first bytes (`archive_kind`). Only ZIP is
unpacked; ARC, ARJ, LHA, RAR and 7-Zip are recognised so the refusal can
name them, and a hub can be asked to send ZIP or bare packets instead.

`extract_packets` is bounded against a hostile or broken archive: at most
`MAX_MEMBERS` members, and `MAX_UNPACKED_BYTES` unpacked in total,
counted while reading rather than trusted from the archive's own
directory, since a zip bomb lies there. Only members named `*.pkt` are
returned, by base name: nothing is written to disk here, so a member's
path cannot escape anywhere.
"""

from __future__ import annotations

import datetime
import io
import zipfile

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress

MAX_MEMBERS = 256
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
_CHUNK = 64 * 1024

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
        packets = []
        total = 0
        for info in members:
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not name.lower().endswith(".pkt"):
                continue
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
            packets.append((name, bytes(content)))
        return packets


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
