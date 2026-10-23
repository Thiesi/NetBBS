"""
Echo-list import: join many echoes at once from the hub's list (design doc
§6.8, issue #1465).

A hub publishes its echoes as a `.NA` list (echomail) and its file echoes
as a `.FA` list, one `TAG description` per line with `;` comments. The
SysOp reads one from the host, checks the tags they want, and picks a
Community; `import_echoes` then creates one board (or file area) per tag,
maps each to its echo, and queues the AreaFix (or FileFix) requests that
link them at the hub -- all of it, or nothing.

The file is bounded (`MAX_ECHOLIST_BYTES`, `MAX_ECHOLIST_LINES`). Tags are
normalised as a board's echo tag is; a line whose tag isn't valid, or that
repeats an earlier tag, is skipped and counted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import User
from netbbs.boards.boards import Board, BoardError, create_board, delete_board
from netbbs.files.areas import FileArea, FileAreaError, create_file_area, delete_file_area
from netbbs.ftn import networks as ftn_networks
from netbbs.ftn.areafix import AREAFIX, FILEFIX, AreaFixError, check_robot_ready, queue_robot_requests_without_commit
from netbbs.ftn.networks import FtnNetwork, FtnNetworkError, area_mappings, normalise_area_tag, set_board_area
from netbbs.ftn.queue import FtnQueueFullError
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database

MAX_ECHOLIST_BYTES = 1024 * 1024
MAX_ECHOLIST_LINES = 10_000
# A board or file area named from an echo's description is cut to this.
MAX_IMPORTED_NAME = 40
MAX_DESCRIPTION = 200

ECHOMAIL = "echomail"
FILE_ECHOES = "file"
KINDS = (ECHOMAIL, FILE_ECHOES)
KIND_LABELS = {ECHOMAIL: "echomail (.NA): boards", FILE_ECHOES: "file echoes (.FA): file areas"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class EchoListError(ValueError):
    """An echo list that can't be read or imported, with the reason."""


@dataclass(frozen=True)
class EchoListEntry:
    tag: str
    description: str


@dataclass(frozen=True)
class EchoList:
    entries: tuple[EchoListEntry, ...]
    skipped: int  # invalid or repeated lines, not comments or blank ones


def kind_for_path(path: Path) -> str:
    """`.FA` is a file-echo list; anything else is read as echomail."""
    return FILE_ECHOES if path.suffix.lower() == ".fa" else ECHOMAIL


def parse_echolist(data: bytes) -> EchoList:
    """The entries of a `.NA`/`.FA` list. UTF-8 if it decodes as such,
    else CP437, FTN's default."""
    if len(data) > MAX_ECHOLIST_BYTES:
        raise EchoListError(f"An echo list is at most {MAX_ECHOLIST_BYTES // 1024} KiB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp437")
    text = text.split("\x1a", 1)[0]  # DOS end-of-file
    lines = text.splitlines()
    if len(lines) > MAX_ECHOLIST_LINES:
        raise EchoListError(f"An echo list is at most {MAX_ECHOLIST_LINES:,} lines.")
    entries: list[EchoListEntry] = []
    seen: set[str] = set()
    skipped = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        tag_text, *rest = line.split(None, 1)
        try:
            tag = normalise_area_tag(tag_text)
        except FtnNetworkError:
            skipped += 1
            continue
        if tag.startswith("%") or tag in seen:  # a robot command, never an echo (issue #1459)
            skipped += 1
            continue
        seen.add(tag)
        entries.append(EchoListEntry(tag, clean_description(rest[0] if rest else "")))
    return EchoList(tuple(entries), skipped)


def clean_description(text: str) -> str:
    """A description as shown and stored: no control characters, spaces
    collapsed, at most `MAX_DESCRIPTION` characters."""
    return " ".join(_CONTROL.sub(" ", text).split())[:MAX_DESCRIPTION]


def imported_name(entry: EchoListEntry) -> str:
    """The name of the board or file area made for `entry`: its
    description cut to `MAX_IMPORTED_NAME` at a word where it can be, else
    the tag."""
    description = entry.description
    if len(description) <= MAX_IMPORTED_NAME:
        return description or entry.tag
    cut = description[:MAX_IMPORTED_NAME + 1].rsplit(" ", 1)[0][:MAX_IMPORTED_NAME].rstrip(" ,;:-(")
    return cut if len(cut) >= MAX_IMPORTED_NAME // 2 else description[:MAX_IMPORTED_NAME].rstrip()


# --- which tags a network already carries ----------------------------------


def mapped_tags(db: Database, network_id: int, kind: str) -> set[str]:
    """The tags of `kind` already mapped on the network, normalised."""
    if kind == ECHOMAIL:
        return {tag.upper() for tag in area_mappings(db, network_id)}
    mappings = getattr(ftn_networks, "file_area_tic_mappings", None)
    return {tag.upper() for tag in mappings(db, network_id)} if mappings is not None else set()


def file_echo_mapping_available() -> bool:
    """Whether this build can map a file area to a file echo (the TIC
    receiver's `set_file_area_tic`, issue #1462)."""
    return getattr(ftn_networks, "set_file_area_tic", None) is not None


# --- [S]ave ------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    created: tuple[str, ...]  # names of the boards or file areas made
    requests: int  # AreaFix or FileFix requests queued
    mapped: bool  # False only for file areas on a build that can't map them


def import_echoes(
    db: Database, network: FtnNetwork, actor: User, kind: str, entries: list[EchoListEntry],
    community_id: int | None,
) -> ImportResult:
    """Create, map and request every one of `entries`, or nothing.

    Everything that can be checked is checked first: the robot can be
    asked, no tag is mapped yet, every name is free, and the queue has
    room. A name already taken falls back to the tag. Creating a board
    commits, so if a later step fails anyway, what was created is removed
    again before the error is raised."""
    if kind not in KINDS:
        raise EchoListError("Choose echomail or file echoes.")
    if not entries:
        raise EchoListError("Check at least one echo to import.")
    robot = AREAFIX if kind == ECHOMAIL else FILEFIX
    try:
        check_robot_ready(network, robot)
    except AreaFixError as exc:
        raise EchoListError(str(exc)) from None
    already = mapped_tags(db, network.id, kind) & {entry.tag for entry in entries}
    if already:
        raise EchoListError(f"Already mapped on {network.name}: {', '.join(sorted(already))}.")
    names = _choose_names(db, kind, entries)
    mapper = getattr(ftn_networks, "set_file_area_tic", None) if kind == FILE_ECHOES else None
    created: list[Board | FileArea] = []
    try:
        for entry, name in zip(entries, names):
            description = entry.description or None
            if kind == ECHOMAIL:
                board = create_board(db, name, description=description, community_id=community_id, creator=actor)
                created.append(board)
                set_board_area(db, board, network.id, entry.tag)
            else:
                area = create_file_area(db, name, description=description, community_id=community_id,
                                        creator=actor)
                created.append(area)
                if mapper is not None:
                    mapper(db, area, network.id, entry.tag)
        requests = queue_robot_requests_without_commit(
            db, network, actor, robot, [f"+{entry.tag}" for entry in entries])
        record_action(
            db, actor=actor, action="import_ftn_echolist",
            detail=(f"{network.name}: {len(entries)} {'echoes' if kind == ECHOMAIL else 'file echoes'} "
                    f"({', '.join(entry.tag for entry in entries)}), {requests} request(s)"),
        )
    except (BoardError, FileAreaError, FtnNetworkError, AreaFixError, FtnQueueFullError) as exc:
        db.connection.rollback()
        _remove(db, created, actor)
        raise EchoListError(f"Nothing was imported: {exc}") from None
    except BaseException:
        db.connection.rollback()
        _remove(db, created, actor)
        raise
    return ImportResult(tuple(names), requests, mapped=kind == ECHOMAIL or mapper is not None)


def _choose_names(db: Database, kind: str, entries: list[EchoListEntry]) -> list[str]:
    table = "boards" if kind == ECHOMAIL else "file_areas"
    taken = {row[0].casefold() for row in db.connection.execute(f"SELECT name FROM {table}")}
    names = []
    for entry in entries:
        for candidate in dict.fromkeys((imported_name(entry), entry.tag)):
            if candidate.casefold() not in taken:
                break
        else:
            what = "board" if kind == ECHOMAIL else "file area"
            raise EchoListError(f"Nothing was imported: a {what} called {candidate!r} already exists, "
                                f"for {entry.tag}.")
        taken.add(candidate.casefold())
        names.append(candidate)
    return names


def _remove(db: Database, created: list, actor: User) -> None:
    for resource in reversed(created):
        if isinstance(resource, Board):
            delete_board(db, resource, deleted_by=actor)
        else:
            delete_file_area(db, resource, deleted_by=actor)
