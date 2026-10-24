"""
The SysOp console's FTN screens (design doc §6.8).

Configuration and operation are kept apart, as MRC's are:

- **Settings → Echomail & netmail (FTN)** (`ftn_networks_screen`): the networks, each
  edited in one draft -- addresses, uplink, passwords, polling, answering,
  default character set, Origin line, netmail level, the node-wide
  answer port, and the Transport section: the node-wide answer addresses,
  TLS for calls to the uplink (pinned or verified), and the node-wide TLS
  answer port with its certificate files (issues #1467, #1468). A new network starts as fsxNet's main hub, the usual first
  network for a new BBS; the SysOp fills in the address fsxNet gave them.
  **[L]inks** opens the highlighted network's points and downlinks
  (`netbbs.net.ftn_links_console`, issues #1478 and #1479), and
  **[P]ublishing** its pointlist and nodelist segment
  (`netbbs.net.ftn_publish_console`, issue #1480).
- **Node → Echomail & netmail (FTN)** (`ftn_status_screen`): what the mailer and listener
  last did, what waits, and the work on a running network: poll now,
  AreaFix and FileFix requests, echo-list import, nodelist import, held
  items (packets, files and TICs, with [R]elease, [S]ave, [E]xport and
  [D]elete) and unknown echoes (tags no board or file area carries, with
  [C]reate, [M]ap and [D]elete), and [R]equest files from another node
  (issue #1475). The robot screens live in `netbbs.net.ftn_robots_console`,
  the file-request pieces in `netbbs.net.ftn_freq_console`.
- **A board's echo** (`board_echo_action`): which echo area a board carries,
  from the board's own screen, and its own Origin line
  (`board_origin_action`, issue #1474); **a file area's file echo**
  (`file_area_echo_action`) likewise, from the area's screen.

Every screen follows §3.5: content first, actions on the bar, drafts that
persist nothing before Save.

The console's own helpers come from `netbbs.net.admin_ui` and
`netbbs.net.admin_fields`, which never import this module.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.boards import Board
from netbbs.config import get_config, set_config_without_commit
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import parse_address
from netbbs.ftn.areafix import AREAFIX, FILEFIX
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.ftn.chrs import CP437
from netbbs.ftn.listener import (
    HOST_CONFIG_KEY,
    PORT_CONFIG_KEY,
    TLS_CERT_CONFIG_KEY,
    TLS_KEY_CONFIG_KEY,
    TLS_PORT_CONFIG_KEY,
)
from netbbs.ftn.transport import (
    DEFAULT_TLS_PORT,
    TLS_OFF,
    TLS_PINNED,
    TLS_VERIFIED,
    parse_host_port,
    parse_listen_hosts,
    show_fingerprint,
)
from netbbs.ftn.message import origin_room
from netbbs.ftn.networks import (
    BUNDLE_MODES,
    BUNDLE_OFF,
    BUNDLE_ZIP,
    AreaMapping,
    FtnNetwork,
    FtnNetworkError,
    board_area,
    clear_board_area,
    clear_file_area_tic,
    delete_network,
    file_area_tic,
    get_network,
    list_networks,
    save_network,
    set_board_area,
    set_board_origin,
    set_file_area_tic,
)
from netbbs.ftn.nodelist import HOW_MANUAL, ImportOutcome, NodelistError, import_list, nodelist_status
from netbbs.ftn.nodelist_feed import read_list_file
from netbbs.ftn.outbound import count_waiting
from netbbs.ftn.queue import (
    HELD_FILE,
    HELD_PACKET,
    FtnQueueFullError,
    bad_echo_dropped,
    delete_bad_echo,
    delete_held,
    held_bad_file_echo_ids,
    list_bad_echoes,
    list_bad_file_echoes,
    list_held,
)
from netbbs.ftn.tosser import TossResult, release_held, retoss_bad_echo
from netbbs.moderation.log import record_action, record_action_without_commit
from netbbs.net.admin_fields import _int_field, _optional_text_field
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce,
    _announce_line,
    _detail_title,
    _load_chrome,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.char_input import InputCancelled
from netbbs.net.ftn_freq_console import (  # issue #1475
    freq_draft,
    freq_fields,
    freq_settings_from_draft,
    freq_status_rows,
    request_files,
    save_freq_settings_without_commit,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.ftn_robots_console import (
    change_robot_password,
    import_echolist,
    robot_draft,
    robot_field_specs,
    robot_request,
    robot_values,
)
from netbbs.net.location import located
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.picker import pick_item as _pick_item
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    bool_step,
    choice_field,
    choice_step,
    inline_field,
    read_field_line,
    text_field,
    write_field_prompt,
)
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import ERROR_COLOR, MUTED_COLOR, colored
from netbbs.rendering.detail import Field, Note, Section
from netbbs.rendering.layout import MenuEntry, status_badge
from netbbs.rendering.menu import menu_key
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

# fsxNet's main hub, from its infopack: the starting point for a new network.
FSXNET = {"name": "fsxNet", "domain": "fsxnet", "uplink_address": "21:1/100", "uplink_host": "net1.fsxnet.nz"}
CHARSETS = [CP437.codec, "utf-8", "latin-1", "cp850", "cp866"]


# --- Settings → Echomail & netmail (FTN) -------------------------------------------------


@located("FTN networks")
async def ftn_networks_screen(session: Session, lane: DatabaseLane, actor: User, *, node_controls=None) -> None:
    """The networks: picking one edits it, [C]reate adds one, [D]elete removes
    the highlighted one."""
    status: str | None = None

    async def _load() -> list[FtnNetwork]:
        return await lane.run(list_networks)

    async def _create() -> FtnNetwork | None:
        return await edit_network(session, lane, actor, None, node_controls=node_controls)

    async def _delete(network: FtnNetwork) -> list[FtnNetwork] | None:
        nonlocal status
        await session.write_line("")
        if not await prompt_yes_no(
            session, f"Delete {network.name}? Its boards become local and keep their posts; "
                     f"mail waiting for it is dropped.", default=False):
            return None

        def _persist(db: Database) -> None:
            delete_network(db, network.id)
            record_action(db, actor=actor, action="delete_ftn_network", detail=f"deleted FTN network {network.name!r}")

        await lane.run(_persist)
        status = f"Deleted {network.name}."
        return await _load()

    # --- robots (issue #1463): Change robot password ---
    async def _robot_password(network: FtnNetwork) -> list[FtnNetwork] | None:
        nonlocal status
        mailer = getattr(node_controls, "ftn_mailer", None) if node_controls is not None else None
        outcome = await change_robot_password(session, lane, actor, network, mailer)
        if outcome is None:
            return None
        status = outcome
        return await _load()
    # --- end robots ---

    # --- points and downlinks (issues #1478, #1479) ---
    async def _links(network: FtnNetwork) -> list[FtnNetwork] | None:
        from netbbs.net.ftn_links_console import ftn_links_screen

        await ftn_links_screen(session, lane, actor, network, node_controls=node_controls)
        return await _load()
    # --- end points and downlinks ---

    # --- publishing (issue #1480) ---
    async def _publishing(network: FtnNetwork) -> list[FtnNetwork] | None:
        from netbbs.net.ftn_publish_console import ftn_publish_screen

        await ftn_publish_screen(session, lane, actor, network)
        return await _load()
    # --- end publishing ---

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        picked = await _pick_item(
            session, await _load(),
            name_of=lambda n: n.name,
            stable_id_of=lambda n: n.id,
            description_of=lambda n: (f"{n.our_address.four_d} via {n.uplink_address.four_d}, "
                                      + ("on" if n.enabled else "off")
                                      + (", answers calls" if n.answers_calls else "")),
            title="FTN networks", own=True,
            empty_message="No FTN networks. [C]reate one: it starts as fsxNet's hub.",
            refresh=_load, on_create=_create, item_keys={"d": _delete, "r": _robot_password, "l": _links,
                                                         "p": _publishing},
            live_nav=[MenuEntry(label=menu_key("D", "elete"), brief="Remove a network",
                                detailed="Remove it: its boards turn local, its waiting mail is dropped"),
                      MenuEntry(label=menu_key("R", "obot password"), brief="Change robot password: send %PWD",
                                detailed="Change AreaFix's or FileFix's password at the hub and here"),
                      MenuEntry(label=menu_key("L", "inks"), brief="Points and downlinks on this network"),
                      MenuEntry(label=menu_key("P", "ublishing"),
                                brief="Pointlist and nodelist segment")],  # issue #1480
            live_label=lambda: status or "FidoNet-style networks: echomail on boards, netmail in Mail",
            help_about=("The FTN networks this BBS is on: its address, the uplink, on or off, and whether it "
                        "answers calls. Picking one edits all its settings in one draft; nothing is stored "
                        "before Save, and the running node picks a change up within a minute."),
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await edit_network(session, lane, actor, picked, node_controls=node_controls)


def _secret_field(key: str, label: str):

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


def _network_draft(network: FtnNetwork | None, listen_port: int) -> dict:
    if network is None:
        return {"name": FSXNET["name"], "domain": FSXNET["domain"], "our_address": "",
                "uplink_address": FSXNET["uplink_address"], "uplink_host": FSXNET["uplink_host"],
                "uplink_port": DEFAULT_PORT, "session_password": "", "packet_password": "", "areafix_password": "",
                "poll_minutes": 60, "answers_calls": False, "enabled": False, "default_charset": CP437.codec,
                "origin_text": "", "netmail_min_level": SYSOP_LEVEL, "listen_port": listen_port,
                **robot_draft(None), **_nodelist_draft(None)}
    return {"name": network.name, "domain": network.domain, "our_address": network.our_address.four_d,
            "uplink_address": network.uplink_address.four_d, "uplink_host": network.uplink_host,
            "uplink_port": network.uplink_port, "session_password": network.session_password,
            "packet_password": network.packet_password, "areafix_password": network.areafix_password,
            "poll_minutes": network.poll_minutes, "answers_calls": network.answers_calls,
            "enabled": network.enabled, "default_charset": network.default_charset,
            "origin_text": network.origin_text, "netmail_min_level": network.netmail_min_level,
            "listen_port": listen_port, **robot_draft(network), **_nodelist_draft(network)}


# --- the nodelist echo's fields (issue #1464) ---


def _nodelist_draft(network: FtnNetwork | None) -> dict:
    if network is None:
        return {"nodelist_echo": "", "nodelist_name": "", "nodediff_name": ""}
    return {"nodelist_echo": network.nodelist_echo, "nodelist_name": network.nodelist_name,
            "nodediff_name": network.nodediff_name}


def _nodelist_fields() -> list[FieldSpec]:
    return [
        FieldSpec(key="nodelist_echo", label="Nodelist echo",
                     render=lambda d: d["nodelist_echo"] or "(none: import by hand)",
                     prompt=_optional_text_field("nodelist_echo"), section="Nodelist",
                     brief="The file echo the network's nodelist arrives in (NIX_LIST)",
                     help="A list arriving there is imported once its CRC-16 checks, and netmail to listed "
                          "BinkP nodes then goes direct. It needs List name. The echo needs no file area. "
                          "Empty: lists are imported by hand."),
        FieldSpec(key="nodelist_name", label="List name",
                     render=lambda d: (f"{d['nodelist_name']}.nnn" if d["nodelist_name"] else "(not set)"),
                     prompt=_optional_text_field("nodelist_name"), section="Nodelist",
                     brief="The list's file name without the day (NODELIST, NIXLIST)",
                     help="1-8 letters, digits, - or _: NIXLIST for NIXLIST.289 or its ZIP NIXLIST.Z89. "
                          "Other files in the echo are not taken for the list."),
        FieldSpec(key="nodediff_name", label="Diff name",
                     render=lambda d: (f"{d['nodediff_name']}.nnn" if d["nodediff_name"] else "(none: full lists only)"),
                     prompt=_optional_text_field("nodediff_name"), section="Nodelist",
                     brief="The daily diffs' file name, if the network sends them (NODEDIFF)",
                     help="Each diff is applied to the last list and must pass its CRC-16. Empty: diffs are "
                          "not applied, only full lists. It must differ from List name."),
    ]


# --- end nodelist echo fields ---


# --- transport: answer addresses and TLS (issues #1467, #1468) ---

TLS_CHOICES = [TLS_OFF, TLS_PINNED, TLS_VERIFIED]
_TLS_LABELS = {TLS_OFF: "off", TLS_PINNED: "on, certificate pinned", TLS_VERIFIED: "on, certificate verified"}


def _transport_draft(db: Database, network: FtnNetwork | None) -> dict:
    return {
        "uplink_tls": network.uplink_tls if network is not None else TLS_OFF,
        "uplink_tls_pin": network.uplink_tls_pin if network is not None else "",
        "listen_host": get_config(db, HOST_CONFIG_KEY) or "",
        "tls_port": get_config(db, TLS_PORT_CONFIG_KEY) or "",
        "tls_cert": get_config(db, TLS_CERT_CONFIG_KEY) or "",
        "tls_key": get_config(db, TLS_KEY_CONFIG_KEY) or "",
    }


def _pin_text(draft: dict) -> str:
    if draft["uplink_tls"] != TLS_PINNED:
        return "(used only when pinned)"
    if not draft["uplink_tls_pin"]:
        return "(learned on the first call)"
    return show_fingerprint(draft["uplink_tls_pin"])[:23] + "..."


def _transport_fields() -> list[FieldSpec]:
    return [
        FieldSpec(key="listen_host",
                     label="Answer addresses",
                     render=lambda d: d["listen_host"] or "(every address, IPv6 and IPv4)",
                     prompt=_optional_text_field("listen_host"), section="Transport",
                     brief="IP addresses to answer on, for all networks; empty for all",
                     help="One or more IP addresses separated by spaces, such as 192.0.2.7 2001:db8::7. "
                          "Empty answers on every address, IPv6 and IPv4. It applies to every network, on both "
                          "answer ports; the running node moves within a minute."),
        FieldSpec(key="uplink_tls", label="Call over TLS",
                     render=lambda d: _TLS_LABELS[d["uplink_tls"]],
                     prompt=choice_field("uplink_tls", TLS_CHOICES), step=choice_step("uplink_tls", TLS_CHOICES),
                     section="Transport", brief="Call the hub over TLS (binkps), usually on port 24553",
                     help="Only if the hub offers BinkP over TLS; set the uplink's port to its TLS port. "
                          "Pinned: the first call learns the hub's certificate and a changed one is refused. "
                          "Verified: the hub's certificate must be signed by a known authority for its host "
                          "name. The session password proves this BBS either way."),
        FieldSpec(key="uplink_tls_pin", label="Pinned certificate",
                     render=_pin_text, prompt=_optional_text_field("uplink_tls_pin"), section="Transport",
                     brief="The hub's certificate fingerprint (SHA-256); empty to learn it again",
                     help="Empty it after the hub renews its certificate, or paste the fingerprint the hub "
                          "published."),
        FieldSpec(key="tls_port", label="TLS answer port",
                     render=lambda d: (f"{d['tls_port']} (every network)" if d["tls_port"] else "(off)"),
                     prompt=_optional_text_field("tls_port"), section="Transport",
                     brief=f"Also answer BinkP over TLS on this port (usually {DEFAULT_TLS_PORT}); empty for off",
                     help="Needs a certificate and its key, and must differ from the answer port. It applies to "
                          "every network that answers calls; the plain port goes on answering. Callers still "
                          "prove themselves with their session password."),
        FieldSpec(key="tls_cert", label="Certificate file",
                     render=lambda d: d["tls_cert"] or "(not set)", prompt=_optional_text_field("tls_cert"),
                     section="Transport", brief="PEM file with the certificate (and chain) for the TLS port",
                     help="A path on this machine, such as the certificate a web server already uses. "
                          "A self-signed one is common for BinkP."),
        FieldSpec(key="tls_key", label="Key file",
                     render=lambda d: d["tls_key"] or "(in the certificate file)",
                     prompt=_optional_text_field("tls_key"), section="Transport",
                     brief="PEM file with the certificate's private key, if separate",
                     help="Empty when the key is in the certificate file. If the files can't be loaded, the "
                          "TLS port stays closed and the status screen shows a TLS error; the node tries them "
                          "again every minute."),
    ]


def _transport_values(draft: dict, candidate: FtnNetwork, listen_port: int,
                      existing: FtnNetwork | None) -> tuple[FtnNetwork, dict[str, str]]:
    """The network's TLS settings, and the node-wide ones to store; raises
    `FtnNetworkError` with the reason. A learned pin is let go when the
    uplink's host changes: another host has another certificate."""
    pin = draft["uplink_tls_pin"]
    try:  # compared as stored: [2001:db8::1]:24554 is the same host as 2001:db8::1
        host = parse_host_port(draft["uplink_host"])[0] if draft["uplink_host"].strip() else ""
    except ValueError:
        host = None  # refused by the save anyway
    if existing is not None and pin == existing.uplink_tls_pin and host != existing.uplink_host:
        pin = ""
    candidate = replace(candidate, uplink_tls=draft["uplink_tls"], uplink_tls_pin=pin)
    try:
        hosts = parse_listen_hosts(draft["listen_host"])
    except ValueError as exc:
        raise FtnNetworkError(f"Answer addresses: {exc}.") from None
    tls_port_text = draft["tls_port"].strip()
    if tls_port_text and not (tls_port_text.isdigit() and 0 < int(tls_port_text) < 65536):
        raise FtnNetworkError("The TLS answer port must be between 1 and 65535, or empty for off.")
    if tls_port_text and int(tls_port_text) == listen_port:
        raise FtnNetworkError("The TLS answer port must differ from the answer port.")
    cert, key = draft["tls_cert"].strip(), draft["tls_key"].strip()
    if tls_port_text and not cert:
        raise FtnNetworkError("Answering over TLS needs a certificate file.")
    for path, label in ((cert, "Certificate file"), (key, "Key file")):
        if path and tls_port_text and not os.path.isfile(path):
            raise FtnNetworkError(f"{label}: {path} is not a file on this machine.")
    return candidate, {HOST_CONFIG_KEY: " ".join(hosts), TLS_PORT_CONFIG_KEY: tls_port_text,
                       TLS_CERT_CONFIG_KEY: cert, TLS_KEY_CONFIG_KEY: key}


# --- end transport ---
# --- the Netmail section (issues #1470 and #1472) ---


def _netmail_draft(network: FtnNetwork | None) -> dict:
    if network is None:
        return {"bounce_unknown": True, "attach_area_id": None, "give_held_unproven": False}
    return {"bounce_unknown": network.bounce_unknown, "attach_area_id": network.attach_area_id,
            "give_held_unproven": network.give_held_unproven}


def _with_netmail_values(network: FtnNetwork, draft: dict) -> FtnNetwork:
    return replace(network, bounce_unknown=bool(draft["bounce_unknown"]), attach_area_id=draft["attach_area_id"],
                   give_held_unproven=bool(draft["give_held_unproven"]))


def _area_name(db: Database, area_id: int | None) -> str:
    if area_id is None:
        return "(none: files are held for you)"
    row = db.connection.execute("SELECT name FROM file_areas WHERE id = ?", (area_id,)).fetchone()
    return sanitize_text(row["name"]) if row is not None else "(removed: files are held for you)"


async def _attach_area_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
    """Pick the file area inbound netmail files are stored in, or none."""
    from netbbs.files.areas import list_file_areas

    areas = await lane.run(list_file_areas)
    none = object()
    picked = await pick_item(
        session, [none, *areas],
        name_of=lambda a: "No area: hold them for me" if a is none else sanitize_text(a.name),
        stable_id_of=lambda a: 0 if a is none else a.id,
        description_of=lambda a: ("Every file sent with a netmail waits in Held items" if a is none
                                  else sanitize_text(a.description or "")),
        title="Store netmail files in which file area?", empty_message="No file areas.",
    )
    if picked is not None:
        draft["attach_area_id"] = None if picked is none else picked.id
        draft["attach_area_label"] = await lane.run(_area_name, draft["attach_area_id"])


def _netmail_fields() -> list[FieldSpec]:
    return [
        FieldSpec(key="bounce_unknown", label="Bounce unknown",
                  render=lambda d: "yes" if d["bounce_unknown"] else "no: to the SysOp",
                  prompt=bool_field("bounce_unknown"), step=bool_step("bounce_unknown"), section="Netmail",
                  brief="Return netmail no account here can take to its sender",
                  help="On by default. Off, such netmail comes to you. Netmail to SysOp always reaches you, and "
                       "a returned netmail, a robot's message or a sender already returned to five times today "
                       "is never returned."),
        FieldSpec(key="attach_area_id", label="Attach area",
                  render=lambda d: d["attach_area_label"],
                  prompt=_attach_area_prompt, section="Netmail",
                  brief="Where files sent with a netmail are stored",
                  help="A file that comes with a netmail is stored here and listed in the letter. Who may read "
                       "this area may download it, and its moderation applies. A Linked area is refused. "
                       "With none, each one is held for you to look at."),
        FieldSpec(key="give_held_unproven", label="Held mail, no password",
                  render=lambda d: "give it" if d["give_held_unproven"] else "no: only to a proven caller",
                  prompt=bool_field("give_held_unproven"), step=bool_step("give_held_unproven"),
                  section="Netmail",
                  brief="Give held mail to callers without a password",
                  help="Off by default. Held netmail waits for its node to call. A node other than your hub "
                       "has no password here, and anyone can claim its address, so with this on a stranger "
                       "pretending to be that node collects its held mail and files, and the real node never "
                       "gets them. Off, held mail goes only in a session with a password (your hub's), or "
                       "expires after 30 days and its sender is told."),
    ]


# --- end Netmail section ---
# --- the Mail bundles section (issue #1477) ---

_BUNDLE_LABELS = {BUNDLE_OFF: "off: bare packets", BUNDLE_ZIP: "ZIP"}


def _bundle_fields() -> list[FieldSpec]:
    choices = list(BUNDLE_MODES)
    return [
        FieldSpec(key="bundle_outgoing", label="Bundle outgoing mail",
                  render=lambda d: _BUNDLE_LABELS[d["bundle_outgoing"]],
                  prompt=choice_field("bundle_outgoing", choices), step=choice_step("bundle_outgoing", choices),
                  section="Mail bundles", brief="Send packets in a ZIP bundle instead of bare",
                  help="Off by default: every hub takes bare packets. ZIP sends each call's packet in one "
                       "bundle named for the day of the week, as most tossers do. Bundles that arrive are "
                       "opened either way: ZIP always, and LHA, ARC, 7-Zip and RAR when their unpacker is "
                       "installed (see the SysOp Handbook)."),
    ]


# --- end Mail bundles section ---


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
        **robot_values(draft),
        nodelist_echo=draft["nodelist_echo"], nodelist_name=draft["nodelist_name"],
        nodediff_name=draft["nodediff_name"],
    )


def _listen_port(db: Database) -> int:
    value = get_config(db, PORT_CONFIG_KEY)
    return int(value) if value and value.isdigit() else DEFAULT_PORT


async def edit_network(session: Session, lane: DatabaseLane, actor: User, existing: FtnNetwork | None, *,
                       node_controls=None) -> FtnNetwork | None:
    """One network's settings in a draft; nothing is stored before [S]ave."""
    draft = _network_draft(existing, await lane.run(_listen_port))
    # --- file echoes (issue #1462) ---
    draft["tic_password"] = existing.tic_password if existing is not None else ""
    # --- end file echoes ---
    draft.update(_netmail_draft(existing))  # issues #1470 and #1472
    draft["bundle_outgoing"] = existing.bundle_outgoing if existing is not None else BUNDLE_OFF  # issue #1477
    draft["attach_area_label"] = await lane.run(_area_name, draft["attach_area_id"])
    secret = lambda key: (lambda d: "set" if d[key] else "(not set)")  # noqa: E731
    fields = [
        FieldSpec(key="enabled", label="Enabled",
                     render=lambda d: "yes" if d["enabled"] else "no", prompt=bool_field("enabled"),
                     step=bool_step("enabled"), section="Network",
                     brief="Call the uplink and carry this network's mail",
                     help="Off by default. While it is off nothing is sent, polled or answered for it. "
                          "It can't be on without the uplink's Host. The running node picks a change up "
                          "within a minute."),
        FieldSpec(key="name", label="Name",
                     render=lambda d: d["name"], prompt=text_field("name", required=True), section="Network",
                     brief="How the network is called here (fsxNet, FidoNet)",
                     help="Up to 40 characters, and no other network here may have it."),
        FieldSpec(key="domain", label="Domain",
                     render=lambda d: d["domain"], prompt=text_field("domain", required=True), section="Network",
                     brief="The network's 5D domain, up to 8 characters (fsxnet)",
                     help="1-8 letters, digits, - or _, stored in lower case. It completes this BBS's and the "
                          "uplink's addresses: 21:1/199@fsxnet."),
        FieldSpec(key="our_address", label="Our address",
                     render=lambda d: d["our_address"] or "(not set)", prompt=text_field("our_address"),
                     section="Network", brief="zone:net/node[.point] the network gave this BBS",
                     help="Everything this BBS sends on the network carries it, and must differ from the "
                          "uplink's. A point address (21:1/199.3) makes this BBS a point: it then can't have "
                          "points or downlinks, and publishes nothing."),
        FieldSpec(key="netmail_min_level",
                     label="Netmail level", render=lambda d: str(d["netmail_min_level"]),
                     prompt=_int_field("netmail_min_level", "Netmail level"), section="Network",
                     brief="Who may send netmail; 255 = SysOp only",
                     help="Callers below this level can't send netmail on this network. Netmail leaves under "
                          "this BBS's address, so it starts at SysOp only."),
        FieldSpec(key="default_charset",
                     label="Charset", render=lambda d: d["default_charset"],
                     prompt=choice_field("default_charset", CHARSETS), step=choice_step("default_charset", CHARSETS),
                     section="Network", brief="How to read a message that names no character set",
                     help="Used for an arriving message without a CHRS line, from the next toss on. What this "
                          "BBS sends is CP437 when it fits and UTF-8 otherwise, whatever this says."),
        FieldSpec(key="origin_text", label="Origin",
                     render=lambda d: d["origin_text"] or "(the BBS's name)", prompt=_optional_text_field("origin_text"),
                     section="Network", brief="The line under every post sent out",
                     help="' * Origin: ', this text and this BBS's address end every post sent out. Up to 60 "
                          "characters, shortened if the line would pass 79. A board's own Origin wins over it."),
        FieldSpec(key="uplink_address", label="Uplink",
                     render=lambda d: d["uplink_address"], prompt=text_field("uplink_address", required=True),
                     section="Uplink", brief="The hub's address (fsxNet: 21:1/100)",
                     help="This BBS's echomail and netmail go to it. A caller presenting this address must "
                          "prove the session password."),
        FieldSpec(key="uplink_host", label="Host",
                     render=lambda d: d["uplink_host"] or "(not set)", prompt=text_field("uplink_host"),
                     section="Uplink", brief="Where the hub answers BinkP",
                     help="A host name or IP address. A port written here wins over Port: host:24554, or "
                          "[2001:db8::1]:24554 for IPv6."),
        FieldSpec(key="uplink_port", label="Port",
                     render=lambda d: str(d["uplink_port"]), prompt=_int_field("uplink_port", "Port"),
                     section="Uplink", brief="Usually 24554",
                     help="24554 is BinkP's usual port, 24553 its TLS port (see Call over TLS)."),
        FieldSpec(key="session_password",
                     label="Session password", render=secret("session_password"),
                     prompt=_secret_field("session_password", "Session password"), section="Uplink",
                     brief="Proves this BBS to the hub (CRAM-MD5 when offered)",
                     help="Agreed with the hub, and proves the hub to this BBS when it calls in. Without one "
                          "every session is unsecure, and everything the hub sends is held for you."),
        FieldSpec(key="packet_password",
                     label="Packet password", render=secret("packet_password"),
                     prompt=_secret_field("packet_password", "Packet password"), section="Uplink",
                     brief="Up to 8 characters, if the hub uses one",
                     help="Written into every packet sent to the hub, and checked, without case, on every "
                          "packet from it: a packet with another one is held. Empty: nothing is checked."),
        FieldSpec(key="areafix_password",
                     label="AreaFix password", render=secret("areafix_password"),
                     prompt=_secret_field("areafix_password", "AreaFix password"), section="Uplink",
                     brief="For linking echo areas",
                     help="The subject of every AreaFix request this BBS sends, and of FileFix's while FileFix "
                          "has no password of its own. Changing it here tells the hub nothing: [R]obot password "
                          "on the networks list changes it at the hub too."),
        FieldSpec(key="poll_minutes", label="Poll every",
                     render=lambda d: f"{d['poll_minutes']} minutes", prompt=_int_field("poll_minutes", "Minutes"),
                     section="Calls", brief="How often to call the hub; at least once a day",
                     help="5 to 1440 minutes. When mail waits, the hub is also called within five minutes; a "
                          "failed call is tried again after 1, 2, 4 ... minutes."),
        FieldSpec(key="answers_calls", label="Answer calls",
                     render=lambda d: "yes" if d["answers_calls"] else "no", prompt=bool_field("answers_calls"),
                     step=bool_step("answers_calls"), section="Calls",
                     brief="Let the hub (and other nodes) call this BBS",
                     help="Needs the answer port open to the internet; points and downlinks need it to call "
                          "in. Hubs deliver crash mail sooner. A caller that proves no password may only "
                          "deliver, and what it brings is held."),
        FieldSpec(key="listen_port",
                     label="Answer port", render=lambda d: f"{d['listen_port']} (every network)",
                     prompt=_int_field("listen_port", "Answer port"), section="Calls",
                     brief="Where this BBS answers BinkP, for all networks",
                     help="24554 by default. It is one port for every network that answers calls; the running "
                          "node moves to a new one within a minute."),
    ]
    fields += robot_field_specs(_secret_field)  # issue #1463: the Robots section
    fields += _nodelist_fields()  # issue #1464: the Nodelist section, after Robots
    draft.update(await lane.run(lambda db: _transport_draft(db, existing)))  # issues #1467, #1468
    # --- file echoes (issue #1462): the File echoes section, after Nodelist ---
    fields.append(
        FieldSpec(key="tic_password",
                     label="TIC password", render=secret("tic_password"),
                     prompt=_secret_field("tic_password", "TIC password"), section="File echoes",
                     brief="The Pw line of the hub's TIC files",
                     help="File echoes arrive with a TIC file carrying this password, and the files you "
                          "hatch or publish carry it. With none set, every file echo is held for you to look "
                          "at, and nothing can be hatched."))
    # --- end file echoes ---
    fields += _transport_fields()  # issues #1467, #1468: the Transport section, after File echoes
    fields += _netmail_fields()  # issues #1470 and #1472: the Netmail section, after File echoes
    fields += _bundle_fields()  # issue #1477: the Mail bundles section, after Netmail
    # --- file requests (issue #1475): the File requests section, after Netmail ---
    draft.update(await lane.run(freq_draft, existing))
    fields += freq_fields()
    # --- end file requests ---

    async def save(draft: dict) -> FtnNetwork:
        candidate = _network_from_draft(draft, existing)
        candidate = replace(candidate, tic_password=draft["tic_password"])  # issue #1462
        candidate = _with_netmail_values(candidate, draft)  # issues #1470 and #1472
        candidate = replace(candidate, bundle_outgoing=draft["bundle_outgoing"])  # issue #1477
        port = int(draft["listen_port"])
        if not 0 < port < 65536:
            raise FtnNetworkError("The answer port must be between 1 and 65535.")
        candidate, node_wide = _transport_values(draft, candidate, port, existing)  # issues #1467, #1468
        # --- file requests (issue #1475): checked before anything is stored ---
        from netbbs.ftn.freq import FreqError, validate_settings

        try:
            validate_settings(freq_settings_from_draft(draft, 0))
        except FreqError as exc:
            raise FtnNetworkError(str(exc)) from None
        # --- end file requests ---

        def _persist(db: Database) -> FtnNetwork:
            # --- netmail extras (issue #1470): a Linked area can't take netmail files ---
            from netbbs.ftn.attach import attach_area_problem

            problem = attach_area_problem(db, candidate.attach_area_id)
            if problem is not None:
                raise FtnNetworkError(problem)
            # --- end netmail extras ---
            # The row, the node-wide listener keys and the audit entry
            # naming them commit together or not at all (issue #1505).
            try:
                saved = save_network(db, candidate, commit=False)
                # --- file requests (issue #1475) ---
                save_freq_settings_without_commit(db, draft, saved.id)
                # --- end file requests ---
                node_keys = {PORT_CONFIG_KEY: str(port), **node_wide}  # issues #1467, #1468
                for key, value in node_keys.items():
                    set_config_without_commit(db, key, value)
                record_action_without_commit(
                    db, actor=actor, action="set_ftn_network",
                    detail=(f"{saved.name}: {saved.our_address} via {saved.uplink_address} "
                            f"{saved.uplink_host}:{saved.uplink_port} tls={saved.uplink_tls or 'off'} "
                            f"enabled={saved.enabled} "
                            f"answers={saved.answers_calls} netmail_level={saved.netmail_min_level} "
                            + " ".join(f"{key}={value or '(none)'}" for key, value in node_keys.items())),
                )
            except BaseException:
                db.connection.rollback()
                raise
            db.connection.commit()
            return saved

        return await lane.run(_persist)

    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=existing.name if existing is not None else "New FTN network",
        fields=fields, draft=draft, save=save, error_type=FtnNetworkError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=("A new network starts as fsxNet's main hub: fill in the address fsxNet gave you."
                  if existing is None else None),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, f"Saved {saved.name}. The mailer and the listener pick it up within a minute.")
        mailer = getattr(node_controls, "ftn_mailer", None) if node_controls is not None else None
        if mailer is not None:
            mailer.kick()
    return saved


# --- Node → Echomail & netmail (FTN) ---------------------------------------------


def _status_sections(db: Database, mailer, listener, *, unicode_style: bool) -> list[Section]:
    sections = []
    for network in list_networks(db):
        rows: list = [Field("State", "on" if network.enabled else "off")]
        status = mailer.status.get(network.id) if mailer is not None else None
        if status is not None and status.last_success_at:
            rows.append(Field("Last call", f"{status.last_success_at}: {status.last_summary}"))
        if status is not None and status.last_error:
            rows.append(Field("Last error", sanitize_text(status.last_error)))
        # --- netmail extras (issues #1470 and #1471): files and held mail too ---
        messages, files_waiting, on_hold = count_waiting(db, network.id)
        rows.append(Field("Waiting", f"{messages} messages"
                          + (f", {files_waiting} files" if files_waiting else "")
                          + (f" ({on_hold} on hold for their node to call)" if on_hold else "")))
        # --- end netmail extras ---
        # --- transport (issue #1468) ---
        if network.uplink_tls:
            rows.append(Field("TLS", _TLS_LABELS[network.uplink_tls] + (
                f" ({show_fingerprint(network.uplink_tls_pin)})" if network.uplink_tls == TLS_PINNED
                and network.uplink_tls_pin else "")))
        # --- end transport ---
        # The same items [H]eld items lists: this network's, and those a
        # deleted network left behind.
        held = [h for h in list_held(db) if h.network_id in (network.id, None)]
        files = sum(1 for h in held if h.kind != HELD_PACKET)
        rows.append(Field("Held", f"{len(held)} items" + (f", {files} of them files or TICs" if files else "")))
        # --- bad echoes (issue #1466) ---
        echoes, file_echoes = list_bad_echoes(db, network.id), list_bad_file_echoes(db, network.id)
        dropped = bad_echo_dropped(db, network.id)
        if echoes or file_echoes or dropped:
            parts = []
            if echoes:
                parts.append(f"{sum(e.count for e in echoes)} messages in {len(echoes)} echoes")
            if file_echoes:
                parts.append(f"{sum(e.count for e in file_echoes)} items in {len(file_echoes)} file echoes")
            if dropped:
                parts.append(f"{dropped} oldest dropped (store full)")
            rows.append(Field("Unknown echoes", "; ".join(parts)))
        # --- end bad echoes ---
        rows.extend(_nodelist_rows(db, network))
        rows.extend(freq_status_rows(db, network))  # issue #1475
        # --- points and downlinks (issues #1478, #1479) ---
        from netbbs.net.ftn_links_console import links_summary

        links = links_summary(db, network)
        if links:
            rows.append(Field("Links", links))
        # --- end points and downlinks ---
        sections.append(Section(f"{network.name} ({network.our_address.four_d})", rows))
    if not sections:
        sections.append(Section(None, [Note("No FTN networks yet: add one under Settings → Echomail & netmail (FTN).")]))
    if mailer is None:
        sections.append(Section(None, [Note("The mailer runs in the node; this console shows no live state.")]))
    if listener is not None:
        where = listener.listening_on
        rows = [Field("Answering", f"port {where[1]}" if where else "no")]
        # --- transport (issues #1467, #1468) ---
        if where:
            addresses = getattr(listener, "addresses", ())
            rows.append(Field("On", ", ".join(addresses) if addresses else "every address, IPv6 and IPv4"))
            tls_port = getattr(listener, "tls_listening_on", None)
            if tls_port:
                rows.append(Field("TLS", f"port {tls_port}"))
        if getattr(listener, "tls_error", None):
            rows.append(Field("TLS error", sanitize_text(listener.tls_error)))
        # --- end transport ---
        if listener.last_error:
            rows.append(Field("Error", sanitize_text(listener.last_error)))
        for call in list(listener.recent)[-5:]:
            rows.append(Field(call.at[:19], sanitize_text(f"{call.addresses or call.peer}: {call.outcome}")))
        sections.append(Section("Calls answered", rows))
    return sections


# --- the nodelist's status (issue #1464) ---

_HOW = {"full": "full list", "diff": "diff", "manual": "by hand"}


def _nodelist_rows(db: Database, network: FtnNetwork) -> list:
    status = nodelist_status(db, network.id)
    rows: list = []
    if status is None or not status.imported_at:
        rows.append(Field("Nodelist", "none imported: netmail goes via the uplink"))
    else:
        day = f"day {status.day:03d}, " if status.day is not None else ""
        rows.append(Field("Nodelist", f"{day}{status.entries} nodes"))
        rows.append(Field("Imported", f"{status.imported_at} ({_HOW.get(status.how or '', 'by hand')})"))
        rows.append(Field("CRC-16", f"{status.crc:05d} verified" if status.crc is not None
                          else "none in the first line"))
    if network.nodelist_echo:
        names = f"{network.nodelist_name}.nnn" + (f", diffs {network.nodediff_name}.nnn"
                                                  if network.nodediff_name else "")
        rows.append(Field("Nodelist echo", f"{network.nodelist_echo}: {names}"))
    if status is not None and status.waiting_for_full:
        rows.append(Field("Waiting", "for a full list: a diff could not be applied"))
    if status is not None and status.failure:
        rows.append(Field("Last failure", sanitize_text(f"{status.failed_at}: {status.failure}")))
    return rows


# --- end nodelist status ---


_STATUS_HELP = [
    MenuEntry(label=menu_key("P", "oll now"), brief="Call the hub now, whatever the poll interval"),
    MenuEntry(label=menu_key("A", "reaFix"), brief="Send the hub's AreaFix one line: +TAG, -TAG, %LIST"),
    MenuEntry(label=menu_key("F", "ileFix"), brief="The same for file echoes"),
    MenuEntry(label=menu_key("I", "mport echo list"), brief="Boards or file areas from a .NA or .FA file"),
    MenuEntry(label=menu_key("N", "odelist import"), brief="Import a nodelist file from this machine"),
    MenuEntry(label=menu_key("H", "eld items"), brief="Release, save, export or delete what was held"),
    MenuEntry(label=menu_key("U", "nknown echoes"), brief="Carry or drop echoes no board or area carries"),
    MenuEntry(label=menu_key("R", "equest files"), brief="Ask a node for files; they come to Held items"),
]


@located("Echomail & netmail (FTN)")
async def ftn_status_screen(session: Session, lane: DatabaseLane, actor: User, node_controls) -> None:
    """What the FTN gateway last did, and the work on it."""
    mailer = getattr(node_controls, "ftn_mailer", None)
    listener = getattr(node_controls, "ftn_listener", None)
    message = None
    page = 0
    while True:
        chrome = await _load_chrome(lane, actor)
        sections = await lane.run(lambda db: _status_sections(db, mailer, listener, unicode_style=chrome.unicode_style))
        choice, page = await show_detail(
            session, title=_detail_title(session, chrome, "Echomail & netmail (FTN)", own=True),
            sections=sections, page=page, message=message,
            actions=[("p", menu_key("P", "oll now")), ("a", menu_key("A", "reaFix")), ("f", menu_key("F", "ileFix")),
                     ("i", menu_key("I", "mport echo list")),
                     ("n", menu_key("N", "odelist import")), ("h", menu_key("H", "eld items")),
                     ("u", menu_key("U", "nknown echoes")), ("r", menu_key("R", "equest files")), _BACK_ACTION],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Echomail & netmail help",
            help_about=(
                "Each network's last call and error, what waits for it (on hold: kept until its node calls), "
                "held items, unknown echoes, its nodelist, file requests and links. Calls answered is the "
                "listener's ports and its last five calls. With more than one network, each key asks which."
            ),
            help_entries=_STATUS_HELP,
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
                       else colored(f"Call failed: {sanitize_text(status.last_error)}", fg_color=ERROR_COLOR))
        elif choice in ("a", "f"):  # issue #1463
            await robot_request(session, lane, actor, network, mailer, AREAFIX if choice == "a" else FILEFIX)
        elif choice == "i":  # issue #1465
            await import_echolist(session, lane, actor, network, mailer)
        elif choice == "n":
            await _import_nodelist(session, lane, actor, network)
        elif choice == "h":
            await _held_packets(session, lane, actor, network)
        elif choice == "u":
            await _bad_echoes(session, lane, actor, network)
        elif choice == "r":  # issue #1475
            await request_files(session, lane, actor, network, mailer)


async def _pick_network(session: Session, lane: DatabaseLane, actor: User) -> FtnNetwork | None:
    networks = await lane.run(list_networks)
    if len(networks) <= 1:
        if not networks:
            _announce(session, "No FTN networks yet: add one under Settings → Echomail & netmail (FTN).", error=True)
        return networks[0] if networks else None
    chrome = await _load_chrome(lane, actor)
    return await pick_item(
        session, networks, name_of=lambda n: n.name, stable_id_of=lambda n: n.id,
        description_of=lambda n: n.our_address.four_d,
        title="Which network?", empty_message="No networks.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )


async def _import_nodelist(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    await session.write_line("")
    await session.write_line(colored(
        f"The {network.name} nodelist, as a file on this machine: the list itself (e.g. "
        f"/bbs/nodelist/FSXNET.123) or a ZIP holding it.",
        fg_color=MUTED_COLOR))
    await write_prompt(session, "Nodelist file: ")
    try:
        path_text = (await session.read_line(cancellable=True)).strip()
    except InputCancelled:
        path_text = ""
    if not path_text:
        _announce(session, "Nothing imported.", color=MUTED_COLOR)
        return
    try:
        outcome = await import_nodelist_file(lane, actor, network, Path(os.path.expanduser(path_text)))
    except (OSError, NodelistError) as exc:
        _announce(session, f"Not imported: {exc}", error=True)
        return
    day = f" (day {outcome.day:03d})" if outcome.day is not None else ""
    _announce(session, f"Imported {outcome.nodes} nodes for {network.name}{day}. "
                       f"Netmail to listed BinkP nodes now goes direct.")


async def import_nodelist_file(lane: DatabaseLane, actor: User | None, network: FtnNetwork,
                               path: Path) -> ImportOutcome:
    """Read and import a nodelist file, or the list in a ZIP; the CRC-16 is
    checked when the first line carries one (issue #1464)."""
    data = await asyncio.to_thread(read_list_file, path, network)

    def _persist(db: Database) -> ImportOutcome:
        outcome = import_list(db, network.id, data, how=HOW_MANUAL, require_crc=False)
        record_action(db, actor=actor, action="import_ftn_nodelist",
                      detail=f"{network.name}: {outcome.nodes} nodes from {path.name}")
        return outcome

    return await lane.run(_persist)


async def _held_packets(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    """The held items: packets, files and TICs. [R]elease re-tosses a
    packet, or opens a held mail bundle and tosses its packets (issue #1477); a file or TIC is [S]aved to a file area or [E]xported to the
    host instead (issue #1460)."""
    status: str | None = None

    async def _load():
        return [h for h in await lane.run(list_held) if h.network_id in (network.id, None)]

    async def _release(held):
        nonlocal status
        try:
            if held.kind != HELD_FILE:
                result = await lane.run(lambda db: release_held(db, get_network(db, network.id), held.id))
            else:
                # --- mail bundles (issue #1477): a held bundle is opened now ---
                from netbbs.ftn.exchange import release_held_bundle

                fresh = await lane.run(get_network, network.id)
                result = await release_held_bundle(lane, fresh or network, held.id)
                # --- end mail bundles ---
        except FtnFormatError as exc:
            status = f"Not released: {sanitize_text(str(exc))}."
            return await _load()
        await lane.run(lambda db: record_action(db, actor=actor, action="release_ftn_packet",
                                                detail=f"{network.name}: {held.file_name} from {held.remote_address}"))
        status = (f"Released {sanitize_text(held.file_name)}: {result.posts} posts, {result.netmail} netmail, "
                  f"{result.duplicates} duplicates" + (f", held again: {sanitize_text(result.refused_packet)}"
                                                       if result.refused_packet else ""))
        return await _load()

    async def _save(held):
        nonlocal status
        status = await _save_held_to_area(session, lane, actor, held)
        return await _load()

    async def _export(held):
        nonlocal status
        status = await _export_held(session, lane, actor, network, held)
        return await _load()

    async def _delete(held):
        nonlocal status
        await lane.run(delete_held, held.id)
        status = f"Deleted {sanitize_text(held.file_name)}."
        return await _load()

    chrome = await _load_chrome(lane, actor)
    await _pick_item(
        session, await _load(),
        name_of=lambda h: sanitize_text(f"{h.file_name} from {h.remote_address}"),
        stable_id_of=lambda h: h.id,
        description_of=lambda h: (f"{_held_kind_label(h)}, {h.size} bytes, {h.received_at[:19]}: "
                                  f"{sanitize_text(h.reason)}"),
        title=f"Held items ({network.name})",
        empty_message="Nothing is held.",
        refresh=_load, item_keys={"r": _release, "s": _save, "e": _export, "d": _delete},
        live_nav=[MenuEntry(label=menu_key("R", "elease"), brief="Toss a packet or a mail bundle as from a known system"),
                  MenuEntry(label=menu_key("S", "ave"), brief="Store a file in a file area"),
                  MenuEntry(label=menu_key("E", "xport"), brief="Write a file to this machine"),
                  MenuEntry(label=menu_key("D", "elete"), brief="Throw it away")],
        live_label=lambda: status or "Held: from an unknown caller, or what could not be stored",
        help_about=("What a call brought that was not tossed or stored, for this network or a deleted one: its "
                    "kind, size, when it came and why. A file or TIC waiting for its other half is paired when "
                    "that arrives, or deleted after 7 days; everything else stays until you act on it."),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )


def _held_kind_label(held) -> str:
    if held.kind == HELD_PACKET:
        return "packet"
    if held.kind == "tic":
        return "TIC" + (", waiting for its file" if held.waiting else "")
    return ("file with its TIC" if held.has_tic else "file") + (", waiting for its TIC" if held.waiting else "")


async def _save_held_to_area(session: Session, lane: DatabaseLane, actor: User, held) -> str:
    """[S]ave: the SysOp picks a file area and the held item becomes their
    own upload there; the outcome, for the screen's status line."""
    from netbbs.files.areas import list_file_areas
    from netbbs.files.diz import read_archive_description
    from netbbs.files.entries import FileEntryError
    from netbbs.files.storage import store_bytes
    from netbbs.ftn.fileecho import HeldItemError, held_file_for_saving, save_held_to_area

    areas = await lane.run(list_file_areas)
    if not areas:
        return "No file areas yet: create one under Content → File areas first."
    chrome = await _load_chrome(lane, actor)
    area = await pick_item(
        session, areas, name_of=lambda a: sanitize_text(a.name), stable_id_of=lambda a: a.id,
        description_of=lambda a: sanitize_text(a.description or ""),
        title=f"Save {sanitize_text(held.file_name)} to which file area?", empty_message="No file areas.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if area is None:
        return "Nothing saved."
    try:
        item = await lane.run(held_file_for_saving, held.id)
        description = item.description
        if description is None:
            _sha256, path = await lane.run(store_bytes, item.data)
            description = await read_archive_description(path, item.name)
        entry = await lane.run(lambda db: save_held_to_area(db, held.id, area, actor, name=item.name,
                                                            description=description))
    except (HeldItemError, FileEntryError) as exc:
        return f"Not saved: {sanitize_text(str(exc))}"
    return (f"Saved {sanitize_text(entry.filename)} to {sanitize_text(area.name)}"
            + (", waiting for approval." if entry.status == "pending" else "."))


async def _export_held(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, held) -> str:
    """[E]xport: one path on the host, a folder or the file to write. The
    rules are a SysOp upload's (§6.2): nothing overwritten, no symbolic
    link followed, audit-logged. The item stays held."""
    from netbbs.ftn.fileecho import HeldItemError, export_held, held_file_for_saving

    await session.write_line("")
    await session.write_line(colored(
        "A path on this machine: a folder, or the file to write. An existing file is never overwritten.",
        fg_color=MUTED_COLOR))
    await write_prompt(session, "Export to: ")
    try:
        text = (await session.read_line(cancellable=True)).strip()
    except InputCancelled:
        text = ""
    if not text:
        return "Nothing exported."
    try:
        item = await lane.run(held_file_for_saving, held.id)
    except HeldItemError as exc:
        return f"Not exported: {exc}"
    target = Path(os.path.expanduser(text))

    def _resolve() -> Path:
        if not target.is_symlink() and target.is_dir():
            return target / item.name
        return target

    try:
        written = await asyncio.to_thread(lambda: export_held(item.data, _resolve()))
    except (HeldItemError, OSError, ValueError) as exc:  # ValueError: a NUL in the typed path
        return f"Not exported: {sanitize_text(str(exc))}"
    await lane.run(lambda db: record_action(
        db, actor=actor, action="export_ftn_held",
        detail=f"{network.name}: held {item.name} ({len(item.data)} bytes) exported to {written}"))
    return f"Exported to {sanitize_text(str(written))}. It stays held until you [D]elete it."


# --- bad echoes (issue #1466) -------------------------------------------------


async def _bad_echoes(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork) -> None:
    """Tags no board or file area carries, one row each: [C]reate a board
    or file area for one, [M]ap an existing one, or [D]elete what waits.
    Creating or mapping tosses the tag's messages again, or runs its file
    pairs through the TIC checks again."""
    status: str | None = None

    async def _load():
        def _rows(db: Database):
            rows = [("echo", e.tag, e.count, e.first_seen, e.last_seen) for e in list_bad_echoes(db, network.id)]
            rows += [("file", e.tag, e.count, e.first_seen, e.last_seen) for e in list_bad_file_echoes(db, network.id)]
            return rows
        return await lane.run(_rows)

    async def _create(row):
        nonlocal status
        status = await _create_for_bad_echo(session, lane, actor, network, row)
        return await _load()

    async def _map(row):
        nonlocal status
        status = await _map_bad_echo(session, lane, actor, network, row)
        return await _load()

    async def _delete(row):
        nonlocal status
        kind, tag, count = row[0], row[1], row[2]
        await session.write_line("")
        what = "messages" if kind == "echo" else "held files and TICs"
        if not await prompt_yes_no(session, f"Delete the {count} {what} waiting for {sanitize_text(tag)}?",
                                   default=False):
            return None

        def _persist(db: Database) -> None:
            if kind == "echo":
                delete_bad_echo(db, network.id, tag)
            else:
                for held_id in held_bad_file_echo_ids(db, network.id, tag):
                    delete_held(db, held_id)
            record_action(db, actor=actor, action="delete_ftn_bad_echo",
                          detail=f"{network.name}: {count} {what} for {tag}")

        await lane.run(_persist)
        status = f"Deleted what waited for {sanitize_text(tag)}."
        return await _load()

    chrome = await _load_chrome(lane, actor)
    await _pick_item(
        session, await _load(),
        name_of=lambda r: sanitize_text(r[1]) + (" (file echo)" if r[0] == "file" else ""),
        stable_id_of=lambda r: f"{r[0]}:{r[1]}",
        description_of=lambda r: (f"{r[2]} {'messages' if r[0] == 'echo' else 'held'}, "
                                  f"first {r[3][:16]}, last {r[4][:16]}"),
        title=f"Unknown echoes ({network.name})",
        empty_message="No unknown echoes: everything that came is carried here.",
        refresh=_load, item_keys={"c": _create, "m": _map, "d": _delete},
        live_nav=[MenuEntry(label=menu_key("C", "reate"), brief="A new board or file area for it"),
                  MenuEntry(label=menu_key("M", "ap"), brief="Carry it on an existing one",
                            detailed="Carry it on a local board or area that carries no echo"),
                  MenuEntry(label=menu_key("D", "elete"), brief="Drop what waits")],
        live_label=lambda: status or "Echoes the hub sends that no board or file area carries",
        help_about=("Tags the hub sends that no board or file area here carries, with how many wait and when "
                    "the first and last came. Create and Map toss what waited, through the board's moderation "
                    "or the TIC checks. At most 1,000 messages wait per tag and 10,000 in all; the oldest go first."),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )


async def _create_for_bad_echo(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, row) -> str:
    kind, tag = row[0], row[1]
    if kind == "echo":
        from netbbs.net.admin_boards import _board_screen  # it imports this module

        created = await _board_screen(session, lane, actor, initial_name=tag)
    else:
        from netbbs.net.admin_files import _area_screen  # it imports this module

        created = await _area_screen(session, lane, actor, initial_name=tag)
    if created is None:
        return "Nothing created."
    return await _carry_bad_echo(lane, actor, network, kind, tag, created)


async def _map_bad_echo(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, row) -> str:
    kind, tag = row[0], row[1]

    def _candidates(db: Database):
        if kind == "echo":
            from netbbs.boards.boards import list_boards
            from netbbs.link.boards import is_board_linked

            return [b for b in list_boards(db) if board_area(db, b) is None and not is_board_linked(db, b)]
        from netbbs.files.areas import list_file_areas
        from netbbs.link.files import is_area_linked

        return [a for a in list_file_areas(db) if file_area_tic(db, a) is None and not is_area_linked(db, a)]

    candidates = await lane.run(_candidates)
    noun = "board" if kind == "echo" else "file area"
    if not candidates:
        return f"Every {noun} here is Linked or carries an echo already: [C]reate one instead."
    chrome = await _load_chrome(lane, actor)
    picked = await pick_item(
        session, candidates, name_of=lambda c: sanitize_text(c.name), stable_id_of=lambda c: c.id,
        description_of=lambda c: sanitize_text(c.description or ""),
        title=f"Which local {noun} carries {sanitize_text(tag)}?", empty_message=f"No local {noun}s.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if picked is None:
        return "Nothing mapped."
    return await _carry_bad_echo(lane, actor, network, kind, tag, picked)


async def _carry_bad_echo(lane: DatabaseLane, actor: User, network: FtnNetwork, kind: str, tag: str, target) -> str:
    """Map `target` (a board or a file area) to `tag`, then toss again what
    waited for it; the outcome for the status line."""
    from netbbs.ftn.exchange import rerun_bad_file_echo

    try:
        if kind == "echo":
            await lane.run(set_board_area, target, network.id, tag)
        else:
            await lane.run(set_file_area_tic, target, network.id, tag)
    except FtnNetworkError as exc:
        return f"Not mapped: {exc}"
    fresh = await lane.run(get_network, network.id)
    if kind == "echo":
        result: TossResult = await lane.run(lambda db: retoss_bad_echo(db, fresh, tag))
        outcome = (f"{sanitize_text(target.name)} carries {sanitize_text(tag)}: {result.posts} posts tossed, "
                   f"{result.duplicates} duplicates, {result.held} held")
    else:
        result = await rerun_bad_file_echo(lane, fresh, tag)
        outcome = (f"{sanitize_text(target.name)} carries {sanitize_text(tag)}: {result.files} files stored, "
                   f"{result.file_duplicates} duplicates, {result.held} still held")
    await lane.run(lambda db: record_action(
        db, actor=actor, action="set_board_echo" if kind == "echo" else "set_file_area_echo",
        detail=f"{network.name}: {target.name} carries unknown echo {tag}"))
    return outcome + "."


# --- a board's echo ------------------------------------------------------------


def board_echo_rows(db: Database, board: Board) -> list[Field]:
    """The board screen's FTN rows: the echo it carries, if any."""
    area = board_area(db, board)
    if area is None:
        return [Field("Echo", "none (a local board)")]
    network = get_network(db, area.network_id)
    return [Field("Echo", f"{area.tag} on {network.name if network else '?'}"),
            *board_origin_rows(db, area, network)]


async def board_echo_action(session: Session, lane: DatabaseLane, actor: User, board: Board) -> bool:
    """[E]cho on a board's screen: carry an echo area, or stop carrying it
    (an empty tag). Returns whether anything changed."""
    networks = await lane.run(list_networks)
    if not networks:
        _announce(session, "No FTN networks yet: add one under Settings → Echomail & netmail (FTN).", error=True)
        return False
    network = networks[0] if len(networks) == 1 else await _pick_network(session, lane, actor)
    if network is None:
        return False
    current = await lane.run(board_area, board)
    await session.write_line("")
    await session.write_line(colored(
        f"The {network.name} echo tag this board carries (e.g. FSX_GEN). An empty line makes it local again; "
        f"link the echo at the hub with AreaFix (Node → Echomail & netmail (FTN)).", fg_color=MUTED_COLOR))
    await write_prompt(session, "Echo tag: ")
    try:
        tag = (await session.read_line(cancellable=True,
                                       initial=current.tag if current and current.network_id == network.id else "")).strip()
    except InputCancelled:
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
        _announce(session, str(exc), error=True)
        return False
    await lane.run(lambda db: record_action(db, actor=actor, action="set_board_echo", detail=outcome))
    _announce(session, outcome)
    return True


# --- a file area's file echo (issue #1462) -------------------------------------


def file_area_echo_rows(db: Database, area) -> list[Field]:
    """The file area screen's FTN rows: the file echo it carries, if any."""
    mapping = file_area_tic(db, area)
    if mapping is None:
        return [Field("File echo", "none (a local area)")]
    network = get_network(db, mapping.network_id)
    return [Field("File echo", f"{mapping.tag} on {network.name if network else '?'}")]


async def file_area_echo_action(session: Session, lane: DatabaseLane, actor: User, area) -> bool:
    """[F]ile echo on a file area's screen: carry a file echo, or stop
    carrying it (an empty tag). Returns whether anything changed."""
    networks = await lane.run(list_networks)
    if not networks:
        _announce(session, "No FTN networks yet: add one under Settings → Echomail & netmail (FTN).", error=True)
        return False
    network = networks[0] if len(networks) == 1 else await _pick_network(session, lane, actor)
    if network is None:
        return False
    current = await lane.run(file_area_tic, area)
    await session.write_line("")
    await session.write_line(colored(
        f"The {network.name} file echo this area carries (e.g. FSX_INFO). An empty line makes it local again; "
        f"link the file echo at the hub with FileFix. Files arrive through this area's own moderation.",
        fg_color=MUTED_COLOR))
    await write_prompt(session, "File echo tag: ")
    try:
        tag = (await session.read_line(cancellable=True,
                                       initial=current.tag if current and current.network_id == network.id else "")).strip()
    except InputCancelled:
        return False
    try:
        if not tag:
            if current is None:
                return False
            await lane.run(clear_file_area_tic, area)
            outcome = f"{area.name} is a local file area again; its files stay."
        else:
            mapping = await lane.run(set_file_area_tic, area, network.id, tag)
            outcome = f"{area.name} carries file echo {mapping.tag} on {network.name}."
    except FtnNetworkError as exc:
        _announce(session, str(exc), error=True)
        return False
    await lane.run(lambda db: record_action(db, actor=actor, action="set_file_area_echo", detail=outcome))
    _announce(session, outcome)
    return True


# --- hatching (issue #1476) ------------------------------------------------------


def file_area_hatch_rows(db: Database, area) -> list[Field]:
    """The file area screen's Auto-hatch row, for an area carrying a file
    echo; none for any other."""
    from netbbs.ftn.hatch import auto_hatch_enabled

    if file_area_tic(db, area) is None:
        return []
    if auto_hatch_enabled(db, area.id):
        return [Field("Auto-hatch", "on: approved uploads are hatched into the file echo")]
    return [Field("Auto-hatch", "off: files are hatched by hand with [H]atch on the listing")]


async def auto_hatch_action(session: Session, lane: DatabaseLane, actor: User, area) -> bool:
    """[A]uto-hatch on the screen of a file area carrying a file echo: a
    toggle. Returns whether anything changed."""
    from netbbs.ftn.hatch import auto_hatch_enabled, set_auto_hatch

    if await lane.run(file_area_tic, area) is None:
        _announce(session, f"{area.name} carries no file echo, so it has nothing to hatch into.", error=True)
        return False
    enabled = not await lane.run(auto_hatch_enabled, area.id)
    await lane.run(lambda db: set_auto_hatch(db, area.id, enabled, actor=actor))
    _announce(session, f"Auto-hatch on: {area.name}'s uploads are hatched into its file echo once approved."
              if enabled else f"Auto-hatch off: {area.name}'s files are hatched only by hand.")
    return True


async def hatch_action(session: Session, lane: DatabaseLane, actor: User, entry) -> bool:
    """[H]atch on a file area's listing: send `entry` into the area's file
    echo, after one yes/no (it goes to the network). A file hatched before
    is hatched again only when the SysOp says so, with `Replaces`. Returns
    whether it was hatched; the outcome is announced either way."""
    from netbbs.ftn.hatch import HatchError, hatch_file, hatch_target
    from netbbs.ftn.queue import FtnQueueFullError

    name = sanitize_text(entry.filename)
    try:
        target = await lane.run(hatch_target, entry)
    except HatchError as exc:
        _announce(session, f"Not hatched: {exc}", error=True)
        return False
    uplink = target.network.uplink_address.four_d
    await session.write_line("")
    if target.previous is not None:
        how = "automatically" if target.previous.automatic else "by hand"
        question = (f"{name} was already hatched into {target.tag} {how} on {target.previous.hatched_at[:10]}. "
                    f"Hatch it again, replacing that copy?")
    else:
        question = f"Hatch {name} into {target.tag} on {target.network.name}? It goes to the uplink {uplink}."
    if not await prompt_yes_no(session, question, default=False):
        _announce(session, "Nothing was hatched.", color=MUTED_COLOR)
        return False
    try:
        hatched = await lane.run(hatch_file, entry.file_id, actor=actor, again=target.previous is not None)
    except (HatchError, FtnQueueFullError) as exc:
        _announce(session, f"Not hatched: {exc}", error=True)
        return False
    sent_as = f" as {hatched.send_name}" if hatched.send_name != hatched.file_name else ""
    again = ", replacing the earlier copy" if hatched.replaces else ""
    _announce(session, f"{name} hatched into {hatched.tag}{sent_as}{again}: it goes to {uplink} with the next "
                       f"session.")
    return True


# --- end hatching ------------------------------------------------------------------


# --- a board's Origin line (issue #1474)---------------------------------------


def board_origin_rows(db: Database, area: AreaMapping, network: FtnNetwork | None) -> list[Field]:
    """The board screen's Origin row: the board's own text, or what it
    falls back to -- the network's Origin, else the BBS's name."""
    if area.origin_text:
        return [Field("Origin", area.origin_text)]
    if network is not None and network.origin_text:
        return [Field("Origin", f"the network's: {network.origin_text}")]
    return [Field("Origin", "the network's: the BBS's name")]


async def board_origin_action(session: Session, lane: DatabaseLane, actor: User, board: Board) -> bool:
    """[O]rigin on the screen of a board carrying an echo: the text its
    echomail's Origin line carries before this node's address, or an empty
    line for the network's Origin. Returns whether anything changed."""
    area = await lane.run(board_area, board)
    network = await lane.run(get_network, area.network_id) if area is not None else None
    if area is None or network is None:
        _announce(session, f"{board.name} carries no echo, so it has no Origin line.", error=True)
        return False
    room = origin_room(network.our_address)
    await session.write_line("")
    await session.write_line(colored(
        f"The Origin line this board's echomail carries on {network.name}, before this node's address: at most "
        f"{room} characters, so the line stays within 79. An empty line uses the network's Origin.",
        fg_color=MUTED_COLOR))
    await write_prompt(session, "Origin: ")
    try:
        text = await session.read_line(cancellable=True, initial=area.origin_text)
    except InputCancelled:
        return False
    try:
        stored = await lane.run(set_board_origin, board, text)
    except FtnNetworkError as exc:
        _announce(session, str(exc), error=True)
        return False
    if stored == area.origin_text:
        return False
    outcome = (
        f"{board.name}'s echomail carries the Origin {stored!r} on {network.name}." if stored
        else f"{board.name}'s echomail carries {network.name}'s Origin again."
    )
    await lane.run(lambda db: record_action(db, actor=actor, action="set_board_origin", detail=outcome))
    _announce(session, outcome)
    return True
