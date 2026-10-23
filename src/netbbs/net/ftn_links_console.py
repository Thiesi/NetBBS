"""
The SysOp console's Links screen: one network's points and downlinks
(design doc §6.8, "Points and downlinks"; issues #1478 and #1479).

Reached with **[L]inks** on **Settings → Echomail & netmail (FTN)**, on the
highlighted network. The screen lists the links, each with its kind, how
its mail reaches it, its status, its last call and what waits for it.
Picking one edits it in a draft; **[C]reate** adds one, starting as the
next free point; **[D]elete** removes the highlighted one after a yes/no,
dropping what waits for it (its senders are told). Every screen follows
§3.5: content first, actions on the bar, a draft that persists nothing
before Save.

The hub's second part (issue #1479) adds:

- the AreaFix, FileFix and TIC password fields in the link's draft;
- **[A]reas** on the highlighted link: the echoes and file echoes it
  carries, where picking one switches it between read-write and
  read-only, **[A]dd** gives it any area this BBS carries and **[D]elete**
  takes one away;
- **[R]obot areas**: every area this BBS carries on the network, where
  picking one offers it to the links' AreaFix and FileFix requests, or
  stops offering it. (`O` is the picker's own [O]rder.)
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import parse_address
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.ftn.links import (
    ACTIVE,
    DISABLED,
    DOWNLINK,
    ECHO,
    FILE_ECHO,
    HOLD_LINK,
    KINDS,
    LINK_FLAVOURS,
    PAUSED,
    POINT,
    POLL_LINK,
    STATUSES,
    FtnLink,
    FtnLinkError,
    count_link_waiting,
    delete_link,
    link_areas,
    list_links,
    next_point_number,
    remove_link_area_without_commit,
    save_link,
    set_link_area_without_commit,
)
from netbbs.ftn.networks import BUNDLE_MODES, BUNDLE_OFF, BUNDLE_ZIP, FtnNetwork, get_network
from netbbs.moderation.log import record_action
from netbbs.net.admin_fields import _int_field, _optional_text_field
from netbbs.net.admin_ui import _announce_line, _load_chrome, edit_resource_draft
from netbbs.net.char_input import InputCancelled
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.location import located
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.picker import pick_item
from netbbs.net.resource_editor import (
    FieldSpec,
    choice_field,
    choice_step,
    inline_field,
    read_field_line,
    text_field,
    write_field_prompt,
)
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.rendering.layout import MenuEntry
from netbbs.rendering.menu import menu_key
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

KIND_LABELS = {POINT: "point", DOWNLINK: "downlink"}
FLAVOUR_LABELS = {HOLD_LINK: "hold: it calls in", POLL_LINK: "poll: this BBS calls it"}
STATUS_LABELS = {ACTIVE: "active", PAUSED: "paused (its own %PAUSE)", DISABLED: "disabled: served nothing"}
BUNDLE_LABELS = {BUNDLE_OFF: "off: bare packets", BUNDLE_ZIP: "ZIP"}


def link_description(db: Database, link: FtnLink) -> str:
    """The picker's line under a link: kind, calls, status, last call and
    what waits."""
    messages, files = count_link_waiting(db, link.id)
    calls = "polled" if link.polled else "calls in"
    parts = [KIND_LABELS[link.kind], calls]
    if link.status != ACTIVE:
        parts.append(link.status)
    last = (f"last call {link.last_call_at[:16].replace('T', ' ')}" if link.last_call_at else "never called")
    waiting = f"{messages} messages" + (f", {files} files" if files else "") + " waiting"
    # --- the hub's areas (issue #1479) ---
    echoes, file_echoes = len(link_areas(db, link.id, ECHO)), len(link_areas(db, link.id, FILE_ECHO))
    areas = f"; {echoes} echo{'' if echoes == 1 else 'es'}" + (
        f", {file_echoes} file echo{'' if file_echoes == 1 else 'es'}" if file_echoes else "")
    # --- end hub's areas ---
    return ", ".join(parts) + f"{areas}; {last}; {waiting}"


def links_summary(db: Database, network: FtnNetwork) -> str | None:
    """One line for the FTN status screen, or None without links."""
    links = list_links(db, network.id)
    if not links:
        return None
    points = sum(1 for link in links if link.kind == POINT)
    waiting = sum(count_link_waiting(db, link.id)[0] for link in links)
    return (f"{points} points, {len(links) - points} downlinks; {waiting} messages waiting for them"
            " (Settings → Echomail & netmail (FTN), [L]inks)")


@located("FTN links")
async def ftn_links_screen(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                           node_controls=None) -> None:
    """The network's points and downlinks; see the module docstring."""
    status: str | None = None

    async def _load() -> list[FtnLink]:
        return await lane.run(list_links, network.id)

    async def _create() -> FtnLink | None:
        return await edit_link(session, lane, actor, network, None, node_controls=node_controls)

    async def _delete(link: FtnLink) -> list[FtnLink] | None:
        nonlocal status
        messages, files = await lane.run(count_link_waiting, link.id)
        await session.write_line("")
        waiting = f" {messages} messages and {files} files waiting for it are dropped." if messages or files else ""
        if not await prompt_yes_no(session, f"Delete {link.label}?{waiting}", default=False):
            return None

        def _persist(db: Database) -> int:
            dropped = delete_link(db, link.id)
            record_action(db, actor=actor, action="delete_ftn_link",
                          detail=f"{network.name}: deleted {link.kind} {link.address.four_d}")
            return dropped

        dropped = await lane.run(_persist)
        status = f"Deleted {link.address.four_d}." + (f" {dropped} waiting items dropped." if dropped else "")
        return await _load()

    # --- the hub's areas (issue #1479) ---
    async def _areas(link: FtnLink) -> list[FtnLink]:
        await link_areas_screen(session, lane, actor, network, link)
        return await _refresh()

    async def _offered() -> list[FtnLink]:
        await offered_areas_screen(session, lane, actor, network)
        return await _refresh()
    # --- end hub's areas ---

    descriptions: dict[int, str] = {}

    async def _refresh() -> list[FtnLink]:
        links = await _load()
        descriptions.clear()
        for link in links:
            descriptions[link.id] = await lane.run(link_description, link)
        return links

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        links = await _refresh()
        picked = await pick_item(
            session, links,
            name_of=lambda link: sanitize_text(link.label),
            stable_id_of=lambda link: link.id,
            description_of=lambda link: descriptions.get(link.id, ""),
            title=f"Links ({network.name})", own=True,
            empty_message=f"No points or downlinks. [C]reate one: it starts as point "
                          f"{network.our_address.four_d}.1.",
            refresh=_refresh, on_create=_create, item_keys={"d": _delete, "a": _areas},
            live_keys={"r": _offered},
            live_nav=[MenuEntry(label=menu_key("D", "elete"), brief="Remove a point or downlink"),
                      MenuEntry(label=menu_key("A", "reas"), brief="What the highlighted link carries"),
                      MenuEntry(label=menu_key("R", "obot areas"), brief="What AreaFix and FileFix offer links")],
            live_label=lambda: status or "Points and downlinks this BBS serves on this network",
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await edit_link(session, lane, actor, network, picked, node_controls=node_controls)


def _secret(key: str, label: str):

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await write_field_prompt(session, colored(f"{label} (typed unseen; Enter on an empty line clears it):",
                                                  fg_color=MUTED_COLOR))
        try:
            value = await read_field_line(session, initial="", echo=False)
        except InputCancelled:
            return
        draft[key] = value.strip()

    return prompt


def _link_draft(network: FtnNetwork, link: FtnLink | None, next_point: int) -> dict:
    if link is None:
        return {"kind": POINT, "address": f"{network.our_address.four_d}.{next_point}", "name": "",
                "status": ACTIVE, "session_password": "", "packet_password": "", "flavour": HOLD_LINK,
                "host": "", "port": DEFAULT_PORT, "bundle": BUNDLE_OFF,
                "areafix_password": "", "filefix_password": "", "tic_password": ""}
    return {"kind": link.kind, "address": link.address.four_d, "name": link.name, "status": link.status,
            "session_password": link.session_password, "packet_password": link.packet_password,
            "flavour": link.flavour, "host": link.host, "port": link.port, "bundle": link.bundle,
            # --- robot and TIC passwords (issue #1479) ---
            "areafix_password": link.areafix_password, "filefix_password": link.filefix_password,
            "tic_password": link.tic_password}


def _link_from_draft(network: FtnNetwork, draft: dict, existing: FtnLink | None) -> FtnLink:
    try:
        address = parse_address(draft["address"])
    except FtnFormatError as exc:
        raise FtnLinkError(f"{exc}.") from None
    base = existing or FtnLink(network_id=network.id, kind=POINT, address=address, session_password="")
    return FtnLink(
        id=base.id, network_id=network.id, kind=draft["kind"], address=address, name=draft["name"],
        session_password=draft["session_password"], packet_password=draft["packet_password"],
        # --- robot and TIC passwords (issue #1479) ---
        areafix_password=draft["areafix_password"], filefix_password=draft["filefix_password"],
        tic_password=draft["tic_password"],
        flavour=draft["flavour"], host=draft["host"], port=int(draft["port"]), bundle=draft["bundle"],
        status=draft["status"],
    )


def link_fields() -> list[FieldSpec]:
    secret = lambda key: (lambda d: "set" if d[key] else "(not set)")  # noqa: E731
    kinds, flavours, statuses, bundles = list(KINDS), list(LINK_FLAVOURS), list(STATUSES), list(BUNDLE_MODES)
    return [
        FieldSpec(key="kind", label="Kind", render=lambda d: KIND_LABELS[d["kind"]],
                  prompt=choice_field("kind", kinds), step=choice_step("kind", kinds), section="Link",
                  brief="A point of this BBS, or a node it feeds",
                  help="A point has this BBS's address with a point number (.1, .2, ...) and calls in for its "
                       "mail. A downlink is a node this BBS feeds; its own points are reached through it."),
        FieldSpec(key="address", label="Address", render=lambda d: d["address"] or "(not set)",
                  prompt=text_field("address", required=True), section="Link",
                  brief="zone:net/node.point for a point, zone:net/node for a downlink"),
        FieldSpec(key="name", label="Name", render=lambda d: d["name"] or "(none)",
                  prompt=_optional_text_field("name"), section="Link",
                  brief="Who runs it, or the system's name, for this list"),
        FieldSpec(key="status", label="Status", render=lambda d: STATUS_LABELS[d["status"]],
                  prompt=choice_field("status", statuses), step=choice_step("status", statuses), section="Link",
                  brief="Disabled: it must still log in, but gets nothing",
                  help="Active serves it. Paused stops its echomail and file echoes but not its netmail; it "
                       "is set by the link itself through AreaFix. Disabled keeps its address safe -- a caller "
                       "claiming it must still know its password -- but hands it nothing and holds what it sends."),
        FieldSpec(key="session_password", label="Session password", render=secret("session_password"),
                  prompt=_secret("session_password", "Session password"), section="Passwords",
                  brief="Required: it proves the link when it calls",
                  help="Agree it with the point's or downlink's SysOp. Only a call that proves it is handed the "
                       "link's mail; a wrong one ends the call."),
        FieldSpec(key="packet_password", label="Packet password", render=secret("packet_password"),
                  prompt=_secret("packet_password", "Packet password"), section="Passwords",
                  brief="Up to 8 characters, if the link uses one"),
        # --- robot and TIC passwords (issue #1479) ---
        FieldSpec(key="areafix_password", label="AreaFix password", render=secret("areafix_password"),
                  prompt=_secret("areafix_password", "AreaFix password"), section="Passwords",
                  brief="What its AreaFix requests carry; none: AreaFix doesn't act for it",
                  help="The link writes it as the subject of its netmail to this BBS's AreaFix. It can change it "
                       "itself with %PWD."),
        FieldSpec(key="filefix_password", label="FileFix password",
                  render=lambda d: "set" if d["filefix_password"] else (
                      "(the AreaFix password)" if d["areafix_password"] else "(not set)"),
                  prompt=_secret("filefix_password", "FileFix password"), section="Passwords",
                  brief="What its FileFix requests carry; empty uses the AreaFix password"),
        FieldSpec(key="tic_password", label="TIC password", render=secret("tic_password"),
                  prompt=_secret("tic_password", "TIC password"), section="Passwords",
                  brief="The Pw of TICs both ways; none: no file echoes to or from it",
                  help="A TIC from the link must carry it, and every TIC this BBS sends the link carries it. "
                       "Without one, the link's TICs are held and it is sent no file echoes."),
        # --- end robot and TIC passwords ---
        FieldSpec(key="flavour", label="Calls", render=lambda d: FLAVOUR_LABELS[d["flavour"]],
                  prompt=choice_field("flavour", flavours), step=choice_step("flavour", flavours), section="Calls",
                  brief="Hold until it calls, or call it when mail waits",
                  help="A point always calls in. A downlink with a host can be polled: this BBS calls it when "
                       "mail waits for it."),
        FieldSpec(key="host", label="Host",
                  render=lambda d: d["host"] or ("(points call in)" if d["kind"] == POINT else "(not set)"),
                  prompt=_optional_text_field("host"), section="Calls",
                  brief="Where a polled downlink answers BinkP"),
        FieldSpec(key="port", label="Port", render=lambda d: str(d["port"]), prompt=_int_field("port", "Port"),
                  section="Calls", brief="Usually 24554"),
        FieldSpec(key="bundle", label="Bundle mail", render=lambda d: BUNDLE_LABELS[d["bundle"]],
                  prompt=choice_field("bundle", bundles), step=choice_step("bundle", bundles), section="Calls",
                  brief="Send its packets bare or in a ZIP bundle"),
    ]


async def edit_link(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                    existing: FtnLink | None, *, node_controls=None) -> FtnLink | None:
    """One point or downlink in a draft; nothing is stored before [S]ave."""
    fresh = await lane.run(get_network, network.id) or network
    draft = _link_draft(fresh, existing, await lane.run(next_point_number, fresh))

    async def save(draft: dict) -> FtnLink:
        candidate = _link_from_draft(fresh, draft, existing)

        def _persist(db: Database) -> FtnLink:
            saved = save_link(db, candidate)
            record_action(db, actor=actor, action="set_ftn_link",
                          detail=(f"{fresh.name}: {saved.kind} {saved.address.four_d} calls={saved.flavour} "
                                  f"status={saved.status}"))
            return saved

        return await lane.run(_persist)

    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=existing.address.four_d if existing is not None else f"New link on {fresh.name}",
        fields=link_fields(), draft=draft, save=save, error_type=FtnLinkError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=("A new link starts as the next free point. Agree its session password with its SysOp."
                  if existing is None else None),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, f"Saved {saved.address.four_d}. Its mail waits for a call that proves its password.")
        mailer = getattr(node_controls, "ftn_mailer", None) if node_controls is not None else None
        if mailer is not None:
            mailer.kick()
    return saved


# --- the hub's areas (issue #1479) ------------------------------------------------------


@dataclass(frozen=True)
class AreaRow:
    kind: str  # ECHO or FILE_ECHO
    tag: str
    name: str
    mode: str = "rw"  # a link's areas: "rw" or "read"
    offered: bool = False  # the offered-areas screen

    @property
    def stable_id(self) -> int:
        return zlib.crc32(f"{self.kind}:{self.tag}".encode("utf-8")) & 0x7FFFFFFF

    @property
    def kind_label(self) -> str:
        return "echo" if self.kind == ECHO else "file echo"


def _carried(db: Database, network: FtnNetwork) -> list[AreaRow]:
    from netbbs.ftn.robot_server import carried_areas, offered_tags

    rows = []
    for kind in (ECHO, FILE_ECHO):
        offered = offered_tags(db, network.id, kind)
        rows += [AreaRow(kind, tag, name, offered=tag in offered) for tag, name in carried_areas(db, network.id, kind)]
    return rows


def link_area_rows(db: Database, network: FtnNetwork, link: FtnLink) -> list[AreaRow]:
    """The areas a link carries, echoes first, with the names of the board
    or file area carrying each here."""
    names = {(row.kind, row.tag): row.name for row in _carried(db, network)}
    return [AreaRow(kind, area.tag.upper(), names.get((kind, area.tag.upper()), "(no longer carried here)"),
                    mode=area.mode)
            for kind in (ECHO, FILE_ECHO) for area in link_areas(db, link.id, kind)]


@located("FTN link areas")
async def link_areas_screen(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                            link: FtnLink) -> None:
    """What one link carries; see the module docstring."""
    status: str | None = None

    async def _load() -> list[AreaRow]:
        return await lane.run(link_area_rows, network, link)

    async def _add() -> list[AreaRow] | None:
        nonlocal status
        carried = await lane.run(_carried, network)
        have = {(row.kind, row.tag) for row in await _load()}
        choices = [row for row in carried if (row.kind, row.tag) not in have]
        if not choices:
            status = "It carries every area this BBS carries on the network."
            return await _load()
        chosen = await pick_item(
            session, choices, name_of=lambda row: sanitize_text(row.tag), stable_id_of=lambda row: row.stable_id,
            description_of=lambda row: f"{row.kind_label}: {sanitize_text(row.name)}",
            title=f"Add an area for {link.address.four_d}", own=True, empty_message="Nothing to add.",
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if chosen is None:
            return await _load()

        def _persist(db: Database) -> None:
            set_link_area_without_commit(db, link.id, chosen.kind, chosen.tag, "rw")
            db.connection.commit()
            record_action(db, actor=actor, action="set_ftn_link_area",
                          detail=f"{network.name}: {link.address.four_d} carries {chosen.kind_label} {chosen.tag}")

        await lane.run(_persist)
        status = f"{link.address.four_d} carries {chosen.tag} now, read-write."
        return await _load()

    async def _remove(row: AreaRow) -> list[AreaRow]:
        nonlocal status

        def _persist(db: Database) -> None:
            remove_link_area_without_commit(db, link.id, row.kind, row.tag)
            db.connection.commit()
            record_action(db, actor=actor, action="remove_ftn_link_area",
                          detail=f"{network.name}: {link.address.four_d} no longer carries {row.kind_label} {row.tag}")

        await lane.run(_persist)
        status = f"{link.address.four_d} no longer carries {row.tag}."
        return await _load()

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        rows = await _load()
        picked = await pick_item(
            session, rows,
            name_of=lambda row: sanitize_text(row.tag), stable_id_of=lambda row: row.stable_id,
            description_of=lambda row: (f"{row.kind_label}, {'read-write' if row.mode == 'rw' else 'read-only'}: "
                                        f"{sanitize_text(row.name)}"),
            title=f"Areas of {sanitize_text(link.label)}", own=True,
            empty_message="It carries nothing yet. [A]dd an area, or offer areas so it can link them by AreaFix.",
            refresh=_load, live_keys={"a": _add}, item_keys={"d": _remove},
            live_nav=[MenuEntry(label=menu_key("A", "dd"), brief="Give it an area this BBS carries"),
                      MenuEntry(label=menu_key("D", "elete"), brief="Take the highlighted area away")],
            live_label=lambda: status or "Pick an area to switch it between read-write and read-only",
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.stable_id
        mode = "read" if picked.mode == "rw" else "rw"

        def _toggle(db: Database, row: AreaRow = picked, mode: str = mode) -> None:
            set_link_area_without_commit(db, link.id, row.kind, row.tag, mode)
            db.connection.commit()
            record_action(db, actor=actor, action="set_ftn_link_area",
                          detail=f"{network.name}: {link.address.four_d} carries {row.kind_label} {row.tag} "
                                 f"{'read-only' if mode == 'read' else 'read-write'}")

        await lane.run(_toggle)
        status = f"{picked.tag} is {'read-only' if mode == 'read' else 'read-write'} for {link.address.four_d}."


@located("FTN offered areas")
async def offered_areas_screen(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    """The areas this BBS offers to its links' AreaFix and FileFix requests;
    picking one offers it or stops offering it."""
    from netbbs.ftn.robot_server import set_offered

    status: str | None = None

    async def _load() -> list[AreaRow]:
        return await lane.run(_carried, network)

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        rows = await _load()
        picked = await pick_item(
            session, rows,
            name_of=lambda row: sanitize_text(row.tag), stable_id_of=lambda row: row.stable_id,
            description_of=lambda row: (f"{row.kind_label}, {'offered to links' if row.offered else 'not offered'}: "
                                        f"{sanitize_text(row.name)}"),
            title=f"Robot areas ({network.name})", own=True,
            empty_message="This BBS carries no echo or file echo on the network yet: map a board or file area first.",
            refresh=_load,
            live_label=lambda: status or "Pick an area to offer it to points and downlinks, or stop offering it",
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.stable_id
        offered = not picked.offered

        def _persist(db: Database, row: AreaRow = picked, offered: bool = offered) -> None:
            set_offered(db, network.id, row.kind, row.tag, offered)
            record_action(db, actor=actor, action="set_ftn_link_offer",
                          detail=f"{network.name}: {row.kind_label} {row.tag} "
                                 f"{'offered to links' if offered else 'no longer offered'}")

        await lane.run(_persist)
        status = (f"{picked.tag} is offered to links: their AreaFix or FileFix can link it." if offered
                  else f"{picked.tag} is no longer offered; links that carry it keep it.")
