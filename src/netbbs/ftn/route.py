"""
Forwarding a received message on: SEEN-BY and PATH for echomail, Via lines
for netmail (FTS-0004, FTS-4001, FTS-4009; design doc §6.8, "Points and
downlinks").

Pure byte work on a `PackedMessage`, like the rest of the package's lowest
layer: nothing here reads the database. The message is changed only where
forwarding must change it -- its control lines at the end and the packed
header's net/node fields -- and never decoded and re-encoded, so a message
forwarded on is byte for byte the message received, but for those.

- `point_echomail_for_uplink`: an echomail a point wrote, as this node
  sends it to its uplink. A point is never in SEEN-BY or PATH (FTS-0004,
  FSC-0074): its boss writes them as if the message were its own, so any
  the point's software wrote are replaced by this node and its uplink in
  SEEN-BY and this node in PATH.
- `merge_seen_by_path`: SEEN-BY merged and this node added to PATH, as a
  hub forwards echomail to its downlinks (the fan-out of #1479 part 2
  uses it).
- `via_addresses`, `append_via`: the netmail Via lines a loop check and a
  hop limit read, and the one this node adds when it routes a netmail on.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import replace

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.message import MAX_CONTROL_LINE, format_net_node_lines, parse_net_nodes
from netbbs.ftn.packet import MAX_TEXT_BYTES, PackedMessage

_KLUDGE = b"\x01"
_SEEN_BY = b"SEEN-BY:"
_PATH = b"\x01PATH:"
_VIA = b"\x01Via"
_VIA_ADDRESS = re.compile(r"\d{1,5}:\d{1,5}/\d{1,5}(?:\.\d{1,5})?")


def _lines(text: bytes) -> list[bytes]:
    lines = text.replace(b"\n", b"").split(b"\r")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _join(lines: list[bytes]) -> bytes:
    text = b"".join(line + b"\r" for line in lines)
    if len(text) > MAX_TEXT_BYTES:
        raise FtnFormatError(f"the forwarded message would be over {MAX_TEXT_BYTES} bytes")
    return text


def _split_tail(lines: list[bytes]) -> tuple[list[bytes], list[bytes]]:
    """The message's lines before its trailing control lines, and those:
    the run at the end of SEEN-BY lines, kludges and blank lines. A
    SEEN-BY quoted in the body is not at the end, so it is left alone."""
    cut = len(lines)
    while cut > 0 and (lines[cut - 1].startswith(_SEEN_BY) or lines[cut - 1].startswith(_KLUDGE)
                       or not lines[cut - 1].strip()):
        cut -= 1
    return lines[:cut], lines[cut:]


def seen_by_and_path(text: bytes) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The SEEN-BY and PATH entries at the end of a message's text."""
    _body, tail = _split_tail(_lines(text))
    seen_by = [entry for line in tail if line.startswith(_SEEN_BY)
               for entry in parse_net_nodes(line[len(_SEEN_BY):].decode("ascii", errors="replace"))]
    path = [entry for line in tail if line.startswith(_PATH)
            for entry in parse_net_nodes(line[len(_PATH):].decode("ascii", errors="replace"))]
    return seen_by, path


def _with_seen_by_path(packed: PackedMessage, seen_by: list[tuple[int, int]], path: list[tuple[int, int]],
                       *, orig: FtnAddress, dest: FtnAddress) -> PackedMessage:
    body, tail = _split_tail(_lines(packed.text))
    kept = [line for line in tail if line.strip() and not line.startswith(_SEEN_BY) and not line.startswith(_PATH)]
    lines = body + [line.encode("ascii") for line in format_net_node_lines("SEEN-BY: ", sorted(set(seen_by)))]
    lines += kept
    lines += [_KLUDGE + line.encode("ascii")
              for line in format_net_node_lines("PATH: ", path, width=MAX_CONTROL_LINE - 1)]
    return replace(packed, text=_join(lines), orig_net=orig.net, orig_node=orig.node,
                   dest_net=dest.net, dest_node=dest.node)


def point_echomail_for_uplink(packed: PackedMessage, ours: FtnAddress, uplink: FtnAddress) -> PackedMessage:
    """An echomail a point of this node wrote, as this node sends it to its
    uplink: SEEN-BY this node and the uplink, PATH this node, and the
    packed header from this node to the uplink. Whatever SEEN-BY and PATH
    the point's software wrote are dropped: a point is never in either."""
    return _with_seen_by_path(packed, [ours.net_node, uplink.net_node], [ours.net_node], orig=ours, dest=uplink)


def merge_seen_by_path(packed: PackedMessage, ours: FtnAddress, dest: FtnAddress,
                       seen_by: list[tuple[int, int]]) -> PackedMessage:
    """An echomail forwarded to `dest` (a downlink, #1479 part 2): `seen_by`
    -- this node and every system it is sent to -- merged into its SEEN-BY,
    and this node added to the end of its PATH unless already last."""
    old_seen_by, path = seen_by_and_path(packed.text)
    if not path or path[-1] != ours.net_node:
        path = [*path, ours.net_node]
    return _with_seen_by_path(packed, [*old_seen_by, *seen_by, ours.net_node], path, orig=ours, dest=dest)


def via_addresses(text: bytes) -> list[FtnAddress]:
    """The address of each Via line in a netmail, in order: the first
    address the line names (FTS-4009 puts it first; older tossers write
    their name before it). A Via line naming no address is left out here
    but still counts in `via_count`."""
    found = []
    for line in _lines(text):
        if line.startswith(_VIA):
            match = _VIA_ADDRESS.search(line[len(_VIA):].decode("ascii", errors="replace"))
            if match is not None:
                try:
                    found.append(parse_address(match.group(0)))
                except FtnFormatError:
                    continue
    return found


def via_count(text: bytes) -> int:
    """How many Via lines a netmail carries: the systems it passed."""
    return sum(1 for line in _lines(text) if line.startswith(_VIA))


def passed_through(text: bytes, ours: FtnAddress) -> bool:
    """Whether a Via line names this node: the netmail passed here before,
    and routing it again would loop."""
    return any(address.same_node(ours) for address in via_addresses(text))


def format_via(ours: FtnAddress, product: str, moment: datetime.datetime) -> str:
    """A Via line's value (FTS-4009): `address @YYYYMMDD.HHMMSS.UTC product`."""
    stamp = moment.astimezone(datetime.timezone.utc).strftime("%Y%m%d.%H%M%S")
    return f"{ours} @{stamp}.UTC {product}"


def append_via(packed: PackedMessage, ours: FtnAddress, product: str, *,
               moment: datetime.datetime | None = None, kludges: list[tuple[str, str]] = (),
               attributes: int | None = None) -> PackedMessage:
    """A netmail as this node routes it on: a Via line naming this node
    added at the end, and `kludges` (INTL, FMPT the sender left out) added
    before the body. The packed header's net/node fields stay the
    originator's and the destination's, as FTS-0001 keeps them."""
    lines = _lines(packed.text)
    if kludges:
        start = 0
        if lines and lines[0].removeprefix(_KLUDGE).startswith(b"AREA:"):
            start = 1
        lines[start:start] = [_KLUDGE + f"{name} {value}".encode("ascii") for name, value in kludges]
    lines.append(_VIA + b" " + format_via(ours, product, moment or datetime.datetime.now(datetime.timezone.utc))
                 .encode("ascii", errors="replace"))
    return replace(packed, text=_join(lines),
                   attributes=packed.attributes if attributes is None else attributes)
