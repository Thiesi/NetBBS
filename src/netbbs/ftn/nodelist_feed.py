"""
The nodelist from a file echo, and from a file on the host (design doc
§6.8 "Nodelist"; §16 "Issue #1154", Decision 5; issue #1464).

Each network may name its nodelist echo (`NIX_LIST`), the full list's base
name (`NIXLIST`) and, optionally, its diffs' base name (`NODEDIFF`). A file
that arrives by TIC in that echo and passed the TIC checks is handed to
`handle_received_file`, which decides by its name:

- `<list name>.<ext>` is a full list, `<diff name>.<ext>` a NODEDIFF, where
  `<ext>` is the day number (`289`), an archive's letter and the day's last
  two digits (`Z89`, FTS-5000 §4), or `ZIP`;
- any other name is not the nodelist's, and is left to the caller.

What the file *is* is decided by content, not by the extension: a ZIP is
opened in-process and its member `<name>.<ddd>` read, within
`MAX_NODELIST_BYTES`; the other archive kinds (ARC, ARJ, LHA, RAR, 7-Zip)
are opened the same way through the sandboxed external tools mail bundles
use (`netbbs.files.extract`, issue #1477), and are refused with the tool
to install when none is; anything else is taken as the text itself.

A full list is imported once its CRC-16 matches (`nodelist.import_list`); a
diff is applied to the kept raw list and the result's CRC-16 checked
(`nodelist.apply_diff`). Whatever fails changes nothing: the stored list
stays in use, the file is held with the reason, the failure is noted for
the status screen, and a diff that failed leaves the network waiting for a
full list -- the fall-back.

`read_list_file` is the by-hand path's reader, shared by the console's
[N]odelist import and `ftn-import-nodelist`: the same archive rules, for a
full list only, with the CRC-16 checked when the first line carries one.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
import zlib
from pathlib import Path

from netbbs.files.extract import KIND_LABELS, ArchiveError, Member, run_blocking, unpack_with_tools
from netbbs.ftn.bundle import MAX_MEMBERS, TOOL_BUDGET_SECONDS, TOOL_KINDS, archive_kind
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.nodelist import (
    HOW_FULL,
    MAX_NODELIST_BYTES,
    ImportOutcome,
    NodelistError,
    apply_diff,
    import_list,
    record_failure,
)
from netbbs.ftn.queue import hold_inbound
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

FULL, DIFF = "full", "diff"
_EXTENSION = re.compile(r"\d{3}|[A-Za-z]\d{2}|[Zz][Ii][Pp]")
_MEMBER_DAY = re.compile(r"\d{3}")
_CHUNK = 64 * 1024
_MIB = MAX_NODELIST_BYTES // (1024 * 1024)


class _NoMember(NodelistError):
    """The ZIP holds no member of the wanted name."""


def classify(network: FtnNetwork, file_name: str) -> str | None:
    """`FULL`, `DIFF`, or None when `file_name` is neither of the network's
    nodelist names."""
    stem, dot, extension = file_name.rpartition(".")
    if not dot or not _EXTENSION.fullmatch(extension):
        return None
    stem = stem.upper()
    if network.nodelist_name and stem == network.nodelist_name.upper():
        return FULL
    if network.nodediff_name and stem == network.nodediff_name.upper():
        return DIFF
    return None


def unpack(data: bytes, base_name: str | None) -> tuple[str | None, bytes]:
    """The text of a list or diff: `data` itself, or the `<base_name>.<ddd>`
    member of a ZIP (any `*.<ddd>` member when `base_name` is None, if
    there is exactly one). Returns `(member name or None, text)`. Raises
    `NodelistError` for a damaged archive, one no installed tool opens, a
    missing member, or one over the limit."""
    kind = archive_kind(data)
    if kind in TOOL_KINDS:
        return _unpack_with_tools(data, kind, base_name)
    if kind != "zip":
        return None, data
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise NodelistError(f"damaged ZIP: {exc}") from None
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_MEMBERS:
            raise NodelistError(f"the ZIP holds {len(members)} files, more than {MAX_MEMBERS}")
        candidates = []
        for info in members:
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            stem, dot, extension = name.rpartition(".")
            if dot and _MEMBER_DAY.fullmatch(extension) and (base_name is None or stem.upper() == base_name.upper()):
                candidates.append((name, info))
        if not candidates:
            wanted = f"{base_name}.nnn" if base_name else "a list named NAME.nnn"
            raise _NoMember(f"the ZIP holds no {wanted}")
        if base_name is None and len(candidates) > 1:
            raise NodelistError("the ZIP holds more than one list: "
                                + ", ".join(name for name, _ in candidates))
        name, info = candidates[0]
        content = bytearray()
        try:
            with archive.open(info) as member:
                while chunk := member.read(_CHUNK):
                    content += chunk
                    if len(content) > MAX_NODELIST_BYTES:
                        raise NodelistError(f"{name} unpacks to more than {_MIB} MiB")
        # RuntimeError: an encrypted member; zlib.error: a broken deflate stream.
        except (zipfile.BadZipFile, RuntimeError, zlib.error, OSError, EOFError) as exc:
            raise NodelistError(f"cannot unpack {name}: {exc}") from None
        return name, bytes(content)


def _wanted(name: str, base_name: str | None) -> bool:
    stem, dot, extension = name.rpartition(".")
    return bool(dot and _MEMBER_DAY.fullmatch(extension) and (base_name is None or stem.upper() == base_name.upper()))


def _unpack_with_tools(data: bytes, kind: str, base_name: str | None) -> tuple[str | None, bytes]:
    """`unpack` for LHA, ARC, 7-Zip, RAR and ARJ (issue #1477), through the
    sandboxed external tools. Blocking: it runs on the database lane or in a
    worker thread, never on the event loop."""
    label = KIND_LABELS[kind]
    candidates: list[str] = []

    def choose(members: list[Member]) -> list[Member]:
        found = [member for member in members if _wanted(member.name, base_name)]
        candidates.extend(member.name for member in found)
        return found[:1] if len(found) == 1 or base_name is not None else []

    try:
        read = run_blocking(lambda: unpack_with_tools(
            data, kind, choose=choose, max_members=MAX_MEMBERS, max_bytes=MAX_NODELIST_BYTES,
            budget_seconds=TOOL_BUDGET_SECONDS, label="FTN nodelist"))
    except ArchiveError as exc:
        raise NodelistError(f"the {label} archive can't be opened: {exc}") from None
    if not candidates:
        wanted = f"{base_name}.nnn" if base_name else "a list named NAME.nnn"
        raise _NoMember(f"the {label} archive holds no {wanted}")
    if not read:
        raise NodelistError(f"the {label} archive holds more than one list: " + ", ".join(candidates))
    member, content = read[0]
    return member.name, content


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_NODELIST_BYTES + 1)
    if len(data) > MAX_NODELIST_BYTES:
        raise NodelistError(f"{path.name} is over {_MIB} MiB")
    return data


def handle_received_file(db: Database, network: FtnNetwork, area_tag: str, file_name: str, path: Path) -> bool:
    """Import a file that arrived by TIC in `area_tag`, if it is the
    network's nodelist or NODEDIFF. True when it was one and was handled --
    imported, or refused, held with the reason and logged; False when it is
    not the network's nodelist echo or not one of its names, and the caller
    stores or holds it as any other file."""
    if not network.nodelist_echo or area_tag.strip().upper() != network.nodelist_echo.upper():
        return False
    kind = classify(network, file_name)
    if kind is None:
        return False
    data = b""
    try:
        data = _read_bounded(path)
        member, text = unpack(data, network.nodelist_name if kind == FULL else network.nodediff_name)
        if kind == FULL:
            outcome = import_list(db, network.id, text, how=HOW_FULL, require_crc=True)
        else:
            outcome = apply_diff(db, network.id, text)
    except (NodelistError, OSError) as exc:
        _refuse(db, network, kind, file_name, data, str(exc))
        return True
    shown = file_name + (f" ({member})" if member else "")
    if outcome is None:
        _logger.info("FTN %s: nodediff %s skipped: the nodelist is already the one it makes", network.name, shown)
    else:
        _logger.info("FTN %s: nodelist %s %s: day %s, %d nodes, CRC-16 %05d verified", network.name, shown,
                     "imported" if kind == FULL else "applied", _day(outcome), outcome.nodes, outcome.crc or 0)
    return True


def _day(outcome: ImportOutcome) -> str:
    return f"{outcome.day:03d}" if outcome.day is not None else "?"


def _refuse(db: Database, network: FtnNetwork, kind: str, file_name: str, data: bytes, reason: str) -> None:
    label = "nodelist" if kind == FULL else "nodediff"
    full_reason = f"{label} {file_name} not {'imported' if kind == FULL else 'applied'}: {reason}"
    if kind == DIFF:
        full_reason += "; waiting for a full list"
    record_failure(db, network.id, full_reason, waiting_for_full=kind == DIFF)
    _logger.warning("FTN %s: %s", network.name, full_reason)
    if not data:  # unreadable, or over the limit: TIC holds a file over 64 MiB before it gets here
        return
    if not hold_inbound(db, network_id=network.id, remote_address=str(network.uplink_address),
                        file_name=file_name, content=data, reason=full_reason):
        _logger.error("FTN %s: %s could not be held (held store full)", network.name, file_name)


def read_list_file(path: Path, network: FtnNetwork) -> bytes:
    """The list text in a file given by hand: the file itself, or the list
    in a ZIP (the network's `<list name>.nnn`, or the one list it holds).
    Raises `NodelistError` or `OSError`. Blocking: run it off the event
    loop."""
    data = _read_bounded(path)
    if network.nodelist_name:
        try:
            return unpack(data, network.nodelist_name)[1]
        except _NoMember:
            pass
    return unpack(data, None)[1]
