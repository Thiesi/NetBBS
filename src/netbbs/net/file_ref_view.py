"""
Files a letter points at, on screen (issue #830).

The screen half of `netbbs.file_refs`: the rows that list a letter's files
under its header, the picker that chooses a file to attach, and the
download a reader starts from the letter. Written for mail and meant for
anything else that points at file-area files -- a board post (issue #842
F086) -- so nothing here knows what a letter is.

The download is the file areas' own (`netbbs.net.file_flow.
send_file_to_caller`): Zmodem where the terminal can carry it, else a
browser link, and a browser link when a Zmodem send fails. It is started
only for a file the reader can open now (`netbbs.file_refs.open_ref`),
checked again as the key is pressed.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.file_refs import (
    AVAILABLE,
    GONE,
    FileRef,
    OpenedRef,
    attachable_files,
    file_size_text,
    open_ref,
    ref_for_entry,
)
from netbbs.files.areas import FileArea
from netbbs.files.entries import FileEntry
from netbbs.net.file_flow import offer_grant, send_file_to_caller, supports_zmodem, visible_areas, what_of
from netbbs.net.file_transfer import DOWNLOAD, TransferGrants
from netbbs.net.notices import announce
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.rendering import LABEL_COLOR, METADATA_COLOR, MUTED_COLOR, colored, sanitize_text
from netbbs.storage.execution import DatabaseLane

#: What a reader who may not read a file's area is shown for it. Neither the
#: file's name nor the area's: the area is closed to them.
NO_ACCESS_TEXT = "A file in a file area you can't open"


def ref_rows(opened: list[OpenedRef], *, accent: int) -> list[str]:
    """The rows naming `opened` under a letter's (or post's) header: a
    "Files:" label, then one row per file -- its name, size and area, or
    that it is no longer available, or `NO_ACCESS_TEXT`. Empty when there
    are none."""
    if not opened:
        return []
    rows = [colored("Files:", fg_color=LABEL_COLOR)]
    for number, item in enumerate(opened, start=1):
        lead = f"  {number}. "
        if item.state == AVAILABLE:
            rows.append(
                lead + colored(sanitize_text(item.ref.filename), fg_color=accent)
                + colored(
                    f"  {file_size_text(item.ref.size_bytes)} in {sanitize_text(item.ref.area_name)}",
                    fg_color=METADATA_COLOR,
                )
            )
        elif item.state == GONE:
            rows.append(
                lead + colored(f"{sanitize_text(item.ref.filename)} -- no longer available", fg_color=MUTED_COLOR)
            )
        else:
            rows.append(lead + colored(NO_ACCESS_TEXT, fg_color=MUTED_COLOR))
    return rows


def attached_rows(refs: list[FileRef], *, accent: int) -> list[str]:
    """The rows a compose screen shows for the files attached so far."""
    return ref_rows([OpenedRef(ref, AVAILABLE) for ref in refs], accent=accent)


async def open_refs(lane: DatabaseLane, user: User, refs: list[FileRef]) -> list[OpenedRef]:
    return await lane.run(lambda db: [open_ref(db, user, ref) for ref in refs])


async def choose_file_to_attach(
    session: Session, lane: DatabaseLane, user: User, *, breadcrumb: tuple[str, ...], **style,
) -> FileRef | None:
    """Pick a file area, then a file in it, to point a letter at: the
    areas `user` may read, and the files in each they can download. `None`
    if they leave without choosing. `style` is `pick_item`'s presentation
    arguments."""
    areas = await lane.run(lambda db: visible_areas(db, user))
    area_start: int | None = None
    while True:
        area: FileArea | None = await pick_item(
            session, areas,
            name_of=lambda item: item.name,
            stable_id_of=lambda item: item.id,
            description_of=lambda item: item.description,
            title="Attach a file: choose its file area",
            breadcrumb=breadcrumb,
            empty_message="There are no file areas you can open.",
            start_stable_id=area_start,
            **style,
        )
        if area is None:
            return None
        area_start = area.id
        chosen = area
        entries = await lane.run(lambda db: attachable_files(db, user, chosen))
        if not entries:
            announce(session, f"{area.name} has no files to attach.", tone="muted")
            continue
        entry: FileEntry | None = await pick_item(
            session, entries,
            name_of=lambda item: item.filename,
            stable_id_of=lambda item: item.id,
            description_of=lambda item: f"{file_size_text(item.size_bytes)}"
            + (f" -- {item.description.splitlines()[0]}" if item.description else ""),
            title=f"Attach a file from {sanitize_text(area.name)}",
            breadcrumb=breadcrumb,
            empty_message=f"{area.name} has no files to attach.",
            **style,
        )
        if entry is None:
            continue
        return ref_for_entry(entry, area)


async def download_ref(
    session: Session, lane: DatabaseLane, user: User, ref: FileRef, *, transfers: TransferGrants | None,
) -> None:
    """Download `ref` for `user` with the file areas' own download, after
    checking again that they can open it now."""
    opened = await lane.run(lambda db: open_ref(db, user, ref))
    if opened.state != AVAILABLE:
        text = (
            f"{ref.filename} is no longer available." if opened.state == GONE
            else "That file is in a file area you can't open."
        )
        announce(session, text, tone="error")
        return
    assert opened.entry is not None and opened.area is not None
    area, entry = opened.area, opened.entry
    failed = await send_file_to_caller(session, lane, area, entry, user, transfers=transfers)
    if failed and transfers is not None and supports_zmodem(session):
        # The Zmodem send did not work; most terminals have no Zmodem at
        # all. A browser link is the way that does (issue #842).
        await offer_grant(
            session, transfers,
            mint=lambda: transfers.issue(direction=DOWNLOAD, user=user, area=area, file_id=entry.file_id),
            direction=DOWNLOAD, what=what_of(DOWNLOAD, area, entry), filename=entry.filename,
        )
