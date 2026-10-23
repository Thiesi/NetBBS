"""
The SysOp console's QWKnet screens (issues #1481 and #1482, design doc §6.11).

**Settings → QWKnet networks** (`qwknet_networks_screen`) lists the
networks with what each last call came to, and on the bar:

- picking a network edits it in one draft: our QWK ID, the hub's QWK ID,
  host, port, transport, password, poll interval, tagline, whether node
  accounts may call; nothing is stored before [S]ave;
- **[P]oll now** calls the highlighted network's hub at once;
- **[M]ap conferences**: the boards that may carry one of its conferences,
  each picked to type its conference number (an empty line makes it local);
- **[N]odes**: the node accounts it serves as a hub, each edited in a draft
  -- QWK ID, password (typed unseen, kept only as a hash), limits and the
  conferences it carries -- with the address and script a node calls with;
- **[H]eld**: messages for conferences no board carries, per conference,
  with [R]e-toss once mapped and [D]elete, and packets that could not be
  read;
- **[D]elete**: its boards become local and keep their posts.

Every screen follows §3.5: content first, actions on the bar, drafts that
persist nothing before Save.
"""

from __future__ import annotations

from netbbs.auth.users import User, hash_password_off_loop
from netbbs.boards import Board
from netbbs.boards.boards import list_boards
from netbbs.config import get_qwk_bbs_id
from netbbs.digits import is_ascii_number
from netbbs.moderation.log import record_action
from netbbs.net.admin_fields import _int_field, _optional_text_field
from netbbs.net.admin_ui import _announce, _announce_line, _load_chrome, edit_resource_draft, pick_item
from netbbs.net.char_input import InputCancelled
from netbbs.net.confirm import prompt_yes_no
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
from netbbs.qwknet.exchange import (
    HeldGroup,
    delete_held,
    list_held,
    pending_for_node,
    pending_for_uplink,
    retoss_conference,
)
from netbbs.qwknet.networks import (
    DEFAULT_PORTS,
    TRANSPORTS,
    QwknetError,
    QwknetNetwork,
    QwknetNode,
    board_conference,
    delete_network,
    delete_node,
    get_network,
    list_networks,
    list_nodes,
    map_conference,
    network_conferences,
    node_conferences,
    save_network,
    save_node,
    unmap_board,
)
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.rendering.layout import MenuEntry
from netbbs.rendering.menu import menu_key
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

# DOVE-Net through Vertrauen, the network most Synchronet systems are on:
# the starting point for a new network. The SysOp fills in the password.
DOVE_NET = {"name": "DOVE-Net", "hub_id": "VERT", "host": "vert.synchro.net", "transport": "https"}
_TRANSPORT_LABELS = {"https": "HTTPS (qnet-http)", "http": "HTTP (qnet-http, password readable)",
                     "ftp": "FTP (passive, password readable)", "ftps": "FTPS (FTP over TLS)"}


def _mailer(node_controls):
    return getattr(node_controls, "qwknet_mailer", None) if node_controls is not None else None


def _describe(network: QwknetNetwork, waiting: int, nodes: int) -> str:
    parts = []
    if network.has_uplink:
        parts.append(f"{network.our_id} via {network.hub_id} ({network.transport})")
    else:
        parts.append(f"{network.our_id}, top hub")
    parts.append("on" if network.enabled else "off")
    if network.serve_nodes:
        parts.append(f"serves {nodes} node{'' if nodes == 1 else 's'}")
    if waiting:
        parts.append(f"{waiting} waiting")
    if network.last_poll_outcome:
        parts.append(f"last call: {network.last_poll_outcome}")
    return ", ".join(parts)


# --- Settings → QWKnet networks ---------------------------------------------------


@located("QWKnet networks")
async def qwknet_networks_screen(session: Session, lane: DatabaseLane, actor: User, *, node_controls=None) -> None:
    status: str | None = None
    facts: dict[int, tuple[int, int]] = {}

    async def _load() -> list[QwknetNetwork]:
        def load(db: Database):
            networks = list_networks(db)
            for network in networks:
                facts[network.id] = (pending_for_uplink(db, network) if network.has_uplink else 0,
                                     len(list_nodes(db, network.id)))
            return networks

        return await lane.run(load)

    async def _create() -> QwknetNetwork | None:
        return await edit_network(session, lane, actor, None, node_controls=node_controls)

    async def _delete(network: QwknetNetwork):
        nonlocal status
        await session.write_line("")
        if not await prompt_yes_no(
            session, f"Delete {network.name}? Its boards become local and keep their posts; its node accounts "
                     "and held messages go.", default=False):
            return None

        def _persist(db: Database) -> None:
            delete_network(db, network.id)
            record_action(db, actor=actor, action="delete_qwknet_network",
                          detail=f"deleted QWKnet network {network.name!r}")

        await lane.run(_persist)
        status = f"Deleted {network.name}."
        return await _load()

    async def _poll(network: QwknetNetwork):
        nonlocal status
        mailer = _mailer(node_controls)
        if not network.has_uplink:
            status = f"{network.name} has no hub to call: its nodes call this BBS."
        elif not network.enabled:
            status = f"{network.name} is off; enable it first."
        elif mailer is None:
            status = "Only the running node calls hubs; this console can't."
        else:
            mailer.poll_now(network.id)
            status = f"Calling {network.hub_id} now; the list shows the outcome when you refresh (Ctrl-R)."
        return await _load()

    async def _conferences(network: QwknetNetwork):
        await qwknet_conferences_screen(session, lane, actor, network)
        return await _load()

    async def _nodes(network: QwknetNetwork):
        await qwknet_nodes_screen(session, lane, actor, network)
        return await _load()

    async def _held(network: QwknetNetwork):
        await qwknet_held_screen(session, lane, actor, network)
        return await _load()

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        picked = await _pick_item(
            session, await _load(),
            name_of=lambda n: n.name,
            stable_id_of=lambda n: n.id,
            description_of=lambda n: _describe(n, *facts.get(n.id, (0, 0))),
            title="QWKnet networks", own=True,
            empty_message="No QWKnet networks. [C]reate one: it starts as DOVE-Net through Vertrauen.",
            refresh=_load, on_create=_create,
            item_keys={"d": _delete, "p": _poll, "m": _conferences, "n": _nodes, "h": _held},
            live_nav=[MenuEntry(label=menu_key("P", "oll now"), brief="Call this network's hub now"),
                      MenuEntry(label=menu_key("M", "ap conferences"), brief="Which board carries which conference"),
                      MenuEntry(label=menu_key("N", "odes"), brief="Systems this BBS serves as a hub"),
                      MenuEntry(label=menu_key("H", "eld"), brief="Messages no board carries yet"),
                      MenuEntry(label=menu_key("D", "elete"), brief="Remove a network")],
            live_label=lambda: status or "QWK networks: conferences on boards, packets to and from a hub",
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await edit_network(session, lane, actor, picked, node_controls=node_controls)


def _secret_field(key: str, label: str, *, clears: bool = True):

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        hint = "typed unseen; Enter on an empty line clears it" if clears else "typed unseen"
        await write_field_prompt(session, colored(f"{label} ({hint}):", fg_color=MUTED_COLOR))
        try:
            value = await read_field_line(session, initial="", echo=False)
        except InputCancelled:
            return
        if value.strip() or clears:
            draft[key] = value.strip()

    return prompt


def _network_draft(network: QwknetNetwork | None, default_id: str) -> dict:
    if network is None:
        return {"name": DOVE_NET["name"], "our_id": default_id, "hub_id": DOVE_NET["hub_id"],
                "transport": DOVE_NET["transport"], "host": DOVE_NET["host"], "port": 0, "password": "",
                "poll_minutes": 240, "tagline": "", "enabled": False, "serve_nodes": False}
    return {"name": network.name, "our_id": network.our_id, "hub_id": network.hub_id,
            "transport": network.transport, "host": network.host, "port": network.port,
            "password": network.password, "poll_minutes": network.poll_minutes, "tagline": network.tagline,
            "enabled": network.enabled, "serve_nodes": network.serve_nodes}


async def edit_network(session: Session, lane: DatabaseLane, actor: User, existing: QwknetNetwork | None, *,
                       node_controls=None) -> QwknetNetwork | None:
    """One network's settings in a draft; nothing is stored before [S]ave."""
    draft = _network_draft(existing, await lane.run(get_qwk_bbs_id))
    transports = list(TRANSPORTS)
    fields = [
        FieldSpec(key="enabled", label="Enabled", render=lambda d: "yes" if d["enabled"] else "no",
                  prompt=bool_field("enabled"), step=bool_step("enabled"), section="Network",
                  brief="Call the hub and carry this network's conferences",
                  help="Off by default. Nothing is sent, fetched or served while it is off."),
        FieldSpec(key="name", label="Name", render=lambda d: d["name"], prompt=text_field("name", required=True),
                  section="Network", brief="How the network is called here (DOVE-Net)"),
        FieldSpec(key="our_id", label="Our QWK ID", render=lambda d: d["our_id"] or "(not set)",
                  prompt=text_field("our_id", required=True), section="Network",
                  brief="This BBS's ID on the network: your account at the hub",
                  help="2-8 letters and digits, starting with a letter, unique on the network. It is the user "
                       "name of this BBS's account at the hub, and how routes name this BBS."),
        FieldSpec(key="tagline", label="Tagline", render=lambda d: d["tagline"] or "(the BBS's name)",
                  prompt=_optional_text_field("tagline"), section="Network",
                  brief="The line under every post written here"),
        FieldSpec(key="hub_id", label="Hub QWK ID", render=lambda d: d["hub_id"] or "(none: this BBS is the hub)",
                  prompt=_optional_text_field("hub_id"), section="Hub",
                  brief="The hub's QWK ID (Vertrauen: VERT); empty when this BBS is the top hub"),
        FieldSpec(key="host", label="Host", render=lambda d: d["host"] or "(not set)", prompt=text_field("host"),
                  section="Hub", brief="Where the hub answers (vert.synchro.net)"),
        FieldSpec(key="transport", label="Transport", render=lambda d: _TRANSPORT_LABELS[d["transport"]],
                  prompt=choice_field("transport", transports), step=choice_step("transport", transports),
                  section="Hub", brief="How to reach the hub",
                  help="HTTPS is what Synchronet's qnet-http calls the hub's web server with, and keeps the "
                       "password private. FTP is qnet-ftp's way, passive only; FTPS adds TLS to it."),
        FieldSpec(key="port", label="Port",
                  render=lambda d: str(d["port"]) if d["port"] else f"{DEFAULT_PORTS[d['transport']]} (usual)",
                  prompt=_int_field("port", "Port (0 for the usual one)"), section="Hub",
                  brief="0 uses the transport's usual port"),
        FieldSpec(key="password", label="Password", render=lambda d: "set" if d["password"] else "(not set)",
                  prompt=_secret_field("password", "Password"), section="Hub",
                  brief="Of this BBS's account at the hub"),
        FieldSpec(key="poll_minutes", label="Poll every", render=lambda d: f"{d['poll_minutes']} minutes",
                  prompt=_int_field("poll_minutes", "Minutes"), section="Hub",
                  brief="How often to call; DOVE-Net asks for about four times a day",
                  help="Posts written here also go up within a quarter of an hour of being written."),
        FieldSpec(key="serve_nodes", label="Serve nodes", render=lambda d: "yes" if d["serve_nodes"] else "no",
                  prompt=bool_field("serve_nodes"), step=bool_step("serve_nodes"), section="Hub for other systems",
                  brief="Let node accounts call this BBS for this network's packets",
                  help="Nodes call this BBS's web listener at /qwk.ssjs, the way Synchronet's qnet-http does. "
                       "Add them under [N]odes."),
    ]

    async def save(draft: dict) -> QwknetNetwork:
        candidate = QwknetNetwork(
            id=existing.id if existing is not None else None, name=draft["name"], our_id=draft["our_id"],
            hub_id=draft["hub_id"], transport=draft["transport"], host=draft["host"], port=int(draft["port"] or 0),
            password=draft["password"], poll_minutes=int(draft["poll_minutes"]), tagline=draft["tagline"],
            enabled=bool(draft["enabled"]), serve_nodes=bool(draft["serve_nodes"]),
        )

        def _persist(db: Database) -> QwknetNetwork:
            saved = save_network(db, candidate)
            record_action(db, actor=actor, action="set_qwknet_network",
                          detail=(f"{saved.name}: {saved.our_id} via {saved.hub_id or '(top hub)'} "
                                  f"{saved.transport}://{saved.host}:{saved.effective_port} "
                                  f"enabled={saved.enabled} serve_nodes={saved.serve_nodes}"))
            return saved

        return await lane.run(_persist)

    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=existing.name if existing is not None else "New QWKnet network",
        fields=fields, draft=draft, save=save, error_type=QwknetError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        preamble=("A new network starts as DOVE-Net through Vertrauen: create an account there named after "
                  "your QWK ID first, then fill in its password." if existing is None else None),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, f"Saved {saved.name}. The mailer picks it up within a minute.")
    return saved


# --- conferences ---------------------------------------------------------------


@located("QWKnet conferences")
async def qwknet_conferences_screen(session: Session, lane: DatabaseLane, actor: User,
                                    network: QwknetNetwork) -> None:
    """The boards that may carry one of `network`'s conferences: each local
    board, and the boards that carry one already. Picking one asks for its
    conference number."""
    from netbbs.ftn.networks import is_board_ftn
    from netbbs.link.boards import is_board_linked

    numbers: dict[int, int | None] = {}

    async def _load() -> list[Board]:
        def load(db: Database) -> list[Board]:
            out = []
            numbers.clear()
            for board in list_boards(db):
                mapping = board_conference(db, board)
                if mapping is not None and mapping.network_id != network.id:
                    continue
                if mapping is None and (is_board_linked(db, board) or is_board_ftn(db, board)):
                    continue
                numbers[board.id] = mapping.number if mapping is not None else None
                out.append(board)
            return out

        return await lane.run(load)

    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        picked = await _pick_item(
            session, await _load(),
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            description_of=lambda b: (f"conference {numbers[b.id]}" if numbers.get(b.id) is not None
                                      else "local"),
            title=f"{network.name} conferences", own=True,
            empty_message="No board can carry a conference: every board is Linked or carries an FTN echo.",
            preamble=("Pick a board to give it a conference number (2001 for DOVE-Net's General, for example). "
                      "A board carries one conference of one network; an empty number makes it local again."),
            refresh=_load, start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await _map_board(session, lane, actor, network, picked, numbers.get(picked.id))


async def _map_board(session: Session, lane: DatabaseLane, actor: User, network: QwknetNetwork, board: Board,
                     current: int | None) -> None:
    await session.write_line("")
    await session.write_line(colored(
        f"The {network.name} conference {board.name} carries (1-65535). An empty line makes it local again; "
        "its posts stay.", fg_color=MUTED_COLOR))
    await write_prompt(session, "Conference: ")
    try:
        text = (await session.read_line(cancellable=True, initial=str(current) if current else "")).strip()
    except InputCancelled:
        return
    try:
        if not text:
            if current is None:
                return
            await lane.run(unmap_board, board)
            outcome = f"{board.name} is a local board again; its posts stay."
        elif not is_ascii_number(text):
            _announce(session, "A conference number is digits only (e.g. 2001).", error=True)
            return
        else:
            mapping = await lane.run(map_conference, board, network.id, int(text))
            outcome = f"{board.name} carries conference {mapping.number} on {network.name}."
    except QwknetError as exc:
        _announce(session, str(exc), error=True)
        return
    await lane.run(lambda db: record_action(db, actor=actor, action="set_board_qwknet", detail=outcome))
    _announce(session, outcome)


# --- held ---------------------------------------------------------------------------


@located("QWKnet held messages")
async def qwknet_held_screen(session: Session, lane: DatabaseLane, actor: User, network: QwknetNetwork) -> None:
    status: str | None = None

    async def _load() -> list[HeldGroup]:
        return await lane.run(list_held, network.id)

    async def _retoss(group: HeldGroup):
        nonlocal status
        if group.conference is None:
            status = "A packet that could not be read can only be deleted."
            return await _load()
        current = await lane.run(get_network, network.id)
        if current is None:
            return None
        if group.conference not in await lane.run(network_conferences, network.id) and group.conference != 0:
            status = f"Map conference {group.conference} to a board first ([M]ap conferences)."
            return await _load()
        result = await lane.run(retoss_conference, current, group.conference)
        status = f"Re-tossed conference {group.conference}: {result.summary()}."
        return await _load()

    async def _delete(group: HeldGroup):
        nonlocal status
        await session.write_line("")
        what = "the packets" if group.conference is None else f"conference {group.conference}'s messages"
        if not await prompt_yes_no(session, f"Delete {what} ({group.count})?", default=False):
            return None
        count = await lane.run(delete_held, network.id, group.conference)
        await lane.run(lambda db: record_action(db, actor=actor, action="delete_qwknet_held",
                                                detail=f"{network.name}: deleted {count} held ({what})"))
        status = f"Deleted {count}."
        return await _load()

    chrome = await _load_chrome(lane, actor)
    await pick_item(
        session, await _load(),
        name_of=lambda g: ("Packets that could not be read" if g.conference is None
                           else "Mail (conference 0)" if g.conference == 0 else f"Conference {g.conference}"),
        stable_id_of=lambda g: -1 if g.conference is None else g.conference,
        description_of=lambda g: f"{g.count} held; {sanitize_text(g.reason)}",
        title=f"{network.name} held", own=True,
        empty_message="Nothing held: every message found its board.",
        refresh=_load, item_keys={"r": _retoss, "d": _delete},
        live_nav=[MenuEntry(label=menu_key("R", "e-toss"), brief="Toss again, once a board carries it"),
                  MenuEntry(label=menu_key("D", "elete"), brief="Drop these held messages")],
        live_label=lambda: status or "Held for 30 days, the newest 2,000 messages",
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
    )


# --- node accounts (the hub) ------------------------------------------------------------


@located("QWKnet nodes")
async def qwknet_nodes_screen(session: Session, lane: DatabaseLane, actor: User, network: QwknetNetwork) -> None:
    status: str | None = None
    waiting: dict[int, int] = {}

    async def _load() -> list[QwknetNode]:
        def load(db: Database):
            nodes = list_nodes(db, network.id)
            for node in nodes:
                waiting[node.id] = pending_for_node(db, node)
            return nodes

        return await lane.run(load)

    async def _create():
        return await edit_node(session, lane, actor, network, None)

    async def _delete(node: QwknetNode):
        nonlocal status
        await session.write_line("")
        if not await prompt_yes_no(session, f"Delete the node account {node.qwk_id}? It can't call any more.",
                                   default=False):
            return None

        def _persist(db: Database) -> None:
            delete_node(db, node.id)
            record_action(db, actor=actor, action="delete_qwknet_node", detail=f"{network.name}: {node.qwk_id}")

        await lane.run(_persist)
        status = f"Deleted {node.qwk_id}."
        return await _load()

    def describe(node: QwknetNode) -> str:
        parts = [sanitize_text(node.name)] if node.name else []
        parts.append("on" if node.enabled else "off")
        if waiting.get(node.id):
            parts.append(f"{waiting[node.id]} waiting")
        if node.last_outcome:
            parts.append(f"last: {sanitize_text(node.last_outcome)}")
        return ", ".join(parts)

    preamble = (f"Nodes call this BBS's web listener at /qwk.ssjs with their QWK ID and password. A Synchronet "
                f"node's call-out command is: ?qnet-http -s %s <this BBS's host> <password>; it calls this "
                f"network {network.our_id}.")
    if not network.serve_nodes:
        preamble = f"{network.name} does not serve nodes yet: turn Serve nodes on in its settings. " + preamble
    chrome = await _load_chrome(lane, actor)
    start = None
    while True:
        picked = await _pick_item(
            session, await _load(),
            name_of=lambda n: n.qwk_id, stable_id_of=lambda n: n.id, description_of=describe,
            title=f"{network.name} nodes", own=True,
            empty_message="No node accounts. [C]reate one for each system this BBS feeds.",
            preamble=preamble, refresh=_load, on_create=_create, item_keys={"d": _delete},
            live_nav=[MenuEntry(label=menu_key("D", "elete"), brief="Remove a node account")],
            live_label=lambda: status or "Systems this BBS serves as a hub",
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed, accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.id
        await edit_node(session, lane, actor, network, picked)


async def edit_node(session: Session, lane: DatabaseLane, actor: User, network: QwknetNetwork,
                    existing: QwknetNode | None) -> QwknetNode | None:
    """A node account in a draft; nothing is stored before [S]ave. The
    password is typed unseen and only its hash is kept."""
    conferences = await lane.run(network_conferences, network.id)
    carried = (await lane.run(node_conferences, existing.id) if existing is not None else set(conferences))
    draft = {
        "qwk_id": existing.qwk_id if existing is not None else "",
        "name": existing.name if existing is not None else "",
        "enabled": existing.enabled if existing is not None else True,
        "password": "",
        "packet_messages": existing.packet_messages if existing is not None else 2000,
        "rep_kib": existing.rep_kib if existing is not None else 4096,
        "conferences": set(carried),
    }

    async def _pick_conferences(session: Session, lane: DatabaseLane, draft: dict) -> None:
        # The ticks are the draft's: nothing is stored before the account's [S]ave.
        ticks: set[int] = draft["conferences"]
        items = sorted(conferences.items())

        async def toggle(item) -> None:
            ticks.symmetric_difference_update({item[0]})

        await _pick_item(
            session, items,
            name_of=lambda item: f"{item[0]} {item[1].name}", stable_id_of=lambda item: item[0],
            name_segments_of=lambda item: (("[x] " if item[0] in ticks else "[ ] ", MUTED_COLOR),
                                           (sanitize_text(f"{item[0]} {item[1].name}"), None)),
            title="Conferences this node carries", empty_message="The network has no conferences mapped yet.",
            preamble="Tick the conferences the node's packets carry; [B]ack returns to the account, which "
                     "keeps them once saved.",
            live_label=lambda: f"{len(ticks)} ticked",
            on_toggle=toggle,
        )

    fields = [
        FieldSpec(key="qwk_id", label="QWK ID", render=lambda d: d["qwk_id"] or "(not set)",
                  prompt=text_field("qwk_id", required=True), section="Node",
                  brief="The node's QWK ID: its login here"),
        FieldSpec(key="name", label="Name", render=lambda d: d["name"] or "(none)",
                  prompt=_optional_text_field("name"), section="Node", brief="A note on whose system it is"),
        FieldSpec(key="enabled", label="Enabled", render=lambda d: "yes" if d["enabled"] else "no",
                  prompt=bool_field("enabled"), step=bool_step("enabled"), section="Node",
                  brief="Off: its logins are refused"),
        FieldSpec(key="password", label="Password",
                  render=lambda d: ("new one typed" if d["password"] else
                                    "(unchanged)" if existing is not None else "(not set)"),
                  prompt=_secret_field("password", "Password", clears=False), section="Node",
                  brief="Its login password; only a hash is kept"),
        FieldSpec(key="conferences", label="Conferences",
                  render=lambda d: (", ".join(str(n) for n in sorted(d["conferences"])) or "(none)"),
                  prompt=_pick_conferences, section="Packets", brief="The conferences its packets carry"),
        FieldSpec(key="packet_messages", label="Packet size", render=lambda d: f"up to {d['packet_messages']} messages",
                  prompt=_int_field("packet_messages", "Messages"), section="Packets",
                  brief="The most messages one packet holds; the rest wait"),
        FieldSpec(key="rep_kib", label="Reply packet limit", render=lambda d: f"{d['rep_kib']} KiB",
                  prompt=_int_field("rep_kib", "KiB"), section="Packets",
                  brief="The largest reply packet it may upload"),
    ]

    async def save(draft: dict) -> QwknetNode:
        password = draft["password"]
        if existing is None and not password:
            raise QwknetError("A new node account needs a password.")
        if password and len(password) < 6:
            raise QwknetError("A node's password is at least 6 characters.")
        password_hash = await hash_password_off_loop(password) if password else None
        candidate = QwknetNode(
            id=existing.id if existing is not None else None, network_id=network.id, qwk_id=draft["qwk_id"],
            name=draft["name"], enabled=bool(draft["enabled"]), packet_messages=int(draft["packet_messages"]),
            rep_kib=int(draft["rep_kib"]),
        )

        def _persist(db: Database) -> QwknetNode:
            saved = save_node(db, candidate, password_hash=password_hash, conferences=set(draft["conferences"]))
            record_action(db, actor=actor, action="set_qwknet_node",
                          detail=(f"{network.name}: {saved.qwk_id} enabled={saved.enabled} "
                                  f"conferences={sorted(draft['conferences'])}"
                                  + (" password changed" if password_hash else "")))
            return saved

        return await lane.run(_persist)

    chrome = await _load_chrome(lane, actor)
    saved = await edit_resource_draft(
        session, lane, title=existing.qwk_id if existing is not None else f"New node on {network.name}",
        fields=fields, draft=draft, save=save, error_type=QwknetError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if saved is not None:
        _announce_line(session, f"Saved the node account {saved.qwk_id}.")
    return saved


__all__ = ["edit_network", "edit_node", "qwknet_networks_screen"]
