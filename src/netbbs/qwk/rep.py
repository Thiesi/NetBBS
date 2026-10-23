"""
QWK reply packets (issue #1389): reading one a caller uploads, and writing
one (for tests, and for a QWKnet node sending to its hub).

A reply packet is a ZIP named `<BBSID>.REP` holding `<BBSID>.MSG`, whose
first block starts with the BBS ID instead of the copyright line, then one
header and text per message. In a reply the header's message-number field
holds the conference as ASCII, and its reference field the number of the
message answered. A QWKE reader may add `TODOOR.EXT`, whose `AREA <n> D`
line drops a conference.

**A reply packet is untrusted input.** `parse_rep` is the whole of what
reads one, and it is strict:

- the archive, the .MSG inside it, the number of messages and each body are
  bounded (`RepLimits`), and a member's inflated size is measured as it is
  read, not taken from the ZIP's header;
- the first block must name this BBS;
- a damaged header stops the walk and is reported, since no later message
  can be found again;
- text is decoded as CP437 with control characters dropped;
- QWKE's `From:` line is read but never used as the author: a reply is
  always its uploader's (Synchronet dropped the same line for spoofing).

What a message *does* -- the board it goes to, whether the caller may post
there -- is decided by the caller of `parse_rep`, which treats every field
here as a claim.
"""

from __future__ import annotations

import datetime
import io
import zipfile
import zlib
from dataclasses import dataclass, field

from netbbs.qwk.format import (
    BLOCK,
    QwkFormatError,
    body_lines,
    encode_text,
    from_cp437,
    pack_header,
    qwke_lines,
    split_qwke,
)
from netbbs.qwk.packet import open_archive, parse_messages, read_member, valid_bbs_id


class RepError(ValueError):
    """A reply packet that cannot be read at all; nothing in it is used."""


@dataclass(frozen=True)
class RepLimits:
    archive_bytes: int = 2 * 1024 * 1024
    msg_bytes: int = 4 * 1024 * 1024
    members: int = 32
    messages: int = 200
    body_bytes: int = 200_000


DEFAULT_LIMITS = RepLimits()


@dataclass(frozen=True)
class RepMessage:
    """One reply, as the packet claims it. `index` is its place in the
    packet, from 1, which the per-message report names it by."""

    index: int
    conference: int
    to: str
    subject: str
    body: str
    reference: int | None
    private: bool
    claimed_from: str = ""
    when: datetime.datetime | None = None


@dataclass(frozen=True)
class RepProblem:
    index: int | None
    reason: str


@dataclass(frozen=True)
class RepPacket:
    messages: list[RepMessage]
    problems: list[RepProblem] = field(default_factory=list)
    #: Conferences a QWKE reader asked to drop (`TODOOR.EXT`), and to keep.
    dropped: list[int] = field(default_factory=list)
    added: list[int] = field(default_factory=list)


def _conference_of(header) -> int:
    """The conference a reply is for: the ASCII number field, which every
    reader writes, else the binary word."""
    return header.number if header.number is not None else header.conference


def _todoor(text: str) -> tuple[list[int], list[int]]:
    dropped: list[int] = []
    added: list[int] = []
    for line in text.splitlines()[:1000]:
        parts = line.split()
        if len(parts) != 3 or parts[0].upper() != "AREA" or not parts[1].isdigit() or len(parts[1]) > 5:
            continue
        number = int(parts[1])
        if "D" in parts[2]:
            dropped.append(number)
        elif any(flag in parts[2] for flag in "apg"):
            added.append(number)
    return dropped, added


def _msg_bytes(data: bytes, bbs_id: str, limits: RepLimits) -> tuple[bytes, bytes | None]:
    """The .MSG inside a reply packet (and TODOOR.EXT, if any). A bare .MSG,
    not zipped, is accepted too: its first block says whose it is."""
    if len(data) > limits.archive_bytes:
        raise RepError(f"the reply packet is larger than {limits.archive_bytes // 1024} KiB")
    if not data.startswith(b"PK"):
        if len(data) > limits.msg_bytes:
            raise RepError(f"the reply packet is larger than {limits.msg_bytes // 1024} KiB")
        return data, None
    try:
        archive = open_archive(data, max_bytes=limits.archive_bytes, max_members=limits.members)
        msg = read_member(archive, f"{bbs_id}.MSG", limits.msg_bytes)
        if msg is None:
            others = [
                info.filename for info in archive.infolist() if info.filename.upper().endswith(".MSG")
            ]
            if others:
                raise RepError(
                    f"this reply packet holds {others[0]!r}, which is for another BBS; "
                    f"this BBS's replies are in {bbs_id}.MSG"
                )
            raise RepError(f"there is no {bbs_id}.MSG in the reply packet")
        todoor = read_member(archive, "TODOOR.EXT", 64 * 1024)
    except QwkFormatError as exc:
        raise RepError(str(exc)) from exc
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, OSError, zlib.error) as exc:
        # A damaged or encrypted member, or a compression method this
        # Python lacks.
        raise RepError(f"the reply packet could not be unpacked: {exc}") from exc
    return msg, todoor


def parse_rep(data: bytes, *, bbs_id: str, limits: RepLimits = DEFAULT_LIMITS) -> RepPacket:
    """Read a reply packet for the BBS `bbs_id`. Raises `RepError` when it
    cannot be read at all; a message that can't be used is a `RepProblem`
    and the rest are still read."""
    bbs_id = bbs_id.upper()
    if not valid_bbs_id(bbs_id):
        raise ValueError(f"{bbs_id!r} is not a QWK BBS ID")
    msg, todoor = _msg_bytes(data, bbs_id, limits)
    try:
        first, walked, stopped = parse_messages(msg, limit=limits.messages)
    except QwkFormatError as exc:
        raise RepError(str(exc)) from exc
    claimed = first[:8].decode("ascii", errors="replace").strip(" \x00").upper()
    if claimed != bbs_id:
        raise RepError(f"this reply packet is for {claimed or 'an unnamed BBS'!r}, not {bbs_id}")
    messages: list[RepMessage] = []
    problems: list[RepProblem] = []
    for index, (header, lines) in enumerate(walked, start=1):
        if not header.active:
            continue
        qwke = split_qwke(lines, {"to": header.to, "from": header.sender, "subject": header.subject})
        body = "\n".join(qwke.lines).rstrip("\n")
        if len(body.encode("utf-8")) > limits.body_bytes:
            problems.append(RepProblem(index, f"its text is longer than {limits.body_bytes} bytes"))
            continue
        messages.append(RepMessage(
            index=index,
            conference=_conference_of(header),
            to=qwke.values.get("to", header.to),
            subject=qwke.values.get("subject", header.subject),
            body=body,
            reference=header.reference or None,
            private=header.private,
            claimed_from=qwke.values.get("from", header.sender),
            when=header.when,
        ))
    problems += [RepProblem(None, reason) for reason in stopped]
    dropped, added = _todoor(from_cp437(todoor)) if todoor else ([], [])
    return RepPacket(messages=messages, problems=problems, dropped=dropped, added=added)


@dataclass(frozen=True)
class OutgoingReply:
    """A reply to write into a reply packet (`build_rep`)."""

    conference: int
    to: str
    sender: str
    subject: str
    body: str
    reference: int | None = None
    private: bool = False
    when: datetime.datetime | None = None


def build_msg(bbs_id: str, replies: list[OutgoingReply]) -> bytes:
    """A reply packet's .MSG: the BBS ID block, then each reply."""
    out = [bbs_id.upper().encode("ascii").ljust(BLOCK, b" ")]
    for reply in replies:
        lines = qwke_lines(to=reply.to, sender=reply.sender, subject=reply.subject)
        text = encode_text([*lines, *body_lines(reply.body)])
        out.append(pack_header(
            status="+" if reply.private else " ",
            number=str(reply.conference),
            when=reply.when or datetime.datetime(2000, 1, 1),
            to=reply.to, sender=reply.sender, subject=reply.subject,
            reference=reply.reference,
            blocks=1 + len(text) // BLOCK,
            conference=reply.conference,
        ))
        out.append(text)
    return b"".join(out)


def build_rep(bbs_id: str, replies: list[OutgoingReply], *, todoor: list[str] | None = None) -> bytes:
    """A reply packet's ZIP, as bytes."""
    if not valid_bbs_id(bbs_id.upper()):
        raise ValueError(f"{bbs_id!r} is not a QWK BBS ID")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{bbs_id.upper()}.MSG", build_msg(bbs_id, replies))
        if todoor:
            archive.writestr("TODOOR.EXT", "".join(f"{line}\r\n" for line in todoor))
    return buffer.getvalue()
