"""
UI-layer glue for Link-wide live direct messages (issue #168, design doc
§8.10.3): the shared "send one private line to `user@node`" flow that
chat's `/msg` and Who's online both call, and the receiving-side
deliverer `netbbs.__main__` hands to `netbbs.link.realtime_direct.
LiveDirectChat` -- built from the same hub/mailbox/session-registry
plumbing the local `/msg` already uses, so a remote message lands in a
caller's session exactly the way a local one does.

Decision 3 (§16, issue #168) governs every failure here: a target that
can't be reached live gets one explicit, reason-free refusal pointing at
Link mail; nothing fails silently and nothing pretends to have sent.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from enum import Enum

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import list_channels
from netbbs.chat.nick import display_label
from netbbs.link.protocol import LinkProtocolError, RealtimeProtocolVersionError
from netbbs.link.node_profiles import (
    identity_for_fingerprint,
    identity_for_peer,
    latest_identity_observation,
    resolve_peer_reference,
)
from netbbs.link.realtime_direct import DirectChatUnreachable, IncomingDirectMessage
from netbbs.link.transport import LinkTransportError
from netbbs.messaging_preferences import accepts_direct_messages
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

if TYPE_CHECKING:
    from netbbs.link.boards import LinkContext

# How long to wait for a freshly established session's node-presence
# snapshot before deciding whether the target user is online there --
# the peer sends it the moment it tracks the session, so this is a
# scheduling delay, not a network round trip.
_PRESENCE_SETTLE_SECONDS = 1.0

class SendOutcome(str, Enum):
    """What `send_live_direct_message` did. `LINE_REJECTED` means *this
    line* was refused (too long, blank, a bad address) while the
    counterpart may be perfectly reachable -- a private-conversation mode
    must stay open on it; `UNREACHABLE` means the counterpart itself
    cannot be reached or is not online, which ends the mode."""

    SENT = "sent"
    LINE_REJECTED = "line_rejected"
    UNREACHABLE = "unreachable"


UNREACHABLE_NOTE = (
    "can't be reached for live chat right now. Link mail still works: "
    "[M]ail from the main menu, addressed user@node-name-or-dns."
)
# The wire bounds (netbbs.link.protocol's direct_message validator), checked
# here first so a caller gets a plain notice instead of a protocol error
# escaping the chat loop.
MAX_DIRECT_MESSAGE_BODY_BYTES = 4000
MAX_REMOTE_USER_BYTES = 128


def parse_remote_address(text: str) -> tuple[str, str] | None:
    """`user@node` -> `(user, node_prefix)`; `None` if it isn't one."""
    if "@" not in text:
        return None
    user, _, node = text.partition("@")
    user, node = user.strip(), node.strip()
    if not user or not node:
        return None
    return user, node


def resolve_node_fingerprint(link_context: LinkContext, node_prefix: str) -> str | list[str]:
    """Resolve a DNS name, unique friendly name, or legacy fingerprint prefix."""
    needle = node_prefix.strip().lower()
    exact = {
        fingerprint for fingerprint in link_context.link_node.peers
        if fingerprint.lower() == needle
    }
    if link_context.realtime_registry is not None:
        exact.update(
            active.remote_fingerprint
            for active in link_context.realtime_registry.all_sessions()
            if active.remote_fingerprint.lower() == needle
        )
    if len(exact) == 1:
        return next(iter(exact))
    peer_result = resolve_peer_reference(link_context.link_node.peers.values(), node_prefix)
    if not isinstance(peer_result, list):
        return peer_result.fingerprint
    candidates = [peer.fingerprint for peer in peer_result]
    seen = set(candidates)
    for fingerprint in link_context.link_node.peers:
        if fingerprint.lower().startswith(node_prefix.lower()) and fingerprint not in seen:
            candidates.append(fingerprint)
            seen.add(fingerprint)
    if link_context.realtime_registry is not None:
        for active in link_context.realtime_registry.all_sessions():
            fingerprint = active.remote_fingerprint
            if fingerprint.lower().startswith(node_prefix.lower()) and fingerprint not in seen:
                candidates.append(fingerprint)
                seen.add(fingerprint)
    if len(candidates) == 1:
        return candidates[0]
    return candidates


def _node_label(link_context: LinkContext, fingerprint: str) -> str:
    peer = link_context.link_node.peers.get(fingerprint)
    return identity_for_peer(peer).label if peer is not None else "linked node"


async def check_live_reachability(
    session: Session,
    address: str,
    *,
    link_context: LinkContext | None,
) -> str | None:
    """Resolve `address` (`user@node`), establish (or reuse) a live
    session with that node, and confirm the user is online there. Writes
    every refusal to `session` (Decision 3's reason-free note included)
    and returns the node fingerprint on success, `None` otherwise. Shared
    by the one-off send and by `/private`, which must know the
    conversation is viable *before* it tells the caller it has begun."""
    parsed = parse_remote_address(address)
    if parsed is None:
        await session.write_line(colored("Address a linked node's user as user@node-name-or-dns.", fg_color=MUTED_COLOR))
        return None
    target_user, node_prefix = parsed
    if link_context is None or link_context.direct_chat is None or link_context.realtime_bridge is None:
        await session.write_line(colored("This node isn't on NetBBS Link, so there is nobody remote to message.", fg_color=MUTED_COLOR))
        return None
    if len(target_user.encode("utf-8")) > MAX_REMOTE_USER_BYTES:
        await session.write_line(colored("That user name is too long to be a NetBBS account.", fg_color=MUTED_COLOR))
        return None
    resolved = resolve_node_fingerprint(link_context, node_prefix)
    if isinstance(resolved, list):
        if not resolved:
            await session.write_line(
                colored(f"No linked node this board knows as {sanitize_text(node_prefix)!r}.", fg_color=MUTED_COLOR)
            )
        else:
            shown = ", ".join(sanitize_text(_node_label(link_context, fp)) for fp in resolved[:5])
            await session.write_line(
                colored(f"{sanitize_text(node_prefix)!r} matches more than one node ({shown}) -- use its DNS name.", fg_color=MUTED_COLOR)
            )
        return None
    fingerprint = resolved
    node_label = _node_label(link_context, fingerprint)
    label = f"{sanitize_text(target_user)}@{sanitize_text(node_label)}"
    direct_chat = link_context.direct_chat
    bridge = link_context.realtime_bridge
    try:
        await direct_chat.ensure_session(fingerprint)
    except DirectChatUnreachable:
        await session.write_line(colored(f"{label} {UNREACHABLE_NOTE}", fg_color=MUTED_COLOR))
        return None
    except RealtimeProtocolVersionError:
        await session.write_line(colored(
            f"{sanitize_text(node_label)} uses an incompatible real-time protocol version -- "
            "upgrade one of the nodes. Link mail still works.", fg_color=MUTED_COLOR,
        ))
        return None
    # The peer pushes its node-presence snapshot as soon as it tracks the
    # session; give a brand-new session a moment to deliver it so "not
    # online there" is an honest answer, not a race.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PRESENCE_SETTLE_SECONDS
    while fingerprint not in bridge.remote_node_presence() and loop.time() < deadline:
        await asyncio.sleep(0.05)
    online = bridge.remote_node_presence().get(fingerprint)
    if online is None:
        await session.write_line(
            colored(f"Couldn't confirm who is online at {sanitize_text(node_label)} just now -- try again in a moment.", fg_color=MUTED_COLOR)
        )
        return None
    if target_user.lower() not in {name.lower() for name in online}:
        await session.write_line(colored(f"{label} is not currently online on that node.", fg_color=MUTED_COLOR))
        return None
    return fingerprint


async def send_live_direct_message(
    session: Session,
    lane: DatabaseLane,
    user: User,
    address: str,
    body: str,
    *,
    link_context: LinkContext | None,
) -> SendOutcome:
    """Send `body` to `address` (`user@node`) live. Writes the outcome to
    `session` and returns it (see `SendOutcome`)."""
    parsed = parse_remote_address(address)
    if parsed is None:
        await session.write_line(colored("Address a linked node's user as user@node-name-or-dns.", fg_color=MUTED_COLOR))
        return SendOutcome.LINE_REJECTED
    target_user, _node_prefix = parsed
    if not body.strip():
        await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
        return SendOutcome.LINE_REJECTED
    if len(body.encode("utf-8")) > MAX_DIRECT_MESSAGE_BODY_BYTES:
        await session.write_line(
            colored(f"Message too long -- a live message is at most {MAX_DIRECT_MESSAGE_BODY_BYTES} bytes.", fg_color=MUTED_COLOR)
        )
        return SendOutcome.LINE_REJECTED
    if len(target_user.encode("utf-8")) > MAX_REMOTE_USER_BYTES:
        await session.write_line(colored("That user name is too long to be a NetBBS account.", fg_color=MUTED_COLOR))
        return SendOutcome.LINE_REJECTED
    fingerprint = await check_live_reachability(session, address, link_context=link_context)
    if fingerprint is None:
        return SendOutcome.UNREACHABLE
    assert link_context is not None and link_context.direct_chat is not None
    label = f"{sanitize_text(target_user)}@{sanitize_text(_node_label(link_context, fingerprint))}"
    identity_notice = await lane.run(latest_identity_observation, fingerprint)
    if identity_notice is not None:
        if identity_notice.severity == "security":
            warning = (
                "Caution: this node name now has a different cryptographic identity. "
                "It may have been legitimately replaced or recovered; proceed only if that change is expected."
            )
        else:
            warning = (
                "Note: this node's displayed name or DNS name changed, while its cryptographic identity stayed the same."
            )
        await session.write_line(colored(warning, fg_color=MUTED_COLOR, bold=identity_notice.severity == "security"))
    direct_chat = link_context.direct_chat
    resolved = fingerprint

    sender_label = sanitize_text(await lane.run(display_label, user))
    try:
        await direct_chat.send_direct_message(
            fingerprint, to_user_id=target_user, from_user_id=user.username, from_display_label=sender_label,
            body=body, created_at=utc_now_iso(),
        )
    except (DirectChatUnreachable, LinkTransportError):
        await session.write_line(colored(f"{label} {UNREACHABLE_NOTE}", fg_color=MUTED_COLOR))
        return SendOutcome.UNREACHABLE
    except LinkProtocolError as exc:
        await session.write_line(colored(f"Couldn't send that: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
        return SendOutcome.LINE_REJECTED
    await session.write_line(colored(f"(sent to {label})", fg_color=MUTED_COLOR))
    return SendOutcome.SENT


def build_direct_message_deliverer(
    *,
    lane: DatabaseLane,
    hub: ChatHub,
    mailbox: MessageMailbox,
    session_registry: ActiveSessionRegistry | None,
    presence: PresenceRegistry,
):
    """The receiving side (module docstring): returns the `deliver`
    callable `LiveDirectChat` calls for every accepted `direct_message`.
    Delivery mirrors `netbbs.net.chat_flow._deliver_private_message`:
    instantly through the hub for the recipient's live chat sessions,
    queued in the mailbox for every other session. Returns whether any
    session received it; an unknown, opted-out, or offline recipient is
    dropped -- the sender already checked presence and is told nothing
    more (§12: a remote node's user list is not a caller's to probe)."""
    # Imported here, not at module top: chat_flow imports this module
    # lazily for /msg, and importing it eagerly back would be a cycle.
    from netbbs.net.chat_flow import _TimestampedNotice

    async def deliver(message: IncomingDirectMessage) -> bool:
        def _lookup(db: Database):
            try:
                target = get_user_by_username(db, message.to_user_id)
            except AuthError:
                return None, [], "linked node", None
            if not accepts_direct_messages(db, target):
                return None, [], "linked node", None
            live = [
                (channel.name, pid)
                for channel in list_channels(db)
                for pid in hub.participants_for_username(channel.name, target.username)
            ]
            return (
                target,
                live,
                identity_for_fingerprint(db, message.from_node_fingerprint).label,
                latest_identity_observation(db, message.from_node_fingerprint),
            )

        target, live, node_label, identity_notice = await lane.run(_lookup)
        if target is None or not presence.is_online(target.username):
            return False
        origin = f"{sanitize_text(message.from_display_label)}@{sanitize_text(node_label)}"
        notice = colored(
            f"*** Private message from {origin}: {sanitize_text(message.body)}", fg_color=MUTED_COLOR, bold=True,
        )
        if identity_notice is not None and identity_notice.severity == "security":
            notice = colored(
                "*** Caution: this familiar node name has a different cryptographic identity. ",
                fg_color=MUTED_COLOR,
                bold=True,
            ) + notice
        delivered = False
        live_keys = {pid.session_key for _channel, pid in live}
        for channel_name, pid in live:
            await hub.send_to(channel_name, pid, _TimestampedNotice(notice, message.created_at))
            delivered = True
        if session_registry is not None:
            for target_session in session_registry.sessions_for_username(target.username):
                if id(target_session) in live_keys:
                    continue
                mailbox.deliver(target_session, notice, message.created_at)
                delivered = True
        return delivered

    return deliver
