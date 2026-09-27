"""
The node map screens (design doc §8.12, issue #777): "Nodes known to
<board>", reached from the Directory, and the pieces the SysOp's view behind
Link status `[P]eers` shares with it (`netbbs.net.admin_flow`).

What is listed, and for whom, is `netbbs.link.node_map.build_node_map`'s
decision; this module draws it. A caller's detail view names the boards,
file areas and channels this node carries from that origin *that this caller
could already open*, filtered by the same read gates as ordinary browsing.
Link addresses, relay roles and reliability are never shown here -- the
caller's map does not even carry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from netbbs.attestation import meets_age
from netbbs.auth.users import User
from netbbs.boards.boards import list_boards
from netbbs.communities import get_effective_min_age, get_effective_min_read_level
from netbbs.config import get_node_display_name, get_node_map_min_level
from netbbs.files.areas import list_file_areas
from netbbs.link.boards import LinkContext
from netbbs.link.dial_in import advertised_dial_in
from netbbs.link.node_map import (
    CANDIDATE,
    ORIGIN,
    NodeMapEntry,
    build_node_map,
    carried_from,
    relative_time,
)
from netbbs.link.trust import TrustDimension
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.chat_flow import _may_enter_quietly, _visible_channels_for
from netbbs.net.detail_view import show_detail
from netbbs.net.node_theme import effective_accent_color, effective_header_color
from netbbs.net.notices import take_notices
from netbbs.net.picker import ListColumn, pick_item
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ALERT_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

MAP_HOTKEY = "m"
MAP_MENU_TEXT = menu_key("M", "ap", prefix="Node ")

NODE_MAP_COLUMNS = (
    ListColumn("known", 22, VALUE_COLOR),
    ListColumn("last heard", 20, VALUE_COLOR),
)

_DIMENSION_LABELS = {
    TrustDimension.IDENTITY_INTEGRITY.value: "Identity trust",
    TrustDimension.RESOURCE_BEHAVIOR.value: "Resource trust",
    TrustDimension.CONTENT_CONDUCT.value: "Content trust",
}
_TRUST_COLORS = {"blocked": ALERT_COLOR, "quarantined": WARNING_COLOR}


@dataclass(frozen=True)
class CarriedNames:
    boards: tuple[str, ...] = ()
    file_areas: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()


def node_map_available(link_context: LinkContext | None) -> bool:
    """The map exists only on a node with Link enabled (§8.12)."""
    return link_context is not None and link_context.link_node is not None


def may_open_node_map(db: Database, user: User) -> bool:
    """The node-wide minimum level the SysOp sets, checked like any other
    level gate -- for every account alike, the guest's included (§4.6)."""
    return meets_level(user, get_node_map_min_level(db))


def openable_carried_names(db: Database, user: User, fingerprint: str) -> CarriedNames:
    """What this node carries from origin `fingerprint` that `user` could
    open anyway, through the same gates ordinary browsing applies: a board's
    or file area's effective read level and age (`netbbs.net.board_flow`,
    `netbbs.net.file_flow`), and a channel's visibility and entry gates
    (`netbbs.net.chat_flow`)."""
    carried = carried_from(db, fingerprint)
    boards = tuple(
        board.name for board in list_boards(db, order_by="alphabetical")
        if board.board_id in carried["boards"]
        and meets_level(user, get_effective_min_read_level(db, board))
        and meets_age(db, user, get_effective_min_age(db, board))
    )
    areas = tuple(
        area.name for area in list_file_areas(db, order_by="alphabetical")
        if area.area_id in carried["file_areas"]
        and meets_level(user, get_effective_min_read_level(db, area))
        and meets_age(db, user, get_effective_min_age(db, area))
    )
    channels = tuple(
        channel.name for channel in _visible_channels_for(db, user)
        if channel.channel_id in carried["channels"] and _may_enter_quietly(db, channel, user)
    )
    return CarriedNames(boards, areas, channels)


def all_carried_names(db: Database, fingerprint: str) -> CarriedNames:
    """Everything this node carries from origin `fingerprint`, ungated: the
    SysOp's view."""
    carried = carried_from(db, fingerprint)
    return CarriedNames(
        tuple(b.name for b in list_boards(db, order_by="alphabetical") if b.board_id in carried["boards"]),
        tuple(a.name for a in list_file_areas(db, order_by="alphabetical") if a.area_id in carried["file_areas"]),
        tuple(
            row["name"] for row in db.connection.execute(
                "SELECT channel_id, name FROM channels WHERE link_hidden_at IS NULL ORDER BY name COLLATE NOCASE"
            ) if row["channel_id"] in carried["channels"]
        ),
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stable_id(entry: NodeMapEntry) -> int:
    """The node's permanent number on this board's map: #3 is the same node to
    every caller and to the SysOp, from one visit to the next."""
    return entry.number


def last_heard_text(entry: NodeMapEntry, *, now: datetime) -> str:
    if entry.source == CANDIDATE:
        return "never heard from"
    text = relative_time(entry.last_heard, now=now)
    return f"stale, {text}" if entry.stale else text


def row_cells(entry: NodeMapEntry, *, now: datetime) -> list[str | tuple[str, int]]:
    heard = last_heard_text(entry, now=now)
    color = WARNING_COLOR if entry.stale else (MUTED_COLOR if entry.last_heard is None else VALUE_COLOR)
    return [sanitize_text(entry.relationship), (heard, color)]


def row_description(entry: NodeMapEntry, *, now: datetime) -> str:
    return f"{entry.relationship}; last heard {last_heard_text(entry, now=now)}"


def _dial_in_lines(entry: NodeMapEntry) -> list[str]:
    """The caller-facing `dial_in` addresses of `entry`'s signed descriptor
    (§8.2), in the signer's order: validated by `advertised_dial_in`, which
    drops a malformed entry and never raises, then sanitized before the
    panel styles and wraps them. An origin-only node has no descriptor on
    file, so none."""
    return [sanitize_text(address.url) for address in advertised_dial_in(entry.descriptor_payload)]


def node_sections(
    entry: NodeMapEntry, *, carried: CarriedNames, now: datetime, sysop: bool,
    first_named: str | None = None,
) -> list[Section]:
    """One node's detail. Every `Field` value is sanitized by the panel;
    names, DNS names and addresses here are the remote node's own text."""
    origin_only = entry.source == ORIGIN
    about: list[Field | Note] = [
        Field("Name", entry.friendly_name, bold=True),
        Field("DNS name", entry.dns_name or "unknown", color=VALUE_COLOR if entry.dns_name else MUTED_COLOR),
    ]
    if sysop:
        about.append(Field("Technical identity", entry.fingerprint, color=METADATA_COLOR))
    about.append(Field("Known", entry.relationship))
    heard = last_heard_text(entry, now=now)
    about.append(Field(
        "Last heard", heard,
        color=WARNING_COLOR if entry.stale else (MUTED_COLOR if entry.last_heard is None else VALUE_COLOR),
        note="Not heard of for over 30 days." if entry.stale else None,
    ))
    if entry.source == CANDIDATE:
        about.append(Field("First named by a peer list", first_named or "unknown"))
        about.append(Note(
            "Unverified: named in a peer list, never met, introduced by nobody. Callers do not see it."
        ))
    sections = [Section("Node", about)]

    if sysop:
        # What the SysOp came for first: trust, then how nodes reach it.
        trust_rows: list[Field | Note] = [
            Field(label, entry.trust.get(dimension, "probationary"),
                  color=_TRUST_COLORS.get(entry.trust.get(dimension, ""), VALUE_COLOR))
            for dimension, label in _DIMENSION_LABELS.items()
        ]
        if entry.hidden_from_callers and entry.source != CANDIDATE:
            trust_rows.append(Note("Quarantined or blocked here, so callers do not see it on the map."))
        sections.append(Section("Trust", trust_rows))
        if origin_only:
            reach: list[Field | Note] = [Field("Addresses", "unknown", color=MUTED_COLOR)]
        else:
            reach = [Field("Address", address) for address in entry.addresses] or [
                Field("Addresses", "none published (outgoing-only)", color=MUTED_COLOR)
            ]
        if entry.outgoing_only is not None:
            reach.insert(0, Field("Kind", "outgoing-only" if entry.outgoing_only else "full peer"))
        sections.append(Section("Reachability", reach))
        sections.append(Section("Relaying", [
            Field("Reliability", f"{entry.reliability:.2f}" if entry.reliability is not None else "unknown"),
            Field("Published relays", str(entry.published_relays)),
            Field("Live relays", str(entry.live_relays)),
            Field("We relay for it", "yes" if entry.we_relay_for_it else "no"),
            Field("It relays for us", "yes" if entry.it_relays_for_us else "no"),
        ], paired=True))

    dial_in = _dial_in_lines(entry)
    if dial_in:
        sections.append(Section("Dial in", [Field("Address", line) for line in dial_in]))
    else:
        sections.append(Section("Dial in", [
            Field("Addresses", "unknown" if origin_only else "none published", color=MUTED_COLOR)
        ]))

    rows: list[Field | Note] = [
        *(Field("Board", name) for name in carried.boards),
        *(Field("File area", name) for name in carried.file_areas),
        *(Field("Channel", name) for name in carried.channels),
    ]
    if not rows:
        rows.append(Note(
            "Nothing carried here from this node." if sysop
            else "Nothing carried here from this node is open to you."
        ))
    sections.append(Section("Carried here", rows))
    return sections


def map_title(board_name: str) -> str:
    """The list is not the whole network, and the title is where it says so
    (§8.12): nodes pass on only the nodes they have met."""
    return f"Nodes known to {board_name}"


async def node_map_screen(
    session: Session, lane: DatabaseLane, user: User, *, link_context: LinkContext | None,
) -> None:
    """The caller's node map: a list, then one node's detail, then back to
    the list, until `[B]ack`. Writes nothing."""
    if not node_map_available(link_context):
        return
    own_fingerprint = link_context.node_identity.fingerprint

    def _load(db: Database) -> dict:
        return {
            "allowed": may_open_node_map(db, user),
            "board": get_node_display_name(db),
            "entries": build_node_map(db, own_fingerprint=own_fingerprint, sysop=False),
            "redraw": redraw_in_place_enabled(db, user),
            "unicode": unicode_style_enabled(db, user),
            "collapsed": breadcrumb_collapsed_enabled(db, user),
            "accent": effective_accent_color(session, db),
            "header": effective_header_color(session, db),
        }

    reopen_at: int | None = None
    while True:
        state = await lane.run(_load)
        if not state["allowed"]:
            # Re-checked at the moment of use: the SysOp may have raised the
            # level since the Directory drew its entry.
            return
        now = utc_now()
        title = map_title(state["board"])
        selected = await pick_item(
            session, state["entries"],
            # The friendly name alone; the DNS name is in the detail view.
            name_of=lambda entry: entry.friendly_name,
            stable_id_of=stable_id,
            description_of=lambda entry: row_description(entry, now=now),
            columns=NODE_MAP_COLUMNS,
            column_values_of=lambda entry: row_cells(entry, now=now),
            title=title,
            breadcrumb=("Directory",),
            empty_message="No other nodes are known here yet.",
            start_stable_id=reopen_at,
            redraw_in_place=state["redraw"],
            unicode_style=state["unicode"],
            collapsed=state["collapsed"],
            accent_color=state["accent"],
            header_color=state["header"],
        )
        if selected is None:
            return
        reopen_at = stable_id(selected)
        carried = await lane.run(openable_carried_names, user, selected.fingerprint)
        await show_detail(
            session,
            title=screen_title(
                sanitize_text(selected.friendly_name),
                breadcrumb=(session.node_display_name, "Directory", title),
                width=session.terminal_width,
                clear=False,
                unicode_style=state["unicode"], collapsed=state["collapsed"],
                header_color=state["header"],
                node_name_gradient=session.node_name_gradient,
            ),
            sections=node_sections(selected, carried=carried, now=now, sysop=False),
            actions=[("b", menu_key("B", "ack"))],
            redraw_in_place=state["redraw"],
            unicode_style=state["unicode"],
            message="\r\n".join(take_notices(session)) or None,
        )
