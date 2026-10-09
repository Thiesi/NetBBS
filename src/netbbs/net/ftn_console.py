"""
The SysOp console's FTN screens (design doc §6.8).

Configuration and operation are kept apart, as MRC's are:

- **Settings → Echomail & netmail (FTN)** (`ftn_networks_screen`): the networks, each
  edited in one draft -- addresses, uplink, passwords, polling, answering,
  default character set, Origin line, netmail level, and the node-wide
  answer port. A new network starts as fsxNet's main hub, the usual first
  network for a new BBS; the SysOp fills in the address fsxNet gave them.
- **Node → FTN mail** (`ftn_status_screen`): what the mailer and listener
  last did, what waits, and the work on a running network: poll now,
  AreaFix requests, nodelist import, held packets.
- **A board's echo** (`board_echo_action`): which echo area a board carries,
  from the board's own screen.

Every screen follows §3.5: content first, actions on the bar, drafts that
persist nothing before Save.

The console's own helpers live in `netbbs.net.admin_flow`, which imports
this module; they are reached through `_af()` when a screen runs.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.boards import Board
from netbbs.config import get_config, set_config
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import parse_address
from netbbs.ftn.areafix import AreaFixError, areafix_commands, queue_areafix
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.ftn.chrs import CP437
from netbbs.ftn.listener import HOST_CONFIG_KEY, PORT_CONFIG_KEY
from netbbs.ftn.networks import (
    FtnNetwork,
    FtnNetworkError,
    board_area,
    clear_board_area,
    delete_network,
    get_network,
    list_networks,
    save_network,
    set_board_area,
)
from netbbs.ftn.nodelist import MAX_NODELIST_BYTES, NodelistError, import_nodelist
from netbbs.ftn.queue import FtnQueueFullError, count_pending_outbound, delete_held, list_held
from netbbs.ftn.tosser import release_held
from netbbs.moderation.log import record_action
from netbbs.net.session import Session
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.rendering.detail import Field, Note, Section
from netbbs.rendering.layout import MenuEntry, status_badge
from netbbs.rendering.menu import menu_key
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

# fsxNet's main hub, from its infopack: the starting point for a new network.
FSXNET = {"name": "fsxNet", "domain": "fsxnet", "uplink_address": "21:1/100", "uplink_host": "net1.fsxnet.nz"}
CHARSETS = [CP437.codec, "utf-8", "latin-1", "cp850", "cp866"]


def _af():
    from netbbs.net import admin_flow

    return admin_flow


# --- Settings → Echomail & netmail (FTN) -------------------------------------------------


async def ftn_networks_screen(session: Session, lane: DatabaseLane, actor: User, *, node_controls=None) -> None:
    """The networks: picking one edits it, [C]reate adds one, [D]elete removes
    the highlighted one."""
    af = _af()
    status: str | None = None

    async def _load() -> list[FtnNetwork]:
        return await lane.run(list_networks)

    async def _create() -> FtnNetwork | None:
        return await edit_network(session, lane, actor, None, node_controls=node_controls)

    async def _delete(network: FtnNetwork) -> list[FtnNetwork] | None:
        nonlocal status
        await session.write_line("")
        if not await af.prompt_yes_no(
            session, f"Delete {network.name}? Its boards become local and keep their posts; "
                     f"mail waiting for it is dropped.", default=False):
            return None

        def _persist(db: Database) -> None:
            delete_network(db, network.id)
            record_action(db, actor=actor, action="delete_ftn_network", detail=f"deleted FTN network {network.name!r}")

        await lane.run(_persist)
        status = f"Deleted {network.name}."
        return await _load()

    chrome = await af._load_chrome(lane, actor)
    start = None
    while True:
        picked = await af._pick_item(
            session, await _load(),
            name_of=lambda n: n.name,
            stable_id_of=lambda n: n.id,
            description_of=lambda n: (f"{n.our_address.four_d} via {n.uplink_address.four_d}, "
                                      + ("on" if n.enabled else "off")
                                      + (", answers calls" if n.answers_calls else "")),
            title="FTN networks",
            empty_message="No FTN networks. [C]reate one: it starts as fsxNet's hub.",
            refresh=_load, on_create=_create, item_keys={"d": _delete},
            live_nav=[MenuEntry(label=menu_key("D", "elete"), brief="Remove a network")],
            live_label=lambda: status or "FidoNet-style networks: echomail on boards, netmail in Mail",
            start_stable_id=start,
            description_level=await lane.run(af.menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await edit_network(session, lane, actor, picked, node_controls=node_controls)


def _secret_field(key: str, label: str):
    af = _af()

    @af.inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await af.write_field_prompt(session, colored(f"{label} (typed unseen; Enter on an empty line clears it):",
                                                     fg_color=MUTED_COLOR))
        try:
            value = await session.read_line(echo=False, cancellable=True)
        except af.InputCancelled:
            return
        draft[key] = value.strip()

    return prompt


def _network_draft(network: FtnNetwork | None, listen_port: int) -> dict:
    if network is None:
        return {"name": FSXNET["name"], "domain": FSXNET["domain"], "our_address": "",
                "uplink_address": FSXNET["uplink_address"], "uplink_host": FSXNET["uplink_host"],
                "uplink_port": DEFAULT_PORT, "session_password": "", "packet_password": "", "areafix_password": "",
                "poll_minutes": 60, "answers_calls": False, "enabled": False, "default_charset": CP437.codec,
                "origin_text": "", "netmail_min_level": SYSOP_LEVEL, "listen_port": listen_port}
    return {"name": network.name, "domain": network.domain, "our_address": network.our_address.four_d,
            "uplink_address": network.uplink_address.four_d, "uplink_host": network.uplink_host,
            "uplink_port": network.uplink_port, "session_password": network.session_password,
            "packet_password": network.packet_password, "areafix_password": network.areafix_password,
            "poll_minutes": network.poll_minutes, "answers_calls": network.answers_calls,
            "enabled": network.enabled, "default_charset": network.default_charset,
            "origin_text": network.origin_text, "netmail_min_level": network.netmail_min_level,
            "listen_port": listen_port}


def _network_from_draft(draft: dict, existing: FtnNetwork | None) -> FtnNetwork:
    try:
        ours = parse_address(draft["our_address"]) if draft["our_address"].strip() else None
        uplink = parse_address(draft["uplink_address"])
    except FtnFormatError as exc:
        raise FtnNetworkError(f"{exc}.") from None
    if ours is None:
        raise FtnNetworkError("Enter this node's address on the network (the hub assigns it, e.g. 21:1/199).")
    return FtnNetwork(
        id=existing.id if existing is not None else None, name=draft["name"], domain=draft["domain"],
        our_address=ours, uplink_address=uplink, uplink_host=draft["uplink_host"],
        uplink_port=int(draft["uplink_port"]), session_password=draft["session_password"],
        packet_password=draft["packet_password"], areafix_password=draft["areafix_password"],
        poll_minutes=int(draft["poll_minutes"]), answers_calls=bool(draft["answers_calls"]),
        enabled=bool(draft["enabled"]), default_charset=draft["default_charset"], origin_text=draft["origin_text"],
        netmail_min_level=int(draft["netmail_min_level"]),
    )


def _listen_port(db: Database) -> int:
    value = get_config(db, PORT_CONFIG_KEY)
    return int(value) if value and value.isdigit() else DEFAULT_PORT


async def edit_network(session: Session, lane: DatabaseLane, actor: User, existing: FtnNetwork | None, *,
                       node_controls=None) -> FtnNetwork | None:
    """One network's settings in a draft; nothing is stored before [S]ave."""
    af = _af()
    draft = _network_draft(existing, await lane.run(_listen_port))
    secret = lambda key: (lambda d: "set" if d[key] else "(not set)")  # noqa: E731
    fields = [
        af.FieldSpec(key="enabled", label="Enabled",
                     render=lambda d: "yes" if d["enabled"] else "no", prompt=af.bool_field("enabled"),
                     step=af.bool_step("enabled"), section="Network",
                     brief="Call the uplink and carry this network's mail",
                     help="Off by default. Nothing is sent or polled while it is off."),
        af.FieldSpec(key="name", label="Name",
                     render=lambda d: d["name"], prompt=af.text_field("name", required=True), section="Network",
                     brief="How the network is called here (fsxNet, FidoNet)"),
        af.FieldSpec(key="domain", label="Domain",
                     render=lambda d: d["domain"], prompt=af.text_field("domain", required=True), section="Network",
                     brief="The network's 5D domain, up to 8 characters (fsxnet)"),
        af.FieldSpec(key="our_address", label="Our address",
                     render=lambda d: d["our_address"] or "(not set)", prompt=af.text_field("our_address"),
                     section="Network", brief="zone:net/node[.point] the network gave this BBS"),
        af.FieldSpec(key="netmail_min_level",
                     label="Netmail level", render=lambda d: str(d["netmail_min_level"]),
                     prompt=af._int_field("netmail_min_level", "Netmail level"), section="Network",
                     brief="Who may send netmail; 255 = SysOp only",
                     help="Netmail leaves under this BBS's address, so it starts at SysOp only."),
        af.FieldSpec(key="default_charset",
                     label="Charset", render=lambda d: d["default_charset"],
                     prompt=af.choice_field("default_charset", CHARSETS), step=af.choice_step("default_charset", CHARSETS),
                     section="Network", brief="How to read a message that names no character set"),
        af.FieldSpec(key="origin_text", label="Origin",
                     render=lambda d: d["origin_text"] or "(the BBS's name)", prompt=af._optional_text_field("origin_text"),
                     section="Network", brief="The line under every post sent out"),
        af.FieldSpec(key="uplink_address", label="Uplink",
                     render=lambda d: d["uplink_address"], prompt=af.text_field("uplink_address", required=True),
                     section="Uplink", brief="The hub's address (fsxNet: 21:1/100)"),
        af.FieldSpec(key="uplink_host", label="Host",
                     render=lambda d: d["uplink_host"] or "(not set)", prompt=af.text_field("uplink_host"),
                     section="Uplink", brief="Where the hub answers BinkP"),
        af.FieldSpec(key="uplink_port", label="Port",
                     render=lambda d: str(d["uplink_port"]), prompt=af._int_field("uplink_port", "Port"),
                     section="Uplink", brief="Usually 24554"),
        af.FieldSpec(key="session_password",
                     label="Session password", render=secret("session_password"),
                     prompt=_secret_field("session_password", "Session password"), section="Uplink",
                     brief="Proves this BBS to the hub (CRAM-MD5 when offered)"),
        af.FieldSpec(key="packet_password",
                     label="Packet password", render=secret("packet_password"),
                     prompt=_secret_field("packet_password", "Packet password"), section="Uplink",
                     brief="Up to 8 characters, if the hub uses one"),
        af.FieldSpec(key="areafix_password",
                     label="AreaFix password", render=secret("areafix_password"),
                     prompt=_secret_field("areafix_password", "AreaFix password"), section="Uplink",
                     brief="For linking echo areas"),
        af.FieldSpec(key="poll_minutes", label="Poll every",
                     render=lambda d: f"{d['poll_minutes']} minutes", prompt=af._int_field("poll_minutes", "Minutes"),
                     section="Calls", brief="How often to call the hub; at least once a day"),
        af.FieldSpec(key="answers_calls", label="Answer calls",
                     render=lambda d: "yes" if d["answers_calls"] else "no", prompt=af.bool_field("answers_calls"),
                     step=af.bool_step("answers_calls"), section="Calls",
                     brief="Let the hub (and other nodes) call this BBS",
                     help="Needs the answer port open to the internet. Hubs deliver crash mail sooner."),
        af.FieldSpec(key="listen_port",
                     label="Answer port", render=lambda d: f"{d['listen_port']} (every network)",
                     prompt=af._int_field("listen_port", "Answer port"), section="Calls",
                     brief="Where this BBS answers BinkP, for all networks"),
    ]

    async def save(draft: dict) -> FtnNetwork:
        candidate = _network_from_draft(draft, existing)
        port = int(draft["listen_port"])
        if not 0 < port < 65536:
            raise FtnNetworkError("The answer port must be between 1 and 65535.")

        def _persist(db: Database) -> FtnNetwork:
            saved = save_network(db, candidate)
            set_config(db, PORT_CONFIG_KEY, str(port))
            record_action(db, actor=actor, action="set_ftn_network",
                          detail=(f"{saved.name}: {saved.our_address} via {saved.uplink_address} "
                                  f"{saved.uplink_host}:{saved.uplink_port} enabled={saved.enabled} "
                                  f"answers={saved.answers_calls} netmail_level={saved.netmail_min_level}"))
            return saved

        return await lane.run(_persist)

    chrome = await af._load_chrome(lane, actor)
    saved = await af.edit_resource_draft(
        session, lane, title=existing.name if existing is not None else "New FTN network",
        fields=fields, draft=draft, save=save, error_type=FtnNetworkError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=("A new network starts as fsxNet's main hub: fill in the address fsxNet gave you."
                  if existing is None else None),
        description_level=await lane.run(af.menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        af._announce_line(session, f"Saved {saved.name}. The mailer and the listener pick it up within a minute.")
        mailer = getattr(node_controls, "ftn_mailer", None) if node_controls is not None else None
        if mailer is not None:
            mailer.kick()
    return saved


# --- Node → FTN mail ---------------------------------------------------------


def _status_sections(db: Database, mailer, listener, *, unicode_style: bool) -> list[Section]:
    sections = []
    for network in list_networks(db):
        rows: list = [Field("State", "on" if network.enabled else "off")]
        status = mailer.status.get(network.id) if mailer is not None else None
        if status is not None and status.last_success_at:
            rows.append(Field("Last call", f"{status.last_success_at}: {status.last_summary}"))
        if status is not None and status.last_error:
            rows.append(Field("Last error", sanitize_text(status.last_error)))
        rows.append(Field("Waiting", f"{count_pending_outbound(db, network.id)} messages"))
        # The same packets [H]eld packets lists: this network's, and those a
        # deleted network left behind.
        held = [h for h in list_held(db) if h.network_id in (network.id, None)]
        rows.append(Field("Held", f"{len(held)} packets"))
        imported = db.connection.execute(
            "SELECT nodelist_imported_at, nodelist_entries FROM ftn_networks WHERE id = ?", (network.id,)
        ).fetchone()
        rows.append(Field("Nodelist", f"{imported['nodelist_entries']} nodes, imported {imported['nodelist_imported_at']}"
                          if imported["nodelist_imported_at"] else "none imported: netmail goes via the uplink"))
        sections.append(Section(f"{network.name} ({network.our_address.four_d})", rows))
    if not sections:
        sections.append(Section(None, [Note("No FTN networks yet: add one under Settings → Echomail & netmail (FTN).")]))
    if mailer is None:
        sections.append(Section(None, [Note("The mailer runs in the node; this console shows no live state.")]))
    if listener is not None:
        where = listener.listening_on
        rows = [Field("Answering", f"port {where[1]}" if where else "no")]
        if listener.last_error:
            rows.append(Field("Error", sanitize_text(listener.last_error)))
        for call in list(listener.recent)[-5:]:
            rows.append(Field(call.at[:19], sanitize_text(f"{call.addresses or call.peer}: {call.outcome}")))
        sections.append(Section("Calls answered", rows))
    return sections


async def ftn_status_screen(session: Session, lane: DatabaseLane, actor: User, node_controls) -> None:
    """What the FTN gateway last did, and the work on it."""
    af = _af()
    mailer = getattr(node_controls, "ftn_mailer", None)
    listener = getattr(node_controls, "ftn_listener", None)
    message = None
    page = 0
    while True:
        chrome = await af._load_chrome(lane, actor)
        sections = await lane.run(lambda db: _status_sections(db, mailer, listener, unicode_style=chrome.unicode_style))
        choice, page = await af.show_detail(
            session, title=af._detail_title(session, chrome, "FTN mail", breadcrumb=("Node",)),
            sections=sections, page=page, message=message,
            actions=[("p", menu_key("P", "oll now")), ("a", menu_key("A", "reaFix")),
                     ("n", menu_key("N", "odelist import")), ("h", menu_key("H", "eld packets")), af._BACK_ACTION],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="FTN mail help",
            help_about=(
                "What the echomail and netmail gateway last did, network by network. "
                "Each action asks which network it is for."
            ),
        )
        message = None
        if choice == "b":
            return
        network = await _pick_network(session, lane, actor)
        if network is None:
            continue
        if choice == "p":
            if mailer is None:
                message = colored("Polling needs the running node.", fg_color=MUTED_COLOR)
                continue
            status = await mailer.poll(network)
            message = (f"Called {network.name}: {status.last_summary}" if status.last_error is None
                       else colored(f"Call failed: {sanitize_text(status.last_error)}", fg_color=af.ERROR_COLOR))
        elif choice == "a":
            await _areafix(session, lane, actor, network, mailer)
        elif choice == "n":
            await _import_nodelist(session, lane, actor, network)
        elif choice == "h":
            await _held_packets(session, lane, actor, network)


async def _pick_network(session: Session, lane: DatabaseLane, actor: User) -> FtnNetwork | None:
    af = _af()
    networks = await lane.run(list_networks)
    if len(networks) <= 1:
        if not networks:
            af._announce(session, "No FTN networks yet: add one under Settings → Echomail & netmail (FTN).", error=True)
        return networks[0] if networks else None
    chrome = await af._load_chrome(lane, actor)
    return await af.pick_item(
        session, networks, name_of=lambda n: n.name, stable_id_of=lambda n: n.id,
        description_of=lambda n: n.our_address.four_d,
        title="Which network?", empty_message="No networks.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )


async def _areafix(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, mailer) -> None:
    af = _af()
    await session.write_line("")
    await session.write_line(colored(
        f"AreaFix at {network.uplink_address.four_d}: +TAG links an echo, -TAG unlinks it, %LIST asks what "
        f"the hub carries. The hub's answer comes to your Mail.", fg_color=MUTED_COLOR))
    await af.write_prompt(session, "Request: ")
    try:
        text = await session.read_line(cancellable=True)
    except af.InputCancelled:
        text = ""
    if not text.strip():
        af._announce(session, "Nothing sent.", color=MUTED_COLOR)
        return
    try:
        commands = areafix_commands(text)
        await lane.run(lambda db: queue_areafix(db, get_network(db, network.id), actor, commands))
    except (AreaFixError, FtnQueueFullError) as exc:
        af._announce(session, str(exc), error=True)
        return
    if mailer is not None:
        mailer.kick()
    af._announce(session, f"AreaFix request queued: {' '.join(commands)}. It goes out with the next call.")


async def _import_nodelist(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    af = _af()
    await session.write_line("")
    await session.write_line(colored(
        f"The {network.name} nodelist, as a file on this machine (unpacked, e.g. /bbs/nodelist/FSXNET.123).",
        fg_color=MUTED_COLOR))
    await af.write_prompt(session, "Nodelist file: ")
    try:
        path_text = (await session.read_line(cancellable=True)).strip()
    except af.InputCancelled:
        path_text = ""
    if not path_text:
        af._announce(session, "Nothing imported.", color=MUTED_COLOR)
        return
    try:
        count = await import_nodelist_file(lane, actor, network, Path(os.path.expanduser(path_text)))
    except (OSError, NodelistError) as exc:
        af._announce(session, f"Not imported: {exc}", error=True)
        return
    af._announce(session, f"Imported {count} nodes for {network.name}. Netmail to listed BinkP nodes now goes direct.")


async def import_nodelist_file(lane: DatabaseLane, actor: User | None, network: FtnNetwork, path: Path) -> int:
    """Read and import a nodelist file; shared by the console and the CLI."""
    size = await asyncio.to_thread(lambda: path.stat().st_size)
    if size > MAX_NODELIST_BYTES:
        raise NodelistError(f"{path.name} is over {MAX_NODELIST_BYTES // (1024 * 1024)} MiB")
    text = await asyncio.to_thread(lambda: path.read_bytes().decode("cp437", errors="replace"))

    def _persist(db: Database) -> int:
        count = import_nodelist(db, network.id, text)
        record_action(db, actor=actor, action="import_ftn_nodelist",
                      detail=f"{network.name}: {count} nodes from {path.name}")
        return count

    return await lane.run(_persist)


async def _held_packets(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    af = _af()
    status: str | None = None

    async def _load():
        return [h for h in await lane.run(list_held) if h.network_id in (network.id, None)]

    async def _release(held):
        nonlocal status
        result = await lane.run(lambda db: release_held(db, get_network(db, network.id), held.id))
        await lane.run(lambda db: record_action(db, actor=actor, action="release_ftn_packet",
                                                detail=f"{network.name}: {held.file_name} from {held.remote_address}"))
        status = (f"Released {held.file_name}: {result.posts} posts, {result.netmail} netmail, "
                  f"{result.duplicates} duplicates" + (f", held again: {result.refused_packet}"
                                                       if result.refused_packet else ""))
        return await _load()

    async def _delete(held):
        nonlocal status
        await lane.run(delete_held, held.id)
        status = f"Deleted {held.file_name}."
        return await _load()

    chrome = await af._load_chrome(lane, actor)
    await af._pick_item(
        session, await _load(),
        name_of=lambda h: f"{h.file_name} from {h.remote_address}",
        stable_id_of=lambda h: h.id,
        description_of=lambda h: f"{h.size} bytes, {h.received_at[:19]}: {sanitize_text(h.reason)}",
        title=f"Held packets ({network.name})",
        empty_message="Nothing is held.",
        refresh=_load, item_keys={"r": _release, "d": _delete},
        live_nav=[MenuEntry(label=menu_key("R", "elease"), brief="Toss it as from a known system"),
                  MenuEntry(label=menu_key("D", "elete"), brief="Throw it away")],
        live_label=lambda: status or "Held: from an unknown caller, or what could not be stored",
        description_level=await lane.run(af.menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )


# --- a board's echo ------------------------------------------------------------


def board_echo_rows(db: Database, board: Board) -> list[Field]:
    """The board screen's FTN rows: the echo it carries, if any."""
    area = board_area(db, board)
    if area is None:
        return [Field("Echo", "none (a local board)")]
    network = get_network(db, area.network_id)
    return [Field("Echo", f"{area.tag} on {network.name if network else '?'}")]


async def board_echo_action(session: Session, lane: DatabaseLane, actor: User, board: Board) -> bool:
    """[E]cho on a board's screen: carry an echo area, or stop carrying it
    (an empty tag). Returns whether anything changed."""
    af = _af()
    networks = await lane.run(list_networks)
    if not networks:
        af._announce(session, "No FTN networks yet: add one under Settings → Echomail & netmail (FTN).", error=True)
        return False
    network = networks[0] if len(networks) == 1 else await _pick_network(session, lane, actor)
    if network is None:
        return False
    current = await lane.run(board_area, board)
    await session.write_line("")
    await session.write_line(colored(
        f"The {network.name} echo tag this board carries (e.g. FSX_GEN). An empty line makes it local again; "
        f"link the echo at the hub with AreaFix (Node → FTN mail).", fg_color=MUTED_COLOR))
    await af.write_prompt(session, "Echo tag: ")
    try:
        tag = (await session.read_line(cancellable=True,
                                       initial=current.tag if current and current.network_id == network.id else "")).strip()
    except af.InputCancelled:
        return False
    try:
        if not tag:
            if current is None:
                return False
            await lane.run(clear_board_area, board)
            outcome = f"{board.name} is a local board again; its posts stay."
        else:
            mapping = await lane.run(set_board_area, board, network.id, tag)
            outcome = f"{board.name} carries {mapping.tag} on {network.name}."
    except FtnNetworkError as exc:
        af._announce(session, str(exc), error=True)
        return False
    await lane.run(lambda db: record_action(db, actor=actor, action="set_board_echo", detail=outcome))
    af._announce(session, outcome)
    return True
