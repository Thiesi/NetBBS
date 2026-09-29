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
    MAX_FILE_REFS,
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
from netbbs.rendering.menu import menu_key
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


# -- attaching and fetching, for any writing that points at files ------------
#
# Mail has its own copies of these in `netbbs.net.mail_flow` (issue #830); a
# board post (issue #842) uses these. They name what is written by `noun`.

#: The review-screen keys that attach a file and take one off.
ATTACH_KEY = "a"
REMOVE_KEY = "r"


def file_actions(files: list[FileRef], *, noun: str) -> list[tuple[str, str, str | None]]:
    """The review screen's `[A]ttach file`, and `[R]emove file` once a file
    is attached, as `review_composition`'s `extra_actions`."""
    actions: list[tuple[str, str, str | None]] = [
        (ATTACH_KEY, menu_key("A", "ttach file"), f"Point the {noun} at a file in a file area"),
    ]
    if files:
        actions.append((REMOVE_KEY, menu_key("R", "emove file"), f"Take a file off the {noun}"))
    return actions


async def change_attached_files(
    session: Session, lane: DatabaseLane, user: User, files: list[FileRef], key: str, *,
    noun: str, breadcrumb: tuple[str, ...], style: dict,
) -> list[FileRef]:
    """`[A]ttach file` or `[R]emove file` on a review screen: the files
    afterwards, with what happened announced for the review screen."""
    if key == ATTACH_KEY:
        if len(files) >= MAX_FILE_REFS:
            announce(session, f"A {noun} can point at {MAX_FILE_REFS} files at most.", tone="error")
            return files
        ref = await choose_file_to_attach(session, lane, user, breadcrumb=breadcrumb, **style)
        if ref is None:
            return files
        if any(attached.file_id == ref.file_id for attached in files):
            announce(session, f"{sanitize_text(ref.filename)} is already attached.", tone="muted")
            return files
        announce(session, f"Attached {sanitize_text(ref.filename)}.", tone="muted")
        return [*files, ref]
    if not files:
        return files
    removed = files[0]
    if len(files) > 1:
        chosen = await pick_item(
            session, files,
            name_of=lambda item: item.filename,
            stable_id_of=lambda item: files.index(item),
            description_of=lambda item: f"in {item.area_name}",
            title="Remove which file?",
            breadcrumb=breadcrumb,
            empty_message="No files are attached.",
            **style,
        )
        if chosen is None:
            return files
        removed = chosen
    announce(session, f"Removed {sanitize_text(removed.filename)}.", tone="muted")
    return [ref for ref in files if ref is not removed]


async def get_referenced_file(
    session: Session, lane: DatabaseLane, user: User, refs: list[FileRef], *,
    noun: str, breadcrumb: tuple[str, ...], style: dict, transfers: TransferGrants | None,
) -> None:
    """`[G]et file`: download the file `refs` point at, or with several, the
    one the reader picks. Only a file they can open now is offered."""
    opened = await open_refs(lane, user, refs)
    available = [item.ref for item in opened if item.state == AVAILABLE]
    if not available:
        announce(session, f"None of the files in this {noun} is available to you.", tone="error")
        return
    ref = available[0]
    if len(available) > 1:
        chosen = await pick_item(
            session, available,
            name_of=lambda item: item.filename,
            stable_id_of=lambda item: available.index(item),
            description_of=lambda item: f"in {item.area_name}",
            title="Download which file?",
            breadcrumb=breadcrumb,
            empty_message=f"None of the files in this {noun} is available to you.",
            **style,
        )
        if chosen is None:
            return
        ref = chosen
    await download_ref(session, lane, user, ref, transfers=transfers)
