"""SysOp console chat-channel screens.

Chat channels, MRC rooms (adopt, retire, bridge, pause) and channel
restrictions. Split out of `netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from netbbs.access_map import LevelContext, level_context
from netbbs.auth.users import User, get_user_by_id
from netbbs.chat.moderation import (
    ChannelRestriction,
    list_active_channel_restrictions,
    unban_user,
    unmute_user,
)
from netbbs.chat.categories import CategoryError as ChannelCategoryError
from netbbs.chat.categories import create_category as create_channel_category
from netbbs.chat.categories import get_category_by_id as get_channel_category_by_id
from netbbs.chat.categories import list_subcategories as list_channel_subcategories
from netbbs.chat.categories import list_top_level_categories as list_top_level_channel_categories
from netbbs.chat.channels import (
    Channel,
    ChannelError,
    create_channel,
    delete_channel,
    list_channels,
    update_channel,
)
from netbbs.chat.hub import ChatHub
from netbbs.communities import get_community
from netbbs.link.boards import LinkContext
from netbbs.link.channels import (
    LinkChannelsError,
    accept_channel_origin_transfer,
    channel_origin_fingerprint,
    is_channel_linked,
    link_channel,
    linked_channel_ids,
    offer_channel_origin_transfer,
)
from netbbs.link.carry import (
    CarryDecisionError,
    carried_from_elsewhere,
    carried_to_review,
    mark_carried_reviewed,
)
from netbbs.moderation.log import record_action
from netbbs.mrc.bridge import MrcBridge
from netbbs.mrc.settings import (
    MrcChannelMapping,
    MrcSettingsError,
    adopt_open_room,
    clear_mrc_room,
    retire_open_room,
    get_mrc_mapping,
    load_mrc_settings,
    set_mrc_paused,
    set_mrc_room,
)
from netbbs.net.char_input import HELP_KEY, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.picker import ListColumn
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    bool_step,
    DetailAction,
    DetailMode,
    DetailState,
    text_field,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ERROR_COLOR,
    HEADER_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    colored,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _announce_line,
    _choice_prompt,
    _confirmation_refused,
    _menu_row,
    _yes_no,
    edit_resource_draft,
    pick_item,
)
from netbbs.net.admin_fields import (
    _NAME_REQUIREMENT_HELP,
    _VERIFIED_AGE_HELP,
    _int_field,
    _min_age_field,
    _min_age_label,
    _name_requirement_field,
    _name_requirement_label,
    _name_requirement_step,
    _optional_int_field,
    _optional_int_label,
    _plain_level_label,
    _resolve_redraw_preference,
)
from netbbs.net.admin_resources import (
    _Effective,
    _GATES_COLUMN,
    _channel_columns,
    _effective_by_id,
    _gate_cell,
    _hide_carried_screen,
    _linked_announcement,
    _peer_reach_rows,
    _remove_resource,
    _sections_text,
    _linked_node_label,
    _pick_new_origin,
    _warn_about_changed_node_identity,
    _moderator_lines,
)
from netbbs.net.admin_boards import _category_field, _community_field


# -- Inter-BBS chat: MRC bridge (issue #275) ---------------------------------

# Shown by the standalone admin CLI, which edits the database but cannot
# reach the running node's bridge: the bridge re-reads its mappings once
# per keepalive tick, so the change is not instant and must not be
# reported as if it were.
_MRC_STANDALONE_NOTE = (
    "Saved. A running node applies this within a minute; until then the bridge keeps its current mapping."
)


_CHANNEL_COLUMNS = [
    # A channel has one level, not a read/write split, and its own
    # visibility/membership pair in place of moderation.
    ListColumn("level", 5, VALUE_COLOR, align_right=True),
    ListColumn("access", 14, VALUE_COLOR),
    _GATES_COLUMN,
]


@located("Chat channels")
async def _channel_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None,
    mrc_bridge: MrcBridge | None = None,
    chat_hub: ChatHub | None = None,
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _channel_screen(session, lane, actor)
            await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_channels_screen(session, lane, actor, link_context=link_context, mrc_bridge=mrc_bridge, chat_hub=chat_hub)
            await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Chat channels help", _CHANNEL_MENU,
                about="Chat channels on this node, carried Linked and MRC-bridged ones included. List "
                      "opens a channel on its own screen: its fields change who may join and how it is "
                      "listed, in force once saved; its actions show mutes and bans and the moderation "
                      "history, schedule Notices, Link it, bridge it to MRC or delete it.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


_CHANNEL_MENU = (
    MenuEntry(label=menu_key("C", "reate"), brief="Add a new chat channel"),
    MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit channels"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
)


async def _draw_channel_menu(
    session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("Chat channels",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            list(_CHANNEL_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await _choice_prompt(session)


def _channel_field_specs(
    *, actor: User, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False,
    levels: LevelContext | None = None,
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_channel_screen`."""
    return [
        FieldSpec(
            key="name", label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The channel's display name",
            help="The channel's display name, shown wherever it's listed. Must be non-blank.",
            section="Identity",
        ),
        FieldSpec(
            key="description", label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing channels",
            help="A short explanation of what this channel is for, shown when browsing/selecting it.",
            section="Identity",
        ),
        FieldSpec(
            key="min_level", label="Min level",
            render=lambda d: _plain_level_label(levels, d.get("min_level")),
            prompt=_int_field("min_level", "Minimum level"),
            brief="Level required to join",
            help=(
                "The level a caller needs to see and join this channel. Never taken from its "
                "Community. 0: no level gate."
            ),
            section="Access",
        ),
        FieldSpec(
            key="min_age", label="Min age",
            render=lambda d: _min_age_label(d.get("min_age"), d.get("age_requirement")),
            prompt=_min_age_field("min_age", "age_requirement"),
            brief="Minimum caller age required",
            help=(
                "The minimum age to join this channel, checked against a caller's birthdate even if "
                "they keep it private. 'none' uses its Community's default, or no gate outside one."
            ) + _VERIFIED_AGE_HELP,
            section="Access",
        ),
        FieldSpec(
            key="name_requirement",
            label="Name requirement",
            render=lambda d: _name_requirement_label(d.get("name_requirement")),
            prompt=_name_requirement_field(),
            step=_name_requirement_step(),
            help=_NAME_REQUIREMENT_HELP + " 'none' uses its Community's rule.",
            brief="How chatters must be identified",
            section="Access",
        ),
        FieldSpec(
            key="community_id", label="Community",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(
                actor=actor,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Parent community, if any",
            help=(
                "The Community it is listed under in Topics. A Min age or Name requirement left at "
                "'none' then takes that Community's default; its level never does. 'none': outside "
                "every Community, still in the main channel list."
            ),
            section="Organization",
        ),
        FieldSpec(
            key="category_id", label="Category",
            render=lambda d: d.get("category_id_label") or "(none)",
            prompt=_category_field(
                actor=actor, create=create_channel_category, error_type=ChannelCategoryError,
                list_top_level=list_top_level_channel_categories, list_subcategories=list_channel_subcategories,
                title="Chat channel category", list_resources=list_channels, get_by_id=get_channel_category_by_id,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Where it's grouped in listings",
            help=(
                "The folder it is listed in when callers browse channels (Content > Categories). Has "
                "no effect on who can join. 'none': listed at the top level."
            ),
            section="Organization",
        ),
        FieldSpec(
            key="pinned", label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned"), step=bool_step("pinned"),
            brief="Shown at the top of listings",
            help="Shown at the top of channel listings, above unpinned channels, regardless of sort order.",
            section="Organization",
        ),
        FieldSpec(
            key="hidden", label="Hidden",
            render=lambda d: "yes" if d.get("hidden") else "no",
            prompt=bool_field("hidden"), step=bool_step("hidden"),
            brief="Omitted from channel listings",
            help=(
                "Delists this channel from ordinary browsing without deleting it. Members "
                "who already know about it (or are invited) can still join and use it "
                "normally -- this only affects whether it shows up when browsing."
            ),
            section="Membership",
        ),
        FieldSpec(
            key="members_only", label="Members-only",
            render=lambda d: "yes" if d.get("members_only") else "no",
            prompt=bool_field("members_only"), step=bool_step("members_only"),
            brief="Only invited members may join",
            help="When on, a caller can only join via an invite from an existing member -- browsing to it isn't enough.",
            section="Membership",
        ),
        FieldSpec(
            key="allow_member_invites",
            label="Allow member invites",
            render=lambda d: "yes" if d.get("allow_member_invites") else "no",
            prompt=bool_field("allow_member_invites"), step=bool_step("allow_member_invites"),
            brief="Members can invite others too",
            help=(
                "When on, any regular member can invite someone else, not just a moderator/"
                "SysOp. Most relevant when Members-only is also on, since that's the only "
                "way new members can join at all."
            ),
            section="Membership",
        ),
    ]


def _channel_draft(channel: Channel | None) -> dict:
    if channel is None:
        return {
            "name": "", "description": None, "min_level": 0,
            "community_id": None, "category_id": None, "pinned": False, "hidden": False,
            "members_only": False, "allow_member_invites": False,
            "min_age": None, "name_requirement": None,
            "community_id_label": None, "category_id_label": None, "age_requirement": None,
        }
    return {
        "name": channel.name, "description": channel.description, "min_level": channel.min_level,
        "community_id": channel.community_id, "category_id": channel.category_id,
        "pinned": channel.pinned, "hidden": channel.hidden, "members_only": channel.members_only,
        "allow_member_invites": channel.allow_member_invites,
        "min_age": channel.min_age, "name_requirement": channel.name_requirement,
        "age_requirement": channel.age_requirement,
    }


def _channel_origin_rows(link_context: LinkContext, origin: str | None, channel_id: str) -> list[Field | Note]:
    """Who a linked channel comes from, and an origin handoff in flight
    (issue #1359)."""
    own = link_context.node_identity.fingerprint
    rows: list[Field | Note] = [Field("Origin", "this node" if origin == own else _linked_node_label(link_context, origin))]
    offer = link_context.link_node.channel_lifecycle.pending_offer(channel_id)
    if offer is not None:
        if offer.payload.get("new_origin_fingerprint") == own:
            rows.append(Note(
                f"{_linked_node_label(link_context, offer.payload.get('old_origin_fingerprint'))} offers to hand "
                "this channel to this node. Accept transfer to take it over.",
                color=WARNING_COLOR,
            ))
        elif origin == own:
            rows.append(Note(
                f"Your offer to hand it to {_linked_node_label(link_context, offer.payload.get('new_origin_fingerprint'))} "
                "waits for that node to accept.",
            ))
    return rows


async def _transfer_channel_origin_screen(
    session: Session, lane: DatabaseLane, channel: Channel, link_context: LinkContext,
) -> None:
    """`[T]ransfer origin` (issue #1359, design doc §16 issue #1357, Decision
    5): offer a known peer this linked channel's origin. Alone it changes
    nothing; that peer's SysOp accepts on their node."""
    await session.write_line(
        colored("\r\nTransfer chat channel origin", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    if not link_context.link_node.peers:
        _announce_line(session, colored("No known peers to transfer this chat channel to.", fg_color=MUTED_COLOR))
        return
    picked = await _pick_new_origin(session, lane, link_context, title="New chat-channel origin")
    if picked is None:
        return
    target, target_label = picked
    await _warn_about_changed_node_identity(session, lane, target, role="The proposed new origin's")
    if not await prompt_yes_no(session, f"Offer to hand #{channel.name} off to {target_label}?", default=False):
        _announce_line(session, "Cancelled.")
        return
    try:
        offer = await lane.run(
            offer_channel_origin_transfer, channel, node_identity=link_context.node_identity,
            new_origin_fingerprint=target,
        )
    except LinkChannelsError as exc:
        _announce_line(session, colored(f"Could not offer transfer: {exc}", fg_color=MUTED_COLOR))
        return
    node = link_context.link_node
    node.channel_lifecycle.record_offer(channel.channel_id, offer)
    node.known_event_ids.add(offer.content_id)
    node.events[offer.content_id] = offer.to_dict()
    _announce_line(session, "Offer sent -- it will be pushed to peers on the next sync pass.")


async def _accept_channel_origin_transfer_screen(
    session: Session, lane: DatabaseLane, channel: Channel, link_context: LinkContext,
) -> None:
    """`[A]ccept transfer` (issue #1359): take over this linked channel's
    origin, as the pending offer naming this node proposes."""
    node = link_context.link_node
    offer = node.channel_lifecycle.pending_offer(channel.channel_id)
    if offer is None or offer.payload.get("new_origin_fingerprint") != link_context.node_identity.fingerprint:
        _announce_line(session, colored("\r\nNo pending incoming offer for this chat channel.", fg_color=MUTED_COLOR))
        return
    old_origin = offer.payload.get("old_origin_fingerprint")
    await session.write_line(
        colored("\r\nAccept chat channel origin", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    if isinstance(old_origin, str):
        await _warn_about_changed_node_identity(session, lane, old_origin, role="The offering node's")
    old_label = sanitize_text(_linked_node_label(link_context, old_origin))
    if not await prompt_yes_no(session, f"Accept origin of #{channel.name} from {old_label}?", default=False):
        _announce_line(session, "Cancelled.")
        return
    try:
        accepted = await lane.run(
            accept_channel_origin_transfer, channel, node_identity=link_context.node_identity, offer=offer,
        )
    except LinkChannelsError as exc:
        _announce_line(session, colored(f"Could not accept transfer: {exc}", fg_color=MUTED_COLOR))
        return
    node.channel_lifecycle.record_acceptance(
        channel.channel_id, new_origin_fingerprint=link_context.node_identity.fingerprint,
        accepted_content_id=accepted.content_id,
    )
    node.known_event_ids.add(accepted.content_id)
    node.events[accepted.content_id] = accepted.to_dict()
    _announce_line(
        session, f"Accepted -- this node is now #{channel.name}'s origin. Pushed to peers on the next sync pass.",
    )


async def _channel_sharing_rows(
    lane: DatabaseLane, channel: Channel, link_context: LinkContext | None, *, linked: bool,
    mrc_mapping: MrcChannelMapping | None,
) -> list[Field | Note]:
    """The Sharing section of a channel's screen: NetBBS Link and its MRC room."""
    sharing: list[Field | Note] = []
    if link_context is not None:
        sharing.append(Field("Linked", _yes_no(linked)))
        if linked:
            sharing.extend(_channel_origin_rows(
                link_context, await lane.run(channel_origin_fingerprint, channel), channel.channel_id,
            ))
            sharing.extend(
                await _peer_reach_rows(lane, link_context, link_context.link_node.channels.get(channel.channel_id))
            )
    open_room = mrc_mapping is not None and mrc_mapping.is_open_room
    if mrc_mapping is None:
        sharing.append(Field("MRC room", "none (not bridged)", color=MUTED_COLOR))
    elif open_room:
        last_active = mrc_mapping.last_active_at
        if last_active:
            display_format, display_timezone = await lane.run(resolve_display_preferences)
            last_active = format_for_display(last_active, override_format=display_format, override_timezone=display_timezone)
        sharing.append(Field(
            "MRC room",
            f"#{mrc_mapping.room} (open room -- opened by a caller"
            f"{', paused' if mrc_mapping.paused else ''}; last active {last_active or 'unknown'})",
        ))
        sharing.append(Note(
            "Retired automatically once idle and unfollowed; adopt it to keep it as an ordinary bridged "
            "channel. Never shared over NetBBS Link."
        ))
    else:
        state = "paused" if mrc_mapping.paused else "bridged"
        sharing.append(Field(
            "MRC room", f"#{mrc_mapping.room} ({state})",
            color=WARNING_COLOR if mrc_mapping.paused else SUCCESS_COLOR,
        ))
        if not (await lane.run(load_mrc_settings)).enabled:
            sharing.append(Note(
                "MRC is switched off node-wide -- nothing is relayed until you enable it under "
                "Settings > Inter-BBS chat (MRC).",
                color=WARNING_COLOR,
            ))
    return sharing


async def _channel_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: Channel | None = None,
    chat_hub: ChatHub | None = None, link_context: LinkContext | None = None,
    mrc_bridge: MrcBridge | None = None,
) -> Channel | None:
    """A chat channel's one screen (issue #1081, design doc §3.5), and the
    screen that creates one -- the same shape as `_board_screen`: whether it
    is Linked and its MRC room above the fields, the Sharing section after
    them, and [D]elete, [R]estrictions, [L]ink and the MRC actions while
    nothing waits to be saved.

    `chat_hub` (issue #277): live membership in `ChatHub` is keyed by
    channel *name*, and every session already inside holds the old name
    for its sends, receives and leave. Renaming an occupied channel
    would therefore cut those callers off from everyone who joins
    afterwards, so with the running node in reach the rename is refused
    until the channel is empty; the standalone admin CLI, which cannot
    see occupancy, says what a rename does to anyone inside instead."""
    with entered(session, sanitize_text(existing.name) if existing is not None else "Create chat channel"):
        current: dict = {"channel": existing, "mrc": None}
        own_fingerprint = link_context.node_identity.fingerprint if link_context is not None else None

        async def save(draft: dict) -> Channel:
            if not draft["name"]:
                raise ChannelError("name cannot be blank")
            channel = current["channel"]
            if channel is not None and draft["name"] != channel.name and chat_hub is not None:
                occupants = chat_hub.participant_count(channel.name)
                if occupants:
                    # A carried Link channel's name comes from a remote signed
                    # genesis: sanitize before it is styled into the refusal.
                    raise ChannelError(
                        f"{sanitize_text(channel.name)!r} cannot be renamed while {occupants} caller(s) are in it: "
                        "live chat membership follows the channel name, so they would be cut off from "
                        "everyone who joins afterwards. Wait until the channel is empty."
                    )
            if channel is None:
                created = await lane.run(
                    create_channel,
                    draft["name"], description=draft["description"], min_level=draft["min_level"],
                    category_id=draft["category_id"], pinned=draft["pinned"], hidden=draft["hidden"],
                    members_only=draft["members_only"], allow_member_invites=draft["allow_member_invites"],
                    min_age=draft["min_age"], name_requirement=draft["name_requirement"],
                    community_id=draft["community_id"], age_requirement=draft["age_requirement"], creator=actor,
                )
                _announce_line(session, f"Created chat channel {created.name!r}.")
                return created
            updated = await lane.run(
                update_channel,
                channel, name=draft["name"], description=draft["description"], min_level=draft["min_level"],
                category_id=draft["category_id"], pinned=draft["pinned"], hidden=draft["hidden"],
                members_only=draft["members_only"], allow_member_invites=draft["allow_member_invites"],
                min_age=draft["min_age"], name_requirement=draft["name_requirement"],
                community_id=draft["community_id"], age_requirement=draft["age_requirement"], changed_by=actor,
            )
            _announce_line(session, f"Updated {updated.name!r}.")
            if updated.name != channel.name:
                if chat_hub is None:
                    _announce_line(session, colored(
                        "If the node is running with callers in this channel, they keep the old name "
                        "until they leave and rejoin.",
                        fg_color=MUTED_COLOR,
                    ))
                if mrc_bridge is not None and current["mrc"] is not None:
                    # The bridge caches the mapped channel's name for hub
                    # delivery; a rename must reach it now (issue #275).
                    await mrc_bridge.refresh_channel_mappings()
            return updated

        def _act(run: Callable[[Channel], Awaitable[bool | None]]):
            async def action(session: Session, lane: DatabaseLane) -> bool:
                await session.write_line("")
                return bool(await run(current["channel"]))
            return action

        async def _delete(channel: Channel) -> bool:
            deleted = await _delete_channel_screen(
                session, lane, actor, channel, own_fingerprint=own_fingerprint, chat_hub=chat_hub,
            )
            if deleted and mrc_bridge is not None and current["mrc"] is not None:
                # The running bridge must forget the room now, not on the next
                # inbound line for it (issue #275).
                await mrc_bridge.refresh_channel_mappings()
            return deleted

        async def _announcements(channel: Channel) -> bool:
            # Issue #1380: this channel's scheduled announcements.
            from netbbs.net.admin_announcements import announcements_screen

            await announcements_screen(session, lane, actor, channel)
            return False

        async def _restrictions(channel: Channel) -> bool:
            await _channel_restrictions_screen(session, lane, actor, channel)
            return False

        async def _link(channel: Channel) -> bool:
            await _link_channel_screen(session, lane, actor, channel, link_context)
            return False

        async def _history(channel: Channel) -> bool:
            from netbbs.net.admin_maintenance import _audit_log_screen

            await _audit_log_screen(
                session, lane, actor, object_type="channel", object_id=channel.id,
                moderators=await lane.run(_moderator_lines, "channel", channel.id),
                title=f"History of {sanitize_text(channel.name)}",
            )
            return False

        async def _transfer(channel: Channel) -> bool:
            await _transfer_channel_origin_screen(session, lane, channel, link_context)
            return False

        async def _accept(channel: Channel) -> bool:
            await _accept_channel_origin_transfer_screen(session, lane, channel, link_context)
            return False

        async def _mrc_room(channel: Channel) -> bool:
            await _mrc_room_screen(session, lane, actor, channel, current["mrc"], mrc_bridge=mrc_bridge)
            return False

        async def _adopt(channel: Channel) -> bool:
            await _adopt_open_room_screen(session, lane, actor, channel, mrc_bridge=mrc_bridge)
            return False

        async def _retire(channel: Channel) -> bool:
            return await _retire_open_room_screen(
                session, lane, actor, channel, mrc_bridge=mrc_bridge, chat_hub=chat_hub,
            )

        async def _unbridge(channel: Channel) -> bool:
            await _unbridge_mrc_room(session, lane, actor, channel, current["mrc"], mrc_bridge=mrc_bridge)
            return False

        async def _pause(channel: Channel) -> bool:
            await _toggle_mrc_pause(session, lane, actor, channel, current["mrc"], mrc_bridge=mrc_bridge)
            return False

        async def refresh() -> DetailState:
            channel = current["channel"]
            if channel is None:
                return DetailState(draft=_channel_draft(None))

            def _load(db: Database):
                fresh = next((c for c in list_channels(db) if c.id == channel.id), channel)
                community = get_community(db, fresh.community_id) if fresh.community_id is not None else None
                category = get_channel_category_by_id(db, fresh.category_id) if fresh.category_id is not None else None
                labels = {
                    "community_id_label": community.name if community is not None else None,
                    "category_id_label": category.name if category is not None else None,
                }
                return fresh, get_mrc_mapping(db, fresh), labels

            channel, mrc_mapping, labels = await lane.run(_load)
            current["channel"], current["mrc"] = channel, mrc_mapping
            linked = await lane.run(is_channel_linked, channel) if link_context is not None else False
            open_room = mrc_mapping is not None and mrc_mapping.is_open_room
            header_bits = []
            if link_context is not None:
                header_bits.append("Linked" if linked else "not Linked")
            if mrc_mapping is None:
                header_bits.append("no MRC room")
            else:
                state = "open room" if open_room else ("paused" if mrc_mapping.paused else "bridged")
                header_bits.append(f"MRC #{mrc_mapping.room} ({state})")
            after = _sections_text(
                [Section("Sharing", await _channel_sharing_rows(
                    lane, channel, link_context, linked=linked, mrc_mapping=mrc_mapping,
                ))],
                width=session.terminal_width, unicode_style=await lane.run(unicode_style_enabled, actor),
            )
            actions = [
                DetailAction("d", menu_key("D", "elete"), _act(_delete), brief="Permanently remove this channel"),
                DetailAction("r", menu_key("R", "estrictions"), _act(_restrictions), brief="Active mutes and bans"),
                DetailAction("h", menu_key("H", "istory"), _act(_history), brief="Its moderators and what they did"),
                DetailAction("n", menu_key("N", "otices"), _act(_announcements),
                             brief="Scheduled announcements here"),
            ]
            if link_context is not None and not linked and not open_room:
                actions.append(DetailAction("l", menu_key("L", "ink this chat channel"), _act(_link), brief="Share it via NetBBS Link"))
            if link_context is not None and linked:
                # Issue #1359: origin succession, as a linked board has.
                own = link_context.node_identity.fingerprint
                offer = link_context.link_node.channel_lifecycle.pending_offer(channel.channel_id)
                if offer is None and await lane.run(channel_origin_fingerprint, channel) == own:
                    actions.append(DetailAction("t", menu_key("T", "ransfer origin"), _act(_transfer),
                                                brief="Hand it to another node"))
                if offer is not None and offer.payload.get("new_origin_fingerprint") == own:
                    actions.append(DetailAction("a", menu_key("A", "ccept transfer"), _act(_accept),
                                                brief="Take over as its origin"))
            if open_room:
                actions.append(DetailAction("a", menu_key("A", "dopt"), _act(_adopt), brief="Keep it as a bridged channel"))
                actions.append(DetailAction("c", menu_key("C", "lose room"), _act(_retire), brief="Remove it and its scrollback now"))
            else:
                actions.append(DetailAction("m", menu_key("M", "RC room"), _act(_mrc_room), brief="Bridge to a Multi Relay Chat room"))
            if mrc_mapping is not None and not open_room:
                actions.append(DetailAction("u", menu_key("U", "nbridge"), _act(_unbridge), brief="Stop relaying and forget the room"))
                if mrc_mapping.paused:
                    actions.append(DetailAction("p", menu_key("P", "ause MRC bridge: on"), _act(_pause), brief="Resume relaying to MRC"))
                else:
                    actions.append(DetailAction("p", menu_key("P", "ause MRC bridge: off"), _act(_pause), brief="Keep the mapping, relay nothing"))
            return DetailState(
                draft={**_channel_draft(channel), **labels},
                title=sanitize_text(channel.name),
                header=colored(" · ".join(header_bits), fg_color=MUTED_COLOR),
                after_fields=after, actions=actions,
            )

        redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
        unicode_style = await lane.run(unicode_style_enabled, actor)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
        return await edit_resource_draft(
            session, lane,
            title=sanitize_text(existing.name) if existing is not None else "Create chat channel", own=True,
            fields=_channel_field_specs(
                actor=actor, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                levels=await lane.run(level_context),
            ),
            draft={}, save=save, error_type=ChannelError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            detail=DetailMode(refresh=refresh, stay_after_save=existing is not None),
        )


async def _list_channels_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None,
    mrc_bridge: MrcBridge | None = None,
    chat_hub: ChatHub | None = None,
) -> None:
    def _load_channels(db: Database):
        channels = list_channels(db)
        # levels=False: a channel's `min_level` is a plain int
        # with no Community default behind it to resolve.
        return channels, _effective_by_id(db, channels, levels=False), carried_to_review(db, "channels")

    channels, effective, to_review = await lane.run(_load_channels)
    linked_channels = await lane.run(linked_channel_ids)
    selected = await pick_item(
        session, channels,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=lambda c: _channel_description(c, effective[c.id], c.channel_id in to_review),
        columns=_CHANNEL_COLUMNS,
        column_values_of=lambda c: _channel_columns(c, effective[c.id], c.channel_id in to_review),
        title="Chat channels", own=True,
        help_about=(
            'Every channel: the level to join, its access (open, members for members-only, hidden) '
            'and its gates. Gates: 18+ an age, name a verified name, name+ one shown. Pick one to '
            'open its screen.'
        ),
        empty_message="No chat channels yet.",
        linked_of=lambda c: c.id in linked_channels,  # issue #1104
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is not None:
        await _channel_detail_screen(session, lane, actor, selected, link_context=link_context, mrc_bridge=mrc_bridge, chat_hub=chat_hub)


def _channel_description(channel: Channel, effective: _Effective, to_review: bool = False) -> str:
    """Narrow-terminal fallback; see `_board_description`."""
    gates, _ = _gate_cell(effective.min_age, effective.name_requirement, effective.age_requirement)
    bits = ["to review"] if to_review else []
    bits += [] if gates == "-" else [gates]  # leads; see `_describe_resource`
    bits.append(f"level {channel.min_level}")
    if channel.members_only:
        bits.append("members-only")
    if channel.hidden:
        bits.append("hidden")
    return ", ".join(bits)


async def _channel_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, *, link_context: LinkContext | None = None,
    mrc_bridge: MrcBridge | None = None,
    chat_hub: ChatHub | None = None,
) -> None:
    """A chat channel's screen is its editor (issue #1081): see `_channel_screen`."""
    await lane.run(mark_carried_reviewed, "channels", channel.channel_id)
    await _channel_screen(
        session, lane, actor, existing=channel, chat_hub=chat_hub, link_context=link_context, mrc_bridge=mrc_bridge,
    )


async def _adopt_open_room_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, *, mrc_bridge: MrcBridge | None,
) -> MrcChannelMapping | None:
    """`[A]dopt` (issue #300): the SysOp keeps a room a caller opened --
    it becomes an ordinary bridged channel the sweeper ignores. Not
    destructive, so no confirmation: a toggle toggles."""

    def _adopt(db: Database) -> MrcChannelMapping:
        mapping = adopt_open_room(db, channel)
        record_action(
            db, actor=actor, action="adopt_mrc_room", object_type="channel", object_id=channel.id,
            detail=f"#{mapping.room}",
        )
        return mapping

    try:
        mapping = await lane.run(_adopt)
    except MrcSettingsError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return await lane.run(get_mrc_mapping, channel)
    if mrc_bridge is not None:
        await mrc_bridge.refresh_channel_mappings()
    _announce_line(session,
        f"Adopted: {channel.name!r} stays bridged to MRC room #{mapping.room} until you unbridge it."
    )
    return mapping


async def _retire_open_room_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, *, mrc_bridge: MrcBridge | None,
    chat_hub: ChatHub | None = None,
) -> bool:
    """`[C]lose room` (issue #300, once `Retire`): remove a room a caller opened, with its
    scrollback, now. Destructive, so the yes/no is the last keystroke
    behind the hotkey. Callers inside are told and returned to the
    picker, as for a deleted channel (issue #716)."""
    if not await prompt_yes_no(
        session, f"Close {sanitize_text(channel.name)} and delete its scrollback now?", default=False,
    ):
        return False

    def _retire(db: Database) -> None:
        retire_open_room(db, channel)
        record_action(
            db, actor=actor, action="retire_mrc_room", object_type="channel", object_id=channel.id,
            detail=f"retired open MRC room channel {channel.name!r} (id {channel.id})",
        )

    try:
        await lane.run(_retire)
    except MrcSettingsError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return False
    _announce_line(session, f"Closed {channel.name!r}.")
    # Callers first: the retirement has committed, and a bridge refresh that
    # fails must not leave anyone inside a room that is gone.
    _move_callers_out(session, chat_hub, channel)
    if mrc_bridge is not None:
        await mrc_bridge.refresh_channel_mappings()
    if mrc_bridge is None:
        _announce_line(session, colored(_MRC_STANDALONE_NOTE, fg_color=MUTED_COLOR))
    return True


async def _mrc_room_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, current: MrcChannelMapping | None,
    *, mrc_bridge: MrcBridge | None,
) -> MrcChannelMapping | None:
    """Issue #275 (design doc §16, #165 Decision 2): map this channel to
    one MRC room -- the SysOp's explicit, per-channel opt-in; nothing is
    ever bridged by default. Applied live through the running node's
    bridge when there is one. Issue #282: the current mapping, the
    relay warning, and the node-wide off notice all live on the channel
    detail screen now; this prompt only sets a room (blank keeps), and
    unmapping is the detail screen's own `[U]nbridge` hotkey rather
    than a magic "-" typed here."""
    shown = f"#{sanitize_text(current.room)}" if current is not None else "none"
    await session.write_line(
        colored(
            "Everything said in this channel will be relayed to that MRC room, and every caller "
            "in it appears on the MRC network under their own handle.",
            fg_color=MUTED_COLOR,
        )
    )
    await write_prompt(session, f"MRC room [{shown}] (blank = keep): ")
    raw = (await session.read_line()).strip()
    if not raw:
        return current

    def _set(db: Database) -> MrcChannelMapping:
        mapping = set_mrc_room(db, channel, raw)
        record_action(
            db, actor=actor, action="set_mrc_room", object_type="channel", object_id=channel.id,
            detail=f"#{mapping.room}",
        )
        return mapping

    try:
        mapping = await lane.run(_set)
    except MrcSettingsError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return current
    if mrc_bridge is not None:
        await mrc_bridge.refresh_channel_mappings()
    _announce_line(session, f"Channel {channel.name!r} is now bridged to MRC room #{mapping.room}.")
    if mrc_bridge is None:
        _announce_line(session, colored(_MRC_STANDALONE_NOTE, fg_color=MUTED_COLOR))
    return mapping


async def _unbridge_mrc_room(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, current: MrcChannelMapping,
    *, mrc_bridge: MrcBridge | None,
) -> None:
    """The channel detail's `[U]nbridge` (issue #282): forget the MRC
    room mapping, live through the running bridge when there is one."""

    def _clear(db: Database) -> None:
        clear_mrc_room(db, channel)
        record_action(
            db, actor=actor, action="clear_mrc_room", object_type="channel", object_id=channel.id,
            detail=f"was #{current.room}",
        )

    await lane.run(_clear)
    if mrc_bridge is not None:
        await mrc_bridge.refresh_channel_mappings()
    _announce_line(session, f"Channel {channel.name!r} is no longer bridged to MRC.")
    if mrc_bridge is None:
        _announce_line(session, colored(_MRC_STANDALONE_NOTE, fg_color=MUTED_COLOR))
    return None


async def _toggle_mrc_pause(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, current: MrcChannelMapping,
    *, mrc_bridge: MrcBridge | None,
) -> MrcChannelMapping:
    """The per-bridge disable §16 left open: keep the mapping, relay
    nothing either way, without touching MRC node-wide."""
    paused = not current.paused

    def _apply(db: Database) -> MrcChannelMapping:
        mapping = set_mrc_paused(db, channel, paused)
        record_action(
            db, actor=actor, action="pause_mrc_bridge" if paused else "resume_mrc_bridge",
            object_type="channel", object_id=channel.id, detail=f"#{mapping.room}",
        )
        return mapping

    try:
        mapping = await lane.run(_apply)
    except MrcSettingsError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return current
    if mrc_bridge is not None:
        await mrc_bridge.refresh_channel_mappings()
    verb = "paused" if paused else "resumed"
    _announce_line(session, f"MRC bridge for {channel.name!r} {verb}.")
    if mrc_bridge is None:
        _announce_line(session, colored(_MRC_STANDALONE_NOTE, fg_color=MUTED_COLOR))
    return mapping


async def _channel_restrictions_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> None:
    """
    Lists every currently-active mute/ban on `channel` and lets a
    SysOp lift one -- the ordinary door alongside `_check_ban`'s own
    emergency SysOp bypass (`netbbs.net.chat_flow`, dogfood follow-up).
    Before this, a self-ban or a ban placed by any channel moderator
    had no interactive recovery path whatsoever: `/unban` can only be
    run from *inside* the channel by someone holding MODERATE, which
    is exactly what being banned prevents, and no admin screen anywhere
    even listed `channel_restrictions` rows -- the only fix was direct
    database surgery.

    Usernames/timestamps are resolved once per redraw via `lane.run`,
    then read synchronously by `pick_item`'s own `name_of`/
    `description_of` callbacks -- same "pre-fetch, don't call the lane
    from inside a sync callback" shape `_who_screen` already
    established (see this module's own docstring) for the identical
    reason.
    """
    def _load(db: Database) -> tuple[list[ChannelRestriction], dict[int, str], str | None, str | None]:
        restrictions = list_active_channel_restrictions(db, channel)
        user_ids = {r.user_id for r in restrictions} | {r.imposed_by_user_id for r in restrictions}
        usernames: dict[int, str] = {}
        for user_id in user_ids:
            resolved = get_user_by_id(db, user_id)
            usernames[user_id] = resolved.username if resolved is not None else "(deleted account)"
        display_format, display_timezone = resolve_display_preferences(db)
        return restrictions, usernames, display_format, display_timezone

    def _name_of(r: ChannelRestriction, usernames: dict[int, str]) -> str:
        return f"{r.kind} -- {usernames[r.user_id]}"

    def _description_of(
        r: ChannelRestriction, usernames: dict[int, str], display_format: str | None, display_timezone: str | None
    ) -> str:
        if r.expires_at is None:
            expiry = "indefinite"
        else:
            expiry = f"until {format_for_display(r.expires_at, override_format=display_format, override_timezone=display_timezone)}"
        by = f"by {usernames[r.imposed_by_user_id]}"
        reason = f" -- {sanitize_text(r.reason)}" if r.reason else ""
        return f"{expiry} ({by}){reason}"

    last_stable_id: int | None = None
    while True:
        restrictions, usernames, display_format, display_timezone = await lane.run(_load)

        selected = await pick_item(
            session, restrictions,
            name_of=lambda r: _name_of(r, usernames),
            stable_id_of=lambda r: r.id,
            description_of=lambda r: _description_of(r, usernames, display_format, display_timezone),
            title=f"Restrictions on {channel.name!r}",
            help_about=(
                'Active mutes and bans in this channel: when each ends, who imposed it and why. Picking '
                'one asks, then lifts it at once.'
            ),
            empty_message="No active mute/ban restrictions on this chat channel.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            start_stable_id=last_stable_id,
        )
        if selected is None:
            return
        last_stable_id = selected.id

        target_label = usernames[selected.user_id]
        if not await prompt_yes_no(session, f"Lift this {selected.kind} on {target_label!r}?", default=False):
            _announce_line(session, "Cancelled.")
            continue

        target = await lane.run(get_user_by_id, selected.user_id)
        if target is None:
            # The account was deleted after this restriction was
            # imposed -- nothing left to look up an unmute/unban
            # against by object, and the restriction row itself is
            # already unreachable through any ordinary path once its
            # subject is gone. Same "nothing to do" shape lifting an
            # already-lifted restriction gets, just for a different
            # reason.
            _announce_line(session, colored("That account no longer exists.", fg_color=MUTED_COLOR))
            continue

        if selected.kind == "mute":
            await lane.run(unmute_user, channel, target, unmuted_by=actor)
        else:
            await lane.run(unban_user, channel, target, unbanned_by=actor)
        _announce_line(session, f"Lifted the {selected.kind} on {target_label!r}.")


def _link_channel_field_specs() -> list[FieldSpec]:
    """`_link_board_field_specs`'s own subset -- no `default_min_write_
    level`/`_moderated`/`_max_post_age_days`, no `forked_from` (channel
    origin succession is reused by reference only, not built, design
    doc §9.6)."""
    return [
        FieldSpec(
            key="default_min_level",
            label="Recommended min level",
            render=lambda d: _optional_int_label(d.get("default_min_level")),
            prompt=_optional_int_field("default_min_level", "Recommended minimum level"),
            brief="Sent to peers as a default",
            help=(
                "The level a node that starts carrying this channel gives its copy; its SysOp can "
                "change it there. Signed into the channel's Link record on Save, so it cannot be "
                "changed afterwards. 'none': no recommendation, so their copy starts at 0."
            ),
        ),
        FieldSpec(
            key="default_min_age",
            label="Recommended min age",
            render=lambda d: _optional_int_label(d.get("default_min_age")),
            prompt=_min_age_field("default_min_age"),
            brief="Sent to peers as a default",
            help="The minimum age a carrying node's copy starts with. Fixed on Save. 'none': no age gate.",
        ),
        FieldSpec(
            key="default_name_requirement",
            label="Recommended name requirement",
            render=lambda d: _name_requirement_label(d.get("default_name_requirement")),
            prompt=_name_requirement_field("default_name_requirement"),
            step=_name_requirement_step("default_name_requirement"),
            brief="Sent to peers as a default",
            help="The name requirement a carrying node's copy starts with. Fixed on Save. " + _NAME_REQUIREMENT_HELP,
        ),
    ]


async def _link_channel_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, link_context: LinkContext
) -> None:
    """
    `[L]ink this channel` (design doc §9.6, issue #87 -- previously
    defined by `netbbs.link.channels.link_channel` but, unlike `link_
    board`, never actually reachable from any live UI action; this is
    that missing call site). Mirrors `_link_board_screen`'s own field
    list, minus every field `Channel` has no equivalent setting for.

    Dogfood report: same fixed-linear-chain bug `_link_board_screen`
    had, same `edit_resource_draft` fix -- see `_link_board_field_
    specs`'s own docstring. `actor` is newly required here (it wasn't
    before): the draft-editor screen needs it to resolve display
    preferences the old plain-prompt chain never touched.
    """
    draft: dict = {
        "default_min_level": channel.min_level,
        "default_min_age": channel.min_age,
        "default_name_requirement": channel.name_requirement,
    }

    async def save(draft: dict) -> bool:
        genesis = await lane.run(
            link_channel,
            channel,
            node_identity=link_context.node_identity,
            default_min_level=draft["default_min_level"],
            default_min_age=draft["default_min_age"],
            default_name_requirement=draft["default_name_requirement"],
            default_age_requirement=channel.age_requirement,
        )
        link_context.link_node.channels[channel.channel_id] = genesis
        link_context.link_node.known_event_ids.add(genesis.content_id)
        link_context.link_node.events[genesis.content_id] = genesis.to_dict()
        _announce_line(session, _linked_announcement(channel.name))
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    await edit_resource_draft(
        session, lane,
        title="Link this chat channel",
        fields=_link_channel_field_specs(), draft=draft, save=save, error_type=LinkChannelsError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


def _move_callers_out(session: Session, chat_hub: ChatHub | None, channel: Channel) -> None:
    """Send everyone inside `channel`, which the SysOp just deleted, hid or
    retired, back to the channel list (issue #716), and say how many."""
    if chat_hub is None:
        return
    moved = chat_hub.close_channel(channel.name)
    if moved:
        _announce_line(
            session, f"{moved} caller session{'s' if moved != 1 else ''} in it moved back to the channel list.",
        )


async def _delete_channel_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, *,
    own_fingerprint: str | None = None, chat_hub: ChatHub | None = None,
) -> bool:
    # Issue #683: a carried chat channel whose origin is another node is hidden,
    # not destroyed -- see `_hide_carried_screen`.
    try:
        carried = await lane.run(carried_from_elsewhere, "channels", channel.channel_id, own_fingerprint)
    except CarryDecisionError as exc:
        _announce_line(session, colored(f"Cannot delete {channel.name!r}: {exc}", fg_color=ERROR_COLOR))
        return False
    if carried:
        hidden = await _hide_carried_screen(
            session, lane, actor, "channels", channel.channel_id, channel.name, own_fingerprint=own_fingerprint,
        )
        if hidden:
            _move_callers_out(session, chat_hub, channel)
        return hidden
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the chat channel, its scrollback, mute/ban "
            "restrictions, membership/invitations, and any moderator grants "
            "scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await write_prompt(
        session,
        f"Type the chat channel name {channel.name!r} to confirm, or anything else to cancel: ",
    )
    confirmation = (await session.read_line()).strip()
    if confirmation != channel.name:
        _confirmation_refused(session, confirmation, channel.name)
        return False
    # Issues #669/#683: a Linked channel keeps its genesis and is recorded as
    # excluded, so it stays declared as not carried and does not come back.
    removed = await _remove_resource(
        session, lane, actor, "channels", channel.channel_id, channel.name, own_fingerprint=own_fingerprint,
        delete=lambda db: delete_channel(db, channel, deleted_by=actor),
    )
    if removed:
        _move_callers_out(session, chat_hub, channel)
    return removed
