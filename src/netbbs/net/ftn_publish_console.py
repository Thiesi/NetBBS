"""
The SysOp console's Publishing screen: one network's pointlist and nodelist
segment (design doc §6.8, "Publishing"; issue #1480).

Reached with **[P]ublishing** on **Settings → Echomail & netmail (FTN)**,
on the highlighted network. The screen shows each list -- on or off, its
file names, the file echo it is hatched into, and its last edition (day,
CRC-16, file) -- then the folder, the schedule and the last failure.

- **[P]ublish now** writes today's edition of every list that is on, and
  hatches it where a file echo is set: that is the one yes/no, since it
  goes to the network.
- **[S]ettings** is a draft: which lists are on, their names and file
  echoes, the segment's Host line, the folder, ZIP and the schedule.
- **[E]ntries for points** lists this node's points; picking one edits its pointlist
  line in a draft (listed or not, system name, location, sysop, phone,
  speed, flags).
- **[N]odes** is the segment's node lines: picking one edits it,
  **[C]reate** adds one (starting from the first downlink in this net not
  listed yet), **[D]elete** removes one after a yes/no.

Every screen follows §3.5: content first, actions on the bar, drafts that
persist nothing before Save.
"""

from __future__ import annotations

from dataclasses import replace

from netbbs.auth.users import User
from netbbs.ftn.links import DISABLED
from netbbs.ftn.networks import FtnNetwork, get_network
from netbbs.ftn.publish import (
    LIST_KINDS,
    LIST_LABELS,
    NODE_KEYWORDS,
    POINTLIST,
    SCHEDULE_OFF,
    SCHEDULE_WEEKLY,
    SCHEDULES,
    SEGMENT,
    WEEKDAYS,
    ListFields,
    PointEntry,
    PublishError,
    PublishSettings,
    SegmentNode,
    data_line,
    clean_fields,
    delete_segment_node,
    edition_names,
    get_settings,
    list_name,
    list_point_entries,
    list_segment_nodes,
    local_today,
    point_defaults,
    point_line,
    publish_folder,
    publish_now,
    save_point_entry,
    save_segment_node,
    save_settings,
    segment_line,
    suggested_node,
)
from netbbs.moderation.log import change_detail, record_action_without_commit
from netbbs.net.admin_fields import _int_field, _optional_text_field
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce_line,
    _detail_title,
    _load_chrome,
    edit_resource_draft,
    show_detail,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.location import located
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.picker import pick_item
from netbbs.net.resource_editor import FieldSpec, bool_field, bool_step, choice_field, choice_step
from netbbs.rendering import ERROR_COLOR, colored
from netbbs.rendering.detail import Field, Note, Section
from netbbs.rendering.layout import MenuEntry
from netbbs.rendering.menu import menu_key
from netbbs.rendering.sanitize import sanitize_text
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

SCHEDULE_LABELS = {SCHEDULE_OFF: "off: publish by hand", "daily": "daily", SCHEDULE_WEEKLY: "weekly"}
KEYWORD_LABELS = {"": "node", "Pvt": "Pvt: private, routed via the host", "Hold": "Hold: not taking calls now",
                  "Down": "Down: not operating"}


# --- the screen ---------------------------------------------------------------------------


def _last_text(settings: PublishSettings, kind: str) -> str:
    at, day, crc, name = settings.last(kind)
    if not at:
        return "never"
    return f"{at[:16].replace('T', ' ')} UTC: {name}, day {day:03d}, CRC-16 {crc:05d}"


def _files_text(db: Database, network: FtnNetwork, settings: PublishSettings, kind: str) -> str:
    plain, zipped = edition_names(list_name(network, settings, kind), local_today(db).date())
    return f"{plain}" + (f" and {zipped}" if settings.archive else "")


def publishing_sections(db: Database, network: FtnNetwork) -> list[Section]:
    settings = get_settings(db, network.id)
    points = list_point_entries(db, network)
    listed = [p for p in points if p.listed and p.link.status != DISABLED]
    rows: list = [Field("State", "on" if settings.pointlist_on else "off")]
    rows.append(Field("Points", f"{len(listed)} of {len(points)} listed under Boss,{network.our_address.zone}:"
                                f"{network.our_address.net}/{network.our_address.node}"))
    rows.append(Field("Files", _files_text(db, network, settings, POINTLIST)))
    rows.append(Field("Hatch into", settings.pointlist_echo or "(none: written only)"))
    rows.append(Field("Last published", _last_text(settings, POINTLIST)))
    sections = [Section("Pointlist (FTS-5002)", rows)]
    nodes = list_segment_nodes(db, network.id)
    rows = [Field("State", "on" if settings.segment_on else "off")]
    try:
        host = data_line("Host", network.our_address.net, clean_fields(settings.host))
    except PublishError:
        host = "(not set: fill it in under [S]ettings)"
    rows.append(Field("Host line", sanitize_text(host)))
    rows.append(Field("Nodes", f"{len(nodes)} listed"))
    rows.append(Field("Files", _files_text(db, network, settings, SEGMENT)))
    rows.append(Field("Hatch into", settings.segment_echo or "(none: written only)"))
    rows.append(Field("Last published", _last_text(settings, SEGMENT)))
    sections.append(Section(f"Nodelist segment for net {network.our_address.net} (FTS-5000)", rows))
    schedule = SCHEDULE_LABELS[settings.schedule]
    if settings.schedule == SCHEDULE_WEEKLY:
        schedule = f"weekly, on {WEEKDAYS[settings.weekday]}"
    rows = [Field("Folder", sanitize_text(str(publish_folder(db, network, settings)))),
            Field("Schedule", schedule + ("" if network.enabled or settings.schedule == SCHEDULE_OFF
                                          else " (the network is off, so nothing runs)"))]
    if settings.failure:
        rows.append(Field("Last failure", sanitize_text(f"{(settings.failed_at or '')[:16]}: {settings.failure}")))
    sections.append(Section("Output", rows))
    if network.our_address.point:
        sections.append(Section(None, [Note(f"This BBS is a point on {network.name}: it publishes nothing.")]))
    return sections


_PUBLISH_HELP = [
    MenuEntry(label=menu_key("P", "ublish now"),
              brief="Write today's edition of each list that is on, and hatch it where a file echo is set"),
    MenuEntry(label=menu_key("S", "ettings"), brief="Which lists are on, names, file echoes, Host line, schedule"),
    MenuEntry(label=menu_key("E", "ntries for points"), brief="Each point's pointlist line, listed or not"),
    MenuEntry(label=menu_key("N", "odes"), brief="The segment's node lines"),
]


@located("FTN publishing")
async def ftn_publish_screen(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    """The network's pointlist and segment; see the module docstring."""
    message = None
    page = 0
    while True:
        network = await lane.run(get_network, network.id) or network
        chrome = await _load_chrome(lane, actor)
        sections = await lane.run(publishing_sections, network)
        choice, page = await show_detail(
            session, title=_detail_title(session, chrome, f"Publishing ({network.name})", own=True),
            sections=sections, page=page, message=message,
            actions=[("p", menu_key("P", "ublish now")), ("s", menu_key("S", "ettings")),
                     ("e", menu_key("E", "ntries for points")), ("n", menu_key("N", "odes")), _BACK_ACTION],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Publishing help",
            help_about=("A pointlist of this BBS's points, and a nodelist segment for the net it coordinates: "
                        "each list's state, files, the file echo it is hatched into and its last edition, then "
                        "the folder, the schedule and the last failure. Each edition is NAME.ddd, ddd the day "
                        "of the year; earlier editions stay in the folder."),
            help_entries=_PUBLISH_HELP,
        )
        message = None
        if choice == "b":
            return
        if choice == "p":
            message = await _publish(session, lane, actor, network)
        elif choice == "s":
            await edit_publish_settings(session, lane, actor, network)
        elif choice == "e":
            await _points_screen(session, lane, actor, network)
        elif choice == "n":
            await _nodes_screen(session, lane, actor, network)


async def _publish(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> str | None:
    settings = await lane.run(get_settings, network.id)
    kinds = [kind for kind in LIST_KINDS if settings.is_on(kind)]
    if not kinds:
        return colored("Neither list is on: switch one on under [S]ettings.", fg_color=ERROR_COLOR)
    echoes = sorted({settings.echo(kind) for kind in kinds if settings.echo(kind)})
    if echoes:
        await session.write_line("")
        what = " and ".join(LIST_LABELS[kind] for kind in kinds)
        if not await prompt_yes_no(session, f"Publish the {what} and hatch into {', '.join(echoes)}?",
                                   default=True):
            return "Nothing published."
    try:
        results = await lane.run(publish_now, network.id, actor=actor)
    except PublishError as exc:
        return colored(str(exc), fg_color=ERROR_COLOR)
    lines = [r.summary if not r.problem else colored(sanitize_text(r.summary), fg_color=ERROR_COLOR)
             for r in results]
    return "\r\n".join(lines)


# --- settings -----------------------------------------------------------------------------


def _settings_draft(settings: PublishSettings) -> dict:
    host = settings.host
    return {"pointlist_on": settings.pointlist_on, "pointlist_name": settings.pointlist_name,
            "pointlist_echo": settings.pointlist_echo, "segment_on": settings.segment_on,
            "segment_name": settings.segment_name, "segment_echo": settings.segment_echo,
            "host_name": host.name, "host_location": host.location, "host_sysop": host.sysop,
            "host_phone": host.phone, "host_speed": host.speed, "host_flags": host.flags,
            "folder": settings.folder, "archive": settings.archive, "schedule": settings.schedule,
            "weekday": settings.weekday}


def _line_audit_values(fields: ListFields, prefix: str = "") -> dict:
    return {f"{prefix}name": fields.name, f"{prefix}location": fields.location, f"{prefix}sysop": fields.sysop,
            f"{prefix}phone": fields.phone, f"{prefix}speed": fields.speed, f"{prefix}flags": fields.flags}


def settings_audit_values(settings: PublishSettings) -> dict:
    """Every value the publishing settings' save writes, as stored, by name,
    for its audit entry (issue #1515)."""
    return {"pointlist": settings.pointlist_on, "pointlist_name": settings.pointlist_name,
            "pointlist_echo": settings.pointlist_echo, "segment": settings.segment_on,
            "segment_name": settings.segment_name, "segment_echo": settings.segment_echo,
            **_line_audit_values(settings.host, "host_"), "folder": settings.folder, "zip": settings.archive,
            "schedule": settings.schedule, "weekday": WEEKDAYS[settings.weekday]}


def _settings_from_draft(base: PublishSettings, draft: dict) -> PublishSettings:
    return replace(
        base, pointlist_on=bool(draft["pointlist_on"]), pointlist_name=draft["pointlist_name"] or "",
        pointlist_echo=draft["pointlist_echo"] or "", segment_on=bool(draft["segment_on"]),
        segment_name=draft["segment_name"] or "", segment_echo=draft["segment_echo"] or "",
        host=ListFields(draft["host_name"] or "", draft["host_location"] or "", draft["host_sysop"] or "",
                        draft["host_phone"] or "", draft["host_speed"] or "", draft["host_flags"] or ""),
        folder=draft["folder"] or "", archive=bool(draft["archive"]), schedule=draft["schedule"],
        weekday=int(draft["weekday"]),
    )


def _on(key: str):
    return lambda d: "on" if d[key] else "off"


def _line_fields(section: str, prefix: str = "") -> list[FieldSpec]:
    """The fields of one list line: system name to flags."""
    k = lambda name: f"{prefix}{name}"  # noqa: E731
    return [
        FieldSpec(key=k("name"), label="System name", render=lambda d: d[k("name")] or "(not set)",
                  prompt=_optional_text_field(k("name")), section=section,
                  brief="The system's name; spaces become underscores",
                  help="Plain ASCII, no commas, up to 60 characters; spaces become underscores. Every field "
                       "of a line follows these rules."),
        FieldSpec(key=k("location"), label="Location", render=lambda d: d[k("location")] or "(not set)",
                  prompt=_optional_text_field(k("location")), section=section,
                  brief="Town, and region where it helps (Wellington_NZ)",
                  help="Up to 60 characters, spaces becoming underscores."),
        FieldSpec(key=k("sysop"), label="Sysop", render=lambda d: d[k("sysop")] or "(not set)",
                  prompt=_optional_text_field(k("sysop")), section=section,
                  brief="Who runs it, up to 36 characters",
                  help="The SysOp's name, up to 36 characters: Jane_Doe."),
        FieldSpec(key=k("phone"), label="Phone", render=lambda d: d[k("phone")] or "-Unpublished-",
                  prompt=_optional_text_field(k("phone")), section=section,
                  brief="-Unpublished- for a system on the Internet only",
                  help="-Unpublished-, or digits in dashed groups, country code first: 1-800-555-0100. "
                       "Empty is -Unpublished-."),
        FieldSpec(key=k("speed"), label="Speed", render=lambda d: d[k("speed")] or "300",
                  prompt=_optional_text_field(k("speed")), section=section,
                  brief="300 for a system without a phone line",
                  help="A number, up to 6 digits. Empty is 300."),
        FieldSpec(key=k("flags"), label="Flags", render=lambda d: d[k("flags")] or "(none)",
                  prompt=_optional_text_field(k("flags")), section=section,
                  brief="Comma-separated: IBN,INA:bbs.example.org",
                  help="IBN says it answers BinkP, on port 24554 or IBN:port; INA:host says where. "
                       "FTS-5001 lists the rest (CM, MO, U flags)."),
    ]


def settings_fields() -> list[FieldSpec]:
    schedules, weekdays = list(SCHEDULES), list(range(7))
    return [
        FieldSpec(key="pointlist_on", label="Pointlist", render=_on("pointlist_on"),
                  prompt=bool_field("pointlist_on"), step=bool_step("pointlist_on"), section="Pointlist",
                  brief="Publish this BBS's points (FTS-5002, Boss format)",
                  help="Off: no pointlist is written, by hand or by the schedule. On, an edition needs at least "
                       "one listed point."),
        FieldSpec(key="pointlist_name", label="File name", render=lambda d: f"{d['pointlist_name'] or 'POINTS'}.ddd",
                  prompt=_optional_text_field("pointlist_name"), section="Pointlist",
                  brief="Up to 8 letters and digits; ddd is the day of the year",
                  help="1-8 letters, digits, - or _. ddd is the day of the year in this BBS's time zone. "
                       "Empty is POINTS."),
        FieldSpec(key="pointlist_echo", label="Hatch into", render=lambda d: d["pointlist_echo"] or "(none)",
                  prompt=_optional_text_field("pointlist_echo"), section="Pointlist",
                  brief="The file echo it goes out in, if any",
                  help="A file echo tag. Each edition is hatched into it with a TIC saying it replaces the "
                       "last one; that needs the network's TIC password. Empty: the file is only written."),
        FieldSpec(key="segment_on", label="Nodelist segment", render=_on("segment_on"),
                  prompt=bool_field("segment_on"), step=bool_step("segment_on"), section="Nodelist segment",
                  brief="Publish a segment for the net this BBS coordinates (FTS-5000)",
                  help="Off: no segment is written. On, saving needs the Host line's fields filled in."),
        FieldSpec(key="segment_name", label="File name",
                  render=lambda d: f"{d['segment_name'] or 'NET<net>'}.ddd",
                  prompt=_optional_text_field("segment_name"), section="Nodelist segment",
                  brief="Up to 8 letters and digits (NET549)",
                  help="1-8 letters, digits, - or _. Empty is NET and this BBS's net number."),
        FieldSpec(key="segment_echo", label="Hatch into", render=lambda d: d["segment_echo"] or "(none)",
                  prompt=_optional_text_field("segment_echo"), section="Nodelist segment",
                  brief="The file echo it goes out in, if any",
                  help="As for the pointlist: hatched with a TIC replacing the last edition, which needs the "
                       "TIC password. Empty: the file is only written."),
        *_line_fields("Host line", "host_"),
        FieldSpec(key="folder", label="Folder", render=lambda d: d["folder"] or "(the BBS's own)",
                  prompt=_optional_text_field("folder"), section="Output",
                  brief="Where the files are written on this machine",
                  help="A full path, such as /bbs/publish. Empty: ftn-publish/<domain> in the BBS's file "
                       "storage, shown on the Publishing screen. Earlier editions stay in it."),
        FieldSpec(key="archive", label="ZIP", render=_on("archive"), prompt=bool_field("archive"),
                  step=bool_step("archive"), section="Output",
                  brief="Also write NAME.Zdd, and hatch that instead",
                  help="dd is the day of the year's last two digits. The plain file is written either way."),
        FieldSpec(key="schedule", label="Schedule", render=lambda d: SCHEDULE_LABELS[d["schedule"]],
                  prompt=choice_field("schedule", schedules), step=choice_step("schedule", schedules),
                  section="Output", brief="Publish by itself, daily or weekly",
                  help="An edition comes out on the first check of its day (this BBS's time zone), only while "
                       "the network is on. One that fails is not tried again that day. Off: only Publish now."),
        FieldSpec(key="weekday", label="Weekday", render=lambda d: WEEKDAYS[d["weekday"]],
                  prompt=choice_field("weekday", weekdays), step=choice_step("weekday", weekdays),
                  section="Output", brief="The day a weekly edition comes out",
                  help="Used only when Schedule is weekly. Friday by default, when FidoNet's own list comes out."),
    ]


async def edit_publish_settings(session: Session, lane: DatabaseLane, actor: User,
                                network: FtnNetwork) -> PublishSettings | None:
    base = await lane.run(get_settings, network.id)

    async def save(draft: dict) -> PublishSettings:
        candidate = _settings_from_draft(base, draft)

        def _persist(db: Database) -> PublishSettings:
            # The settings and their audit entry commit together; the entry
            # names every value that changed (issue #1515).
            with db.connection:
                before = settings_audit_values(get_settings(db, network.id))
                saved = save_settings(db, network, candidate, commit=False)
                record_action_without_commit(
                    db, actor=actor, action="set_ftn_publishing",
                    detail=change_detail(network.name, before, settings_audit_values(saved)))
            return saved

        return await lane.run(_persist)

    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=f"Publishing settings ({network.name})", fields=settings_fields(),
        draft=_settings_draft(base), save=save, error_type=PublishError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=(f"The Host line is the segment's first line, for net {network.our_address.net}. "
                  "A weekly edition comes out on the first check of its weekday."),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, "Saved the publishing settings.")
    return saved


# --- points ------------------------------------------------------------------------------


def _point_description(entry: PointEntry) -> str:
    if entry.link.status == DISABLED:
        return "disabled on the Links screen: left out"
    if not entry.listed:
        return "not listed"
    try:
        return sanitize_text(point_line(entry))
    except PublishError as exc:
        return sanitize_text(f"can't be listed: {exc}")


async def _points_screen(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    async def _load() -> list[PointEntry]:
        return await lane.run(list_point_entries, network)

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        picked = await pick_item(
            session, await _load(),
            name_of=lambda e: sanitize_text(e.link.label), stable_id_of=lambda e: e.link.id,
            description_of=_point_description, title=f"Pointlist lines ({network.name})", own=True,
            empty_message="No points: add them on the Links screen first.",
            refresh=_load, start_stable_id=start,
            live_label=lambda: "Each point's line in the pointlist; empty fields take the link's name",
            help_about=("Each point from the Links screen with its pointlist line, or why it isn't listed. "
                        "Picking one edits its line; the next edition carries it. Empty fields take the link's "
                        "name (Point_n without one) and Unknown. A point disabled on the Links screen is always "
                        "left out."),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.link.id
        await _edit_point(session, lane, actor, network, picked)


async def _edit_point(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                      entry: PointEntry) -> PointEntry | None:
    f = entry.fields
    draft = {"listed": entry.listed, "name": f.name, "location": f.location, "sysop": f.sysop, "phone": f.phone,
             "speed": f.speed, "flags": f.flags}
    defaults = point_defaults(entry.link)

    async def save(draft: dict) -> PointEntry:
        candidate = replace(entry, listed=bool(draft["listed"]), fields=ListFields(
            draft["name"] or "", draft["location"] or "", draft["sysop"] or "", draft["phone"] or "",
            draft["speed"] or "", draft["flags"] or ""))

        def _persist(db: Database) -> PointEntry:
            # The line and its audit entry commit together; the entry names
            # every value that changed (issue #1515).
            with db.connection:
                saved = save_point_entry(db, candidate, commit=False)
                record_action_without_commit(
                    db, actor=actor, action="set_ftn_pointlist_line",
                    detail=change_detail(
                        f"{network.name} point {entry.link.address.four_d}",
                        {"listed": entry.listed, **_line_audit_values(entry.fields)},
                        {"listed": saved.listed, **_line_audit_values(saved.fields)}))
            return saved

        return await lane.run(_persist)

    fields = [FieldSpec(key="listed", label="Listed", render=lambda d: "yes" if d["listed"] else "no",
                        prompt=bool_field("listed"), step=bool_step("listed"), section="Point",
                        brief="Whether the pointlist carries this point",
                        help="No leaves it out of the next edition. A point disabled on the Links screen is left "
                             "out whatever this says."),
              *_line_fields("Line")]
    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=f"Point {entry.link.address.four_d}", fields=fields, draft=draft, save=save,
        error_type=PublishError, save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=sanitize_text(f"Empty fields become: system {defaults.name}, location {defaults.location}, "
                               f"sysop {defaults.sysop}."),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, f"Saved point {entry.link.address.four_d}'s line.")
    return saved


# --- the segment's nodes ------------------------------------------------------------------


def _node_description(node: SegmentNode) -> str:
    try:
        return sanitize_text(segment_line(node))
    except PublishError as exc:
        return sanitize_text(f"can't be listed: {exc}")


async def _nodes_screen(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    status: str | None = None

    async def _load() -> list[SegmentNode]:
        return await lane.run(list_segment_nodes, network.id)

    async def _create() -> SegmentNode | None:
        return await _edit_node(session, lane, actor, network, None)

    async def _delete(node: SegmentNode) -> list[SegmentNode] | None:
        nonlocal status
        await session.write_line("")
        if not await prompt_yes_no(session, f"Delete node {node.node}'s line from the segment?", default=False):
            return None

        def _persist(db: Database) -> None:
            with db.connection:  # the line and its entry together (issue #1515)
                delete_segment_node(db, node.id, commit=False)
                record_action_without_commit(db, actor=actor, action="delete_ftn_segment_node",
                                             detail=f"{network.name}: node {network.our_address.net}/{node.node}")

        await lane.run(_persist)
        status = f"Deleted node {node.node}."
        return await _load()

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        picked = await pick_item(
            session, await _load(),
            name_of=lambda n: sanitize_text(f"{network.our_address.net}/{n.node} {n.fields.name}"),
            stable_id_of=lambda n: n.id, description_of=_node_description,
            title=f"Segment nodes (net {network.our_address.net})", own=True,
            empty_message="No nodes in the segment yet. [C]reate one: it starts from a downlink in this net.",
            refresh=_load, on_create=_create, item_keys={"d": _delete},
            live_nav=[MenuEntry(label=menu_key("D", "elete"), brief="Remove a node's line")],
            live_label=lambda: status or "The node lines under the segment's Host line",
            help_about=("The node lines under the segment's Host line, by node number, as the next edition "
                        "writes them. Picking one edits it. Create starts from the first downlink in this net "
                        "not listed yet, with its name and BinkP host filled in."),
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await _edit_node(session, lane, actor, network, picked)


async def _edit_node(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork,
                     existing: SegmentNode | None) -> SegmentNode | None:
    base = existing or await lane.run(suggested_node, network)
    f = base.fields
    draft = {"node": base.node, "keyword": base.keyword, "name": f.name, "location": f.location, "sysop": f.sysop,
             "phone": f.phone, "speed": f.speed, "flags": f.flags}

    async def save(draft: dict) -> SegmentNode:
        candidate = replace(base, node=int(draft["node"]), keyword=draft["keyword"], fields=ListFields(
            draft["name"] or "", draft["location"] or "", draft["sysop"] or "", draft["phone"] or "",
            draft["speed"] or "", draft["flags"] or ""))

        def _persist(db: Database) -> SegmentNode:
            # The line and its audit entry commit together; the entry names
            # every value that changed (issue #1515).
            values = lambda n: {"node": n.node, "keyword": n.keyword or "(plain)",  # noqa: E731
                                **_line_audit_values(n.fields)}
            with db.connection:
                saved = save_segment_node(db, network, candidate, commit=False)
                record_action_without_commit(
                    db, actor=actor, action="set_ftn_segment_node",
                    detail=change_detail(f"{network.name} node {network.our_address.net}/{saved.node}",
                                         values(existing) if existing is not None else None, values(saved)))
            return saved

        return await lane.run(_persist)

    keywords = list(NODE_KEYWORDS)
    fields = [FieldSpec(key="node", label="Node", render=lambda d: f"{network.our_address.net}/{d['node']}",
                        prompt=_int_field("node", "Node number"), section="Node",
                        brief="Its node number in this net, 1 to 32767",
                        help="Each number is listed once; 0 is the Host line's."),
              FieldSpec(key="keyword", label="Listed as", render=lambda d: KEYWORD_LABELS[d["keyword"]],
                        prompt=choice_field("keyword", keywords), step=choice_step("keyword", keywords),
                        section="Node", brief="A plain node, or Pvt, Hold or Down",
                        help="Pvt: a private node, its mail routed via the host. Hold: not taking calls for "
                             "now. Down: not operating. The keyword starts the node's line."),
              *_line_fields("Line")]
    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=(f"Node {network.our_address.net}/{existing.node}" if existing is not None
                              else f"New node in net {network.our_address.net}"),
        fields=fields, draft=draft, save=save, error_type=PublishError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=("It starts from the first downlink in this net not listed yet." if existing is None else None),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, f"Saved node {network.our_address.net}/{saved.node}.")
    return saved
