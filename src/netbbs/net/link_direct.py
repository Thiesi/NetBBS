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

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import list_channels
from netbbs.chat.nick import display_label
from netbbs.link.protocol import LinkProtocolError
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

UNREACHABLE_NOTE = (
    "can't be reached for live chat right now. Link mail still works: "
    "[M]ail from the main menu, addressed user@node-fingerprint."
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
    """The one known peer whose fingerprint starts with `node_prefix`
    (case-insensitively), or the list of candidates when it isn't
    unique/known. Live sessions and completed peers both count -- a
    relayed counterpart is a known peer this node holds no session
    with yet."""
    prefix = node_prefix.lower()
    candidates: list[str] = []
    seen: set[str] = set()
    sources: list[str] = list(link_context.link_node.peers)
    if link_context.realtime_registry is not None:
        sources += [s.remote_fingerprint for s in link_context.realtime_registry.all_sessions()]
    for fingerprint in sources:
        if fingerprint.lower().startswith(prefix) and fingerprint not in seen:
            seen.add(fingerprint)
            candidates.append(fingerprint)
    if len(candidates) == 1:
        return candidates[0]
    return candidates


async def send_live_direct_message(
    session: Session,
    lane: DatabaseLane,
    user: User,
    address: str,
    body: str,
    *,
    link_context: LinkContext | None,
) -> bool:
    """Send `body` to `address` (`user@node`) live. Writes the outcome to
    `session`; returns whether it was sent."""
    parsed = parse_remote_address(address)
    if parsed is None:
        await session.write_line(colored("Address a linked node's user as user@node-fingerprint.", fg_color=MUTED_COLOR))
        return False
    target_user, node_prefix = parsed
    if link_context is None or link_context.direct_chat is None or link_context.realtime_bridge is None:
        await session.write_line(colored("This node isn't on NetBBS Link, so there is nobody remote to message.", fg_color=MUTED_COLOR))
        return False
    if not body.strip():
        await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
        return False
    if len(body.encode("utf-8")) > MAX_DIRECT_MESSAGE_BODY_BYTES:
        await session.write_line(
            colored(f"Message too long -- a live message is at most {MAX_DIRECT_MESSAGE_BODY_BYTES} bytes.", fg_color=MUTED_COLOR)
        )
        return False
    if len(target_user.encode("utf-8")) > MAX_REMOTE_USER_BYTES:
        await session.write_line(colored("That user name is too long to be a NetBBS account.", fg_color=MUTED_COLOR))
        return False

    resolved = resolve_node_fingerprint(link_context, node_prefix)
    if isinstance(resolved, list):
        if not resolved:
            await session.write_line(
                colored(f"No linked node this board knows starts with {sanitize_text(node_prefix)!r}.", fg_color=MUTED_COLOR)
            )
        else:
            shown = ", ".join(sanitize_text(fp[:16]) + "…" for fp in resolved[:5])
            await session.write_line(
                colored(f"{sanitize_text(node_prefix)!r} matches more than one node ({shown}) -- give more of the fingerprint.", fg_color=MUTED_COLOR)
            )
        return False
    fingerprint = resolved
    label = f"{sanitize_text(target_user)}@{sanitize_text(fingerprint[:12])}…"

    direct_chat = link_context.direct_chat
    bridge = link_context.realtime_bridge
    try:
        await direct_chat.ensure_session(fingerprint)
    except DirectChatUnreachable:
        await session.write_line(colored(f"{label} {UNREACHABLE_NOTE}", fg_color=MUTED_COLOR))
        return False

    # The peer pushes its node-presence snapshot as soon as it tracks the
    # session; give a brand-new session a moment to deliver it so "not
    # online there" is an honest answer, not a race.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PRESENCE_SETTLE_SECONDS
    while fingerprint not in bridge.remote_node_presence() and loop.time() < deadline:
        await asyncio.sleep(0.05)
    online = bridge.remote_node_presence().get(fingerprint)
    if online is None:
        # No presence from that node yet: never claim delivery blind.
        await session.write_line(
            colored(f"Couldn't confirm who is online at {sanitize_text(fingerprint[:12])}… just now -- try again in a moment.", fg_color=MUTED_COLOR)
        )
        return False
    if target_user.lower() not in {name.lower() for name in online}:
        await session.write_line(colored(f"{label} is not currently online on that node.", fg_color=MUTED_COLOR))
        return False

    sender_label = sanitize_text(await lane.run(display_label, user))
    try:
        await direct_chat.send_direct_message(
            fingerprint, to_user_id=target_user, from_user_id=user.username, from_display_label=sender_label,
            body=body, created_at=utc_now_iso(),
        )
    except (DirectChatUnreachable, LinkTransportError):
        await session.write_line(colored(f"{label} {UNREACHABLE_NOTE}", fg_color=MUTED_COLOR))
        return False
    except LinkProtocolError as exc:
        await session.write_line(colored(f"Couldn't send that: {sanitize_text(str(exc))}", fg_color=MUTED_COLOR))
        return False
    await session.write_line(colored(f"(sent to {label})", fg_color=MUTED_COLOR))
    return True


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
                return None, []
            if not accepts_direct_messages(db, target):
                return None, []
            live = [
                (channel.name, pid)
                for channel in list_channels(db)
                for pid in hub.participants_for_username(channel.name, target.username)
            ]
            return target, live

        target, live = await lane.run(_lookup)
        if target is None or not presence.is_online(target.username):
            return False
        origin = f"{sanitize_text(message.from_display_label)}@{sanitize_text(message.from_node_fingerprint[:12])}…"
        notice = colored(
            f"*** Private message from {origin}: {sanitize_text(message.body)}", fg_color=MUTED_COLOR, bold=True,
        )
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
