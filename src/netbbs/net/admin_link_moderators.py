"""
The SysOp console's Link moderators screen (issue #1359, design doc §16 issue
#1357 Decision 7): who, on other nodes, moderates the linked boards and
channels this node originates. Reached from Link status with `[M]oderators`.

Each grant names a user as `name@TheirBBS`, a scope -- one linked board or
channel, one of this node's Link Communities, or everything this node
originates -- and what they may do there. Saving signs this node's whole
roster again (`netbbs.link.moderators`) and hands it to the running node, so
it reaches carriers with the next pass. Every change is audited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.link.boards import LinkContext
from netbbs.link.moderators import (
    LinkModeratorsError,
    OwnGrant,
    grant_link_moderator,
    grants_held_here,
    list_own_grants,
    revoke_link_moderator,
)
from netbbs.link.node_profiles import identity_for_fingerprint, link_address_label
from netbbs.net.admin_trust import _help_entries
from netbbs.net.admin_ui import _BACK_ACTION, _detail_title, _load_chrome, pick_item
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.detail_view import show_detail
from netbbs.net.location import located
from netbbs.net.session import Session
from netbbs.rendering import ERROR_COLOR, MUTED_COLOR, SUCCESS_COLOR, colored, menu_key, sanitize_text
from netbbs.rendering.detail import Note, Section, Table
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display

_PERMISSION_WORDS = {
    "edit": "edit posts",
    "delete": "remove posts",
    "moderate": "mute and ban",
    "topic": "set the topic",
}

#: What a grant may give, per scope, offered in this order.
_PRESETS = {
    "board": (("edit", "delete"), ("delete",)),
    "channel": (("moderate", "topic"), ("moderate",)),
    "community": (("delete", "edit", "moderate", "topic"), ("delete", "edit"), ("moderate", "topic")),
    "origin": (("delete", "edit", "moderate", "topic"), ("delete", "edit"), ("moderate", "topic")),
}


@dataclass(frozen=True)
class _Scope:
    kind: str
    id: str | None
    label: str


def _permission_text(permissions) -> str:
    return ", ".join(_PERMISSION_WORDS[p] for p in sorted(permissions))


def own_scopes(db: Database, own_fingerprint: str) -> list[_Scope]:
    """What this node can delegate: everything it originates, each of its
    own Link Communities, and each linked board and channel it originates."""
    scopes = [_Scope("origin", None, "Everything this node originates")]
    for row in db.connection.execute(
        "SELECT name, link_id, link_genesis_json FROM communities WHERE link_genesis_json IS NOT NULL "
        "AND link_closure_json IS NULL ORDER BY name COLLATE NOCASE"
    ):
        if json.loads(row["link_genesis_json"])["envelope"]["payload"].get("origin_fingerprint") == own_fingerprint:
            scopes.append(_Scope("community", row["link_id"], f"Community {row['name']}"))
    for row in db.connection.execute(
        "SELECT name, board_id, link_genesis_json, link_origin_fingerprint FROM boards "
        "WHERE link_genesis_json IS NOT NULL ORDER BY name COLLATE NOCASE"
    ):
        origin = row["link_origin_fingerprint"] or json.loads(row["link_genesis_json"])["envelope"]["payload"].get(
            "origin_fingerprint"
        )
        if origin == own_fingerprint:
            scopes.append(_Scope("board", row["board_id"], f"Board {row['name']}"))
    for row in db.connection.execute(
        "SELECT name, channel_id, link_genesis_json, link_origin_fingerprint FROM channels "
        "WHERE link_genesis_json IS NOT NULL ORDER BY name COLLATE NOCASE"
    ):
        origin = row["link_origin_fingerprint"] or json.loads(row["link_genesis_json"])["envelope"]["payload"].get(
            "origin_fingerprint"
        )
        if origin == own_fingerprint:
            scopes.append(_Scope("channel", row["channel_id"], f"Channel #{row['name']}"))
    return scopes


def _scope_label(db: Database, own_fingerprint: str, grant: OwnGrant) -> str:
    for scope in own_scopes(db, own_fingerprint):
        if (scope.kind, scope.id) == (grant.scope_kind, grant.scope_id):
            return scope.label
    # The resource has gone, or moved to another origin: the grant no longer
    # covers anything, and says so.
    return f"{grant.scope_kind} no longer here"


def _grant_rows(db: Database, own_fingerprint: str) -> list[tuple[OwnGrant, str, str]]:
    rows = []
    for grant in list_own_grants(db):
        node = identity_for_fingerprint(db, grant.home_node_fingerprint).label
        who = link_address_label(grant.local_user_id, node)
        rows.append((grant, sanitize_text(who), sanitize_text(_scope_label(db, own_fingerprint, grant))))
    return rows


def _held_rows(db: Database, own_fingerprint: str) -> list[tuple[str, str, str]]:
    """What this node's own users may moderate on other nodes' resources."""
    rows = []
    for origin, entry in grants_held_here(db, own_fingerprint):
        scope = entry["scope"]
        if scope == "origin":
            where = "everything it originates"
        elif "community" in scope:
            where = "one of its Link Communities"
        else:
            where = f"one {scope['kind']}"
        rows.append((
            sanitize_text(entry["moderator"]["local_user_id"]),
            sanitize_text(f"{identity_for_fingerprint(db, origin).label}: {where}"),
            _permission_text(entry["permissions"]),
        ))
    return rows


def _register(link_context: LinkContext, raw: dict) -> None:
    """Hand this node's new roster to the running node, on the event loop."""
    from netbbs.link.events import event_content_id

    node = link_context.link_node
    content_id = event_content_id(raw["envelope"])
    node.known_event_ids.add(content_id)
    node.events[content_id] = raw
    # What delegated moderators' actions arriving here are checked against.
    node.record_moderator_roster(raw, raw["envelope"]["payload"]["created_at"])
    wake = getattr(node, "wake_sync", None)
    if callable(wake):
        wake()


@located("Link moderators")
async def link_moderators_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext,
) -> None:
    own = link_context.node_identity.fingerprint
    message = None
    while True:
        chrome = await _load_chrome(lane, actor)
        rows = await lane.run(_grant_rows, own)
        if rows:
            table = Table(
                headers=("Moderator", "On", "May", "Since"),
                rows=[
                    (who, scope, _permission_text(grant.permissions), format_for_display(grant.granted_at))
                    for grant, who, scope in rows
                ],
                flex=1,
            )
            body: list = [table]
        else:
            body = [Note("Nobody on another node moderates what this node originates.")]
        body.append(Note(
            "A Link moderator edits and removes posts, or mutes, bans and sets the topic, for everyone "
            "carrying the board or channel, not only on their own node. Removing a grant takes effect on "
            "each node as the change reaches it."
        ))
        sections = [Section(None, body)]
        held = await lane.run(_held_rows, own)
        if held:
            sections.append(Section("Held by this node's users", [
                Table(headers=("User", "On", "May"), rows=held, flex=1),
                Note("Granted by the nodes named: their moderation of those boards and channels is accepted "
                     "everywhere they are carried."),
            ]))
        actions = [("a", menu_key("A", "dd"))]
        if rows:
            actions.append(("r", menu_key("R", "emove")))
        actions.append(_BACK_ACTION)
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Link moderators", own=True,
                subtitle="Other nodes' users who moderate what this node originates.",
            ),
            sections=sections, actions=actions, message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Link moderators help",
            help_about=(
                "Users of other nodes who moderate what this node originates on Link, and below, what "
                "this node's users may moderate elsewhere. A grant covers one linked board or channel, a "
                "Link Community or everything this node originates, and every carrier accepts it. Your "
                "own moderators need no grant here."
            ),
            help_entries=_help_entries(actions, {
                "a": "Grant one; carriers learn it next pass",
                "r": "Asks once; carriers stop as it arrives",
            }),
        )
        message = None
        if choice == "b":
            return
        if choice == "a":
            message = await _add(session, lane, actor, link_context, chrome)
        elif choice == "r":
            message = await _remove(session, lane, actor, link_context, rows, chrome)


async def _add(session: Session, lane: DatabaseLane, actor: User, link_context: LinkContext, chrome) -> str | None:
    """One draft editor (design doc §3.5): `[U]ser`, `[O]n`, `[M]ay`, then
    `[S]ave`. Nothing is granted before Save, and the summary the editor
    keeps on screen is the confirmation; a grant is undone with `[R]emove`."""
    from netbbs.net.mail_flow import _resolve_link_address
    from netbbs.net.menu_description_preference import menu_description_level
    from netbbs.net.resource_editor import FieldSpec, edit_resource_draft, text_field

    own = link_context.node_identity.fingerprint
    outcome: list[str] = []
    draft: dict = {"user": "", "scope": None, "preset": 0}

    async def _scope_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        scope = await pick_item(
            session, await lane.run(own_scopes, own), name_of=lambda item: item.label,
            stable_id_of=lambda item: f"{item.kind}:{item.id}", title="Moderate what?",
            empty_message="This node originates nothing on NetBBS Link yet.",
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
            accent_color=chrome.accent_color, header_color=chrome.header_color,
        )
        if scope is not None:
            draft["scope"] = scope
            draft["preset"] = 0

    def _presets(d: dict) -> tuple:
        return _PRESETS[d["scope"].kind] if d["scope"] is not None else ()

    def _may_step(draft: dict, direction: int) -> None:
        presets = _presets(draft)
        if presets:
            draft["preset"] = (draft["preset"] + direction) % len(presets)

    async def _may_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        _may_step(draft, 1)

    def _may_render(d: dict) -> str:
        presets = _presets(d)
        return _permission_text(presets[d["preset"]]).capitalize() if presets else "(choose On first)"

    fields = [
        FieldSpec(
            key="user", label="User", render=lambda d: sanitize_text(d["user"]) or "(not chosen)",
            prompt=text_field("user", required=True), brief="name@TheirBBS",
            help="The user on another node, as name@TheirBBS -- the address Link mail reaches them at. "
                 "Checked when you save.",
        ),
        FieldSpec(
            key="scope", label="On", render=lambda d: d["scope"].label if d["scope"] is not None else "(not chosen)",
            prompt=_scope_prompt, brief="What they moderate",
            help="One linked board or channel this node originates, one of its Link Communities, or all of it.",
        ),
        FieldSpec(
            key="preset", label="May", render=_may_render, prompt=_may_prompt,
            step=_may_step,
            brief="What they may do there",
            help="Enter or Space steps through what fits On: a board, edit and remove posts or remove only; "
                 "a channel, mute, ban and set the topic or mute and ban only; a Community or everything, "
                 "all of it, posts only or channels only.",
        ),
    ]

    async def save(draft: dict) -> bool | None:
        if not draft["user"].strip() or draft["scope"] is None:
            raise LinkModeratorsError("Choose a User and what they moderate under On first.")
        resolved = await lane.run(_resolve_link_address, draft["user"].strip())
        if isinstance(resolved, str):
            raise LinkModeratorsError(resolved)
        scope = draft["scope"]
        permissions = _PRESETS[scope.kind][draft["preset"]]
        raw = await lane.run(
            grant_link_moderator, link_context.node_identity,
            home_node_fingerprint=resolved.fingerprint, local_user_id=resolved.user,
            scope_kind=scope.kind, scope_id=scope.id, permissions=list(permissions), actor=actor, label=scope.label,
        )
        _register(link_context, raw)
        who = link_address_label(resolved.user, (await lane.run(identity_for_fingerprint, resolved.fingerprint)).label)
        outcome.append(colored(
            f"{sanitize_text(who)} may now {_permission_text(permissions)} on {sanitize_text(scope.label)}. "
            "Carriers learn it with their next pass.",
            fg_color=SUCCESS_COLOR,
        ))
        return True

    await edit_resource_draft(
        session, lane, title="Add a Link moderator", own=True, fields=fields, draft=draft, save=save,
        error_type=LinkModeratorsError, save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    return outcome[0] if outcome else colored("Nothing granted.", fg_color=MUTED_COLOR)


async def _remove(
    session: Session, lane: DatabaseLane, actor: User, link_context: LinkContext, rows, chrome,
) -> str | None:
    picked = await pick_item(
        session, rows, name_of=lambda item: f"{item[1]} on {item[2]}", stable_id_of=lambda item: item[0].id,
        title="Remove which grant?", empty_message="No grants.",
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if picked is None:
        return None
    grant, who, scope = picked
    await session.write_line("")
    if not await prompt_yes_no(session, f"Remove {who}'s moderation of {scope}?", default=False):
        return colored("Grant kept.", fg_color=MUTED_COLOR)
    raw = await lane.run(revoke_link_moderator, link_context.node_identity, grant.id, actor=actor, label=scope)
    if raw is not None:
        _register(link_context, raw)
    return colored(f"{who} no longer moderates {scope}. Each carrier stops accepting it as the change arrives.",
                   fg_color=SUCCESS_COLOR)
