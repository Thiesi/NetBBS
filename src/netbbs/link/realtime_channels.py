"""
Live linked-channel chat bridge (design doc §8.10.2, issue #148's first
vertical) -- the seam between `netbbs.link.transport`'s Noise session
layer (which knows nothing about channels) and `netbbs.chat`'s local
domain state (which knows nothing about Link).

`LiveChannelBridge` is one instance per running node, shared by every
`LinkRealtimeSession` this node holds in either direction (the same
`on_frame` callback goes to `LinkRealtimeServer`, `dial_realtime_
session`, and `LinkRealtimeConnector` alike). It owns exactly the state
a bare session doesn't know about: which peer sessions are currently
subscribed to which of *this node's own* channels. Inbound frames turn
into the same `netbbs.chat.hub.ChatHub` broadcast a local participant's
own message/join/leave already produces -- a remote live event renders
through the exact same path (`_render_channel_message`) scrollback
replay does, per that renderer's own docstring (GitHub issue #64). This
module never touches `channel_messages`/scrollback itself: a live frame
is ephemeral by design (§8.10), never a substitute for the durable
async `channel_message` event `netbbs.link.channels.queue_channel_
message_if_linked` already queues separately.

Deliberately does not decide *when* to dial a peer on its own --
`ensure_live_subscription` is a plain function a caller (a channel-join
flow) invokes; nothing here runs a background connector loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from netbbs.chat.channels import Channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import ChannelMessage as LocalChannelMessage
from netbbs.chat.scrollback import get_scrollback
from netbbs.link.channels import channel_origin_fingerprint, get_channel_by_channel_id, is_channel_linked
from netbbs.link.enforcement import (
    LinkPolicyAction,
    content_visible_for_subject,
    decide_node_action,
    ensure_node_subject,
    event_author,
    node_transport_state,
)
from netbbs.link.events import ChannelMessage as LinkChannelMessage
from netbbs.link.node_identity import NodeIdentity
from netbbs.link.protocol import (
    LinkNode,
    LinkProtocolError,
    RealtimeProtocolVersionError,
    RealtimeFrame,
    build_channel_message_frame,
    build_node_presence_delta_frame,
    build_node_presence_snapshot_frame,
    build_presence_delta_frame,
    build_presence_snapshot_frame,
    build_scrollback_snapshot_frame,
    build_subscribe_frame,
    new_realtime_message_id,
)
from netbbs.link.trust import TrustState, TrustSubject
from netbbs.link.transport import (
    LinkRealtimeSession,
    LinkRealtimeSessionRegistry,
    LinkTransportError,
    dial_realtime_session,
    dialable_realtime_addresses_for_peer,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

# design doc §8.10.1: "Subscriptions, remote presence entries ... are
# bounded per peer and node."
_MAX_SUBSCRIBERS_PER_CHANNEL = 200
_MAX_PRESENCE_SNAPSHOT_ENTRIES = 200

# Issue #194: how many of this node's own most-recent local scrollback
# entries to offer a freshly-subscribing peer, and how much of each
# entry's body to send. Must not exceed protocol.py's own
# _REALTIME_MAX_SCROLLBACK_SNAPSHOT_ENTRIES/_REALTIME_MAX_SCROLLBACK_
# ENTRY_BODY_BYTES ceilings (kept as separate, non-imported constants the
# same way _MAX_PRESENCE_SNAPSHOT_ENTRIES already duplicates rather than
# imports protocol.py's own presence-entry bound) -- that module's
# validator is the actual hard enforcement; this is just how much this
# side chooses to offer, and a mismatch would only ever make an outbound
# snapshot smaller than allowed, never invalid.
_MAX_SCROLLBACK_SNAPSHOT_ENTRIES = 20
_MAX_SCROLLBACK_ENTRY_BODY_BYTES = 400


def _bounded_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut `text` down to at most `max_bytes` UTF-8 bytes without
    splitting a multi-byte character in two. Issue #194's scrollback
    snapshot bundles many entries into one 16 KiB-bounded frame (unlike a
    single live channel_message), so a body that easily fits on its own
    may still need shortening here -- acceptable because this is a
    "catch-up" glance, not a substitute for the full durable copy already
    on its way through the existing async materialization path."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _snapshot_entries(db: Database, channel: Channel) -> list[dict]:
    """Build origin-attested entries with enough author identity for the
    subscriber to apply its own trust policy. Carried-message identities
    are extracted from the signed event accepted by this node; origin-local
    labels are explicitly qualified so they cannot resolve as subscriber-
    local accounts with the same username."""
    origin_fingerprint = channel_origin_fingerprint(db, channel)
    if origin_fingerprint is None:
        return []
    entries: list[dict] = []
    for message in get_scrollback(db, channel)[-_MAX_SCROLLBACK_SNAPSHOT_ENTRIES:]:
        author_node_fingerprint: str | None = None
        author_user_id: str | None = None
        author_label = message.author_label
        content_id = message.link_content_id
        if message.kind not in {"mute", "unmute", "ban", "unban", "kick", "daybreak"}:
            if message.link_content_id is None:
                author_node_fingerprint = origin_fingerprint
                author_user_id = message.author_label
                author_label = f"{message.author_label}@{origin_fingerprint}"
                if message.link_event_json is not None:
                    try:
                        content_id = LinkChannelMessage.from_dict(
                            json.loads(message.link_event_json)
                        ).content_id
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        content_id = None
            else:
                row = db.connection.execute(
                    "SELECT envelope_json FROM link_events WHERE content_id = ?",
                    (message.link_content_id,),
                ).fetchone()
                try:
                    subject = event_author(json.loads(row[0])) if row is not None else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    subject = None
                if subject is None or subject.kind != "user" or subject.opaque_user_id is None:
                    continue
                author_node_fingerprint = subject.node_fingerprint
                author_user_id = subject.opaque_user_id
        body, body_truncated = (
            _bounded_utf8(message.body, _MAX_SCROLLBACK_ENTRY_BODY_BYTES)
            if message.body is not None else (None, False)
        )
        entries.append({
            "kind": message.kind,
            "author_label": author_label,
            "author_node_fingerprint": author_node_fingerprint,
            "author_user_id": author_user_id,
            "content_id": content_id,
            "body": body,
            "body_truncated": body_truncated,
            "created_at": message.created_at,
        })
    return entries


def _accept_scrollback_snapshot(
    db: Database, *, channel_id: str, peer_fingerprint: str, entries: list[dict]
) -> tuple[Channel, list[LocalChannelMessage]]:
    channel = _decide_channel_subscribe_authorization(
        db, channel_id=channel_id, peer_fingerprint=peer_fingerprint
    )
    if channel_origin_fingerprint(db, channel) != peer_fingerprint:
        raise LinkProtocolError("scrollback_snapshot sender is not the channel's current origin")
    messages = []
    for entry in entries:
        authored = entry["author_node_fingerprint"] is not None
        if authored:
            home = node_transport_state(db, entry["author_node_fingerprint"])
            if home in {TrustState.BLOCKED, TrustState.QUARANTINED}:
                continue
            subject = TrustSubject.user(entry["author_node_fingerprint"], entry["author_user_id"])
            if not content_visible_for_subject(db, subject):
                continue
        author_label = (
            f"{entry['author_user_id']}@{entry['author_node_fingerprint']}"
            if authored else entry["author_label"]
        )
        messages.append(LocalChannelMessage(
            id=-1, channel_id=channel.id, kind=entry["kind"],
            author_label=author_label, author_fingerprint=None,
            body=entry["body"], created_at=entry["created_at"],
            link_content_id=entry["content_id"], body_truncated=entry["body_truncated"],
        ))
    return channel, messages


def _decide_channel_subscribe_authorization(db: Database, *, channel_id: str, peer_fingerprint: str) -> Channel:
    """Design doc §8.10.2: "checks that the channel exists, is linked,
    is locally allowed by trust policy, and is available to the
    subscribing peer" -- called again on every delivered message, not
    only at subscribe time, since a subscription is not a permanent
    grant. Raises `LinkProtocolError` (a bounded strike at the session
    layer, not a hard close -- see `LinkRealtimeSession._reader_loop`)
    for any failure, deliberately without distinguishing which reason
    applies: none of "unknown channel," "not linked," or "not trusted"
    are anything a rejected peer needs to be told apart."""
    channel = get_channel_by_channel_id(db, channel_id)
    if channel is None or not is_channel_linked(db, channel):
        raise LinkProtocolError(f"channel {channel_id!r} is not a linked channel this node carries")
    ensure_node_subject(db, peer_fingerprint)
    if not decide_node_action(db, peer_fingerprint, LinkPolicyAction.REALTIME).allowed:
        raise LinkProtocolError(f"node {peer_fingerprint!r} is not currently allowed real-time traffic")
    return channel


class LiveChannelBridge:
    """See module docstring. `hub`/`lane` are this node's own
    already-running singletons (one `ChatHub`, one `DatabaseLane`) --
    this class adds no storage or broadcast mechanism of its own.

    `presence`/`registry` (issue #164) are this node's own already-
    running `PresenceRegistry`/`LinkRealtimeSessionRegistry` singletons,
    needed for node-wide (not channel-scoped) presence: `presence`
    answers "who's actually online on this node right now" the same way
    the local Who's Online screen already does, and `registry` is what
    lets a broadcast reach every currently-connected peer session, not
    just channel subscribers."""

    def __init__(
        self, *, hub: ChatHub, lane: DatabaseLane, presence: PresenceRegistry, registry: LinkRealtimeSessionRegistry
    ) -> None:
        self._hub = hub
        self._lane = lane
        self._presence = presence
        self._registry = registry
        # channel_id -> {peer_fingerprint: session}
        self._subscribers: dict[str, dict[str, LinkRealtimeSession]] = {}
        self._watchers: set[asyncio.Task] = set()
        # peer_fingerprint -> {user_id: display_label} -- the last-known
        # node-wide presence snapshot/delta stream received from that
        # peer. Cleared for a peer the moment its session closes
        # (_untrack_on_close) -- stale presence from a now-disconnected
        # peer must not linger.
        self._remote_node_presence: dict[str, dict[str, str]] = {}
        # channel_id -> {user_id: display_label} -- issue #195's merged
        # live roster: the last-known remote roster for a linked channel
        # this node subscribes to (received via presence_snapshot/delta
        # from that channel's origin). `_remote_channel_presence_source`
        # tracks which peer fingerprint populated each entry, so a
        # disconnect can clear only the channels that peer actually owns
        # -- one subscriber session is shared across every channel from
        # the same origin (`ensure_live_subscription`'s own registry
        # reuse), so this can't simply key on session identity alone.
        self._remote_channel_presence: dict[str, dict[str, str]] = {}
        self._remote_channel_presence_source: dict[str, str] = {}
        # channel_id -> pending scrollback_snapshot entries (issue #194),
        # a one-time pickup for whichever local caller's subscribe flow is
        # currently waiting on it (netbbs.net.chat_flow._subscribe_live).
        # Popped, not merely read, by pop_channel_scrollback -- design doc
        # §16 Decision 2: "rendered once, never durably stored on the
        # subscribing side. Unlike presence, snapshots are also correlated
        # to the exact subscribe attempt so a late response can never be
        # consumed by a later caller.
        # request_id -> (channel_id, expected origin) and response entries.
        # Correlation prevents a late reply from one completed join attempt
        # from being consumed by a later caller joining the same channel.
        self._pending_scrollback_requests: dict[str, tuple[str, str]] = {}
        self._remote_channel_scrollback: dict[str, list[LocalChannelMessage]] = {}
        # peer_fingerprint -> whether this node has already sent that
        # peer its initial node_presence_snapshot for the *current*
        # session -- track_session can be called more than once per
        # session (existing convention), but the snapshot must only go
        # out once per connection, not once per call.
        self._node_presence_sent: set[str] = set()
        # channel_id -> opaque holder ids (issue #159): this node's own
        # *subscriber*-side interest in a linked channel it doesn't
        # originate -- the mirror image of `_subscribers` above, which is
        # the *origin*-side "who subscribes to my channels" bookkeeping.
        # Needed because a live subscription to a remote origin is a
        # node-level resource (one `LinkRealtimeSession` per remote
        # fingerprint, shared by every local caller who wants it), but
        # `_chat_loop` used to send `unsubscribe` unconditionally on
        # leaving one channel view -- correct only when exactly one local
        # party ever cared about that channel at a time, wrong the moment
        # a second caller (or a caller's own background subscription
        # alongside another active view) is also relying on the same
        # feed: the first to leave would silently cut off live delivery
        # for everyone else still interested.
        self._local_interest: dict[str, set[int]] = {}
        # Issue #168: frames that are not this bridge's own business --
        # relay rendezvous (`netbbs.link.realtime_relay`) and live direct
        # messages (`netbbs.link.realtime_direct`) -- are routed to the
        # first registered handler that claims them. The bridge stays the
        # single `on_frame` seam every session is constructed with; the
        # other components plug in rather than each needing a session
        # hook of their own.
        self._frame_handlers: list[tuple[
            Callable[[LinkRealtimeSession, RealtimeFrame], bool],
            Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
        ]] = []

    def register_frame_handler(
        self,
        owns: Callable[[LinkRealtimeSession, RealtimeFrame], bool],
        handle: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
    ) -> None:
        self._frame_handlers.append((owns, handle))

    def register_local_interest(self, channel_id: str, holder: int) -> bool:
        """Record that `holder` (a caller's own `id(session)`) wants live
        delivery for `channel_id`. Returns whether this is the *first*
        local holder -- informational only, since re-sending `subscribe`
        to an origin that's already subscribed is harmless and idempotent
        (`_handle_subscribe` just re-registers), so callers aren't
        required to gate that frame on this return value the way they
        must gate `unsubscribe` on `release_local_interest`'s."""
        holders = self._local_interest.setdefault(channel_id, set())
        first = not holders
        holders.add(holder)
        return first

    def release_local_interest(self, channel_id: str, holder: int) -> bool:
        """Record that `holder` no longer wants live delivery for
        `channel_id`. Returns whether `holder` was the *last* local
        holder -- the caller must send `unsubscribe` to the origin only
        when this is `True`; otherwise some other local caller/view is
        still relying on the same feed and unsubscribing would silently
        cut them off. A `holder` that was never registered (e.g. this
        caller never actually got a live session) is a safe no-op,
        returning `False`."""
        holders = self._local_interest.get(channel_id)
        if holders is None or holder not in holders:
            return False
        holders.discard(holder)
        last = not holders
        if last:
            del self._local_interest[channel_id]
        return last

    async def track_session(self, session: LinkRealtimeSession) -> None:
        """Spawn the bounded (one per session) watcher that purges
        `session` from every channel's subscriber set once it closes --
        a peer that disconnects without unsubscribing must not linger
        as a phantom subscriber forever. Safe to call more than once for
        the same session (a no-op watcher only ever removes what it
        finds).

        Also sends that peer an initial `node_presence_snapshot` --
        push-on-connect (issue #164), exactly once per session even if
        this is called more than once for it. Best-effort: a session
        that's already gone by the time this runs is handled the same
        way every other outbound send in this class handles a dead
        session, and `_untrack_on_close`'s own watcher (already spawned
        above) will clean up `_node_presence_sent` once `session.closed`
        actually fires."""
        watcher = asyncio.get_running_loop().create_task(self._untrack_on_close(session))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

        if session.remote_fingerprint in self._node_presence_sent:
            return
        self._node_presence_sent.add(session.remote_fingerprint)
        entries = [
            {"user_id": username, "display_label": username}
            for username in sorted(self._presence.online_usernames())
        ][:_MAX_PRESENCE_SNAPSHOT_ENTRIES]
        try:
            await session.send(build_node_presence_snapshot_frame(entries))
        except LinkTransportError:
            pass

    async def _untrack_on_close(self, session: LinkRealtimeSession) -> None:
        await session.closed.wait()
        for channel_id, subscribers in list(self._subscribers.items()):
            if subscribers.pop(session.remote_fingerprint, None) is not None and not subscribers:
                del self._subscribers[channel_id]
        self._remote_node_presence.pop(session.remote_fingerprint, None)
        self._node_presence_sent.discard(session.remote_fingerprint)
        for channel_id, fingerprint in list(self._remote_channel_presence_source.items()):
            if fingerprint == session.remote_fingerprint:
                del self._remote_channel_presence_source[channel_id]
                self._remote_channel_presence.pop(channel_id, None)
        for request_id, (_, fingerprint) in list(self._pending_scrollback_requests.items()):
            if fingerprint == session.remote_fingerprint:
                self.finish_scrollback_request(request_id)

    async def close(self) -> None:
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)

    async def on_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        if frame.type == "subscribe":
            await self._handle_subscribe(session, frame)
        elif frame.type == "unsubscribe":
            self._handle_unsubscribe(session, frame)
        elif frame.type == "channel_message":
            await self._handle_channel_message(session, frame)
        elif frame.type == "scrollback_snapshot":
            await self._handle_scrollback_snapshot(session, frame)
        elif frame.type == "presence_delta":
            await self._handle_presence_delta(session, frame)
        elif frame.type == "presence_snapshot":
            await self._handle_presence_snapshot(session, frame)
        elif frame.type == "node_presence_snapshot":
            await self._handle_node_presence_snapshot(session, frame)
        elif frame.type == "node_presence_delta":
            await self._handle_node_presence_delta(session, frame)
        else:
            for owns, handle in self._frame_handlers:
                if owns(session, frame):
                    await handle(session, frame)
                    return
            # "error": nothing actionable locally yet from a peer-reported
            # rejection of a frame this node sent. A relay/direct frame
            # with no handler registered (a node that doesn't run those
            # components) is ignored the same way -- never a strike.

    async def _handle_subscribe(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        channel_id = frame.payload["channel_id"]
        channel = await self._lane.run(
            _decide_channel_subscribe_authorization, channel_id=channel_id,
            peer_fingerprint=session.remote_fingerprint,
        )
        subscribers = self._subscribers.setdefault(channel_id, {})
        if session.remote_fingerprint not in subscribers and len(subscribers) >= _MAX_SUBSCRIBERS_PER_CHANNEL:
            raise LinkProtocolError(f"channel {channel_id!r} is already at its live-subscriber limit")
        subscribers[session.remote_fingerprint] = session
        await self.track_session(session)

        seen_usernames: set[str] = set()
        entries = []
        for participant in self._hub.participant_ids(channel.name):
            if participant.username in seen_usernames:
                continue
            seen_usernames.add(participant.username)
            entries.append({"user_id": participant.username, "display_label": participant.username})
        await session.send(
            build_presence_snapshot_frame(channel_id, entries[:_MAX_PRESENCE_SNAPSHOT_ENTRIES])
        )
        await self._send_scrollback_snapshot(session, channel, request_id=frame.message_id)

    async def _send_scrollback_snapshot(
        self, session: LinkRealtimeSession, channel: Channel, *, request_id: str
    ) -> None:
        """Issue #194 Decision 1: a sibling frame sent right alongside the
        presence_snapshot above, at the same call site, sourced from this
        node's own already-bounded, origin-policy-filtered local
        scrollback (`netbbs.chat.scrollback.get_scrollback`). Entries also
        preserve author identity for the subscriber's independent policy
        check. Best-
        effort and silent on failure: an empty local scrollback, a
        validation failure (this node's own bound choices above are meant
        to prevent that, but nothing here depends on them being perfect),
        or a transport failure must never surface as an error to the
        subscribing peer or block anything else `_handle_subscribe`
        already did -- the existing async catch-up path is still there
        regardless (design doc §16)."""
        entries = await self._lane.run(_snapshot_entries, channel)
        if not entries:
            return
        # JSON escaping can make valid per-field values exceed the frame
        # ceiling. Drop oldest catch-up entries until the fully serialized
        # frame fits; never enqueue a frame that will later kill the session.
        frame = None
        while entries:
            try:
                frame = build_scrollback_snapshot_frame(
                    channel.channel_id, request_id, entries
                )
                break
            except LinkProtocolError:
                entries.pop(0)
        if frame is None:
            return
        try:
            await session.send(frame)
        except LinkTransportError:
            pass

    async def _handle_scrollback_snapshot(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        channel, messages = await self._lane.run(
            _accept_scrollback_snapshot, channel_id=frame.payload["channel_id"],
            peer_fingerprint=session.remote_fingerprint, entries=frame.payload["entries"],
        )
        request_id = frame.payload["request_id"]
        if self._pending_scrollback_requests.get(request_id) != (
            channel.channel_id, session.remote_fingerprint
        ):
            return
        self._remote_channel_scrollback[request_id] = messages

    def begin_scrollback_request(self, channel_id: str, origin_fingerprint: str) -> str:
        request_id = new_realtime_message_id()
        self._pending_scrollback_requests[request_id] = (channel_id, origin_fingerprint)
        return request_id

    def finish_scrollback_request(self, request_id: str) -> None:
        self._pending_scrollback_requests.pop(request_id, None)
        self._remote_channel_scrollback.pop(request_id, None)

    def pop_channel_scrollback(self, channel_id: str, request_id: str) -> list[LocalChannelMessage]:
        """One-time pickup for a just-received scrollback_snapshot (issue
        #194) -- pops and clears rather than a plain get, matching design
        doc §16 Decision 2's "rendered once, never durably stored on the
        subscribing side." A caller (`netbbs.net.chat_flow._subscribe_
        live`) polls this briefly after subscribing; empty means either
        nothing has arrived yet, it was already popped, or this node
        doesn't hold a live subscription for `channel_id` at all."""
        pending = self._pending_scrollback_requests.get(request_id)
        if pending is None or pending[0] != channel_id:
            return []
        return self._remote_channel_scrollback.pop(request_id, [])

    def _handle_unsubscribe(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        channel_id = frame.payload["channel_id"]
        subscribers = self._subscribers.get(channel_id)
        if subscribers is None:
            return
        subscribers.pop(session.remote_fingerprint, None)
        if not subscribers:
            del self._subscribers[channel_id]

    async def _handle_channel_message(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        channel = await self._lane.run(
            _decide_channel_subscribe_authorization, channel_id=frame.payload["channel_id"],
            peer_fingerprint=session.remote_fingerprint,
        )
        message = LocalChannelMessage(
            id=-1, channel_id=channel.id, kind="message",
            author_label=f"{frame.payload['user_id']}@{session.remote_fingerprint}",
            author_fingerprint=None, body=frame.payload["body"], created_at=frame.payload["created_at"],
        )
        await self._hub.broadcast(channel.name, message)

    async def _handle_presence_delta(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        channel = await self._lane.run(
            _decide_channel_subscribe_authorization, channel_id=frame.payload["channel_id"],
            peer_fingerprint=session.remote_fingerprint,
        )
        kind = "join" if frame.payload["change"] == "join" else "leave"
        message = LocalChannelMessage(
            id=-1, channel_id=channel.id, kind=kind,
            author_label=f"{frame.payload['user_id']}@{session.remote_fingerprint}",
            author_fingerprint=None, body=None, created_at=utc_now_iso(),
        )
        await self._hub.broadcast(channel.name, message)

        # Issue #195: keep the merged live roster (populated by the
        # initial presence_snapshot below) in sync with subsequent
        # deltas too -- otherwise it would go stale the instant anyone
        # joined or left after the snapshot was taken. Bounded the same
        # way the snapshot itself already is; mirrors
        # `_handle_node_presence_delta`'s identical shape.
        online = self._remote_channel_presence.setdefault(channel.channel_id, {})
        self._remote_channel_presence_source[channel.channel_id] = session.remote_fingerprint
        user_id = frame.payload["user_id"]
        if kind == "join":
            if user_id not in online and len(online) >= _MAX_PRESENCE_SNAPSHOT_ENTRIES:
                pass  # bounded -- silently dropped, not a protocol violation to strike over
            else:
                online[user_id] = frame.payload["display_label"]
        else:
            online.pop(user_id, None)

    async def _handle_presence_snapshot(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        channel = await self._lane.run(
            _decide_channel_subscribe_authorization, channel_id=frame.payload["channel_id"],
            peer_fingerprint=session.remote_fingerprint,
        )
        # Issue #195: merged live roster -- annotates a channel's own
        # /who list with connected peers, not just live join/leave
        # notices. Replaces any prior snapshot for this channel outright
        # (a fresh subscribe supersedes whatever was tracked before).
        entries = frame.payload["entries"][:_MAX_PRESENCE_SNAPSHOT_ENTRIES]
        self._remote_channel_presence[channel.channel_id] = {
            entry["user_id"]: entry["display_label"] for entry in entries
        }
        self._remote_channel_presence_source[channel.channel_id] = session.remote_fingerprint

    async def _node_realtime_allowed(self, fingerprint: str) -> bool:
        """Design doc §8.10.2's "checked again at message delivery"
        principle, extended to node-wide presence: a session can outlive
        the peer's trust degrading below `ESTABLISHED` (it isn't force-
        closed the instant that happens), so an incoming/outgoing
        node-presence frame re-checks fresh rather than trusting that
        the session's continued existence still implies continued
        trust."""
        return await self._lane.run(
            lambda db: decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed
        )

    async def _handle_node_presence_snapshot(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        if not await self._node_realtime_allowed(session.remote_fingerprint):
            return
        # Issue #168: a session that arrives for a direct message or a
        # relay anchor never subscribes to a channel, so this is the first
        # (and only) place the receiving side learns it exists. Tracking
        # here is what spawns the close-watcher that clears this presence
        # again, and what sends *our* snapshot back so the peer's own
        # "is that user online there" check has an answer. Idempotent.
        await self.track_session(session)
        entries = frame.payload["entries"][:_MAX_PRESENCE_SNAPSHOT_ENTRIES]
        self._remote_node_presence[session.remote_fingerprint] = {
            entry["user_id"]: entry["display_label"] for entry in entries
        }

    async def _handle_node_presence_delta(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        if not await self._node_realtime_allowed(session.remote_fingerprint):
            return
        await self.track_session(session)
        online = self._remote_node_presence.setdefault(session.remote_fingerprint, {})
        user_id = frame.payload["user_id"]
        if frame.payload["change"] == "join":
            if user_id not in online and len(online) >= _MAX_PRESENCE_SNAPSHOT_ENTRIES:
                return  # bounded -- silently dropped, not a protocol violation to strike over
            online[user_id] = frame.payload["display_label"]
        else:
            online.pop(user_id, None)

    def remote_node_presence(self) -> dict[str, dict[str, str]]:
        """`{peer_fingerprint: {user_id: display_label}}` -- the
        last-known node-wide presence for every peer this node currently
        holds a live session with. A UI caller (Who's Online, issue
        #164) reads this directly; nothing here renders it."""
        return {fingerprint: dict(entries) for fingerprint, entries in self._remote_node_presence.items()}

    def remote_channel_presence(self, channel_id: str) -> dict[str, str]:
        """`{user_id: display_label}` -- the last-known remote roster for
        `channel_id`, if this node currently subscribes to it live and
        has received at least one snapshot or delta. Empty if not
        subscribed, or if the channel isn't linked at all. A UI caller
        (`/who`, issue #195) reads this directly to annotate the local
        roster; nothing here renders it."""
        return dict(self._remote_channel_presence.get(channel_id, {}))

    def remote_channel_origin_fingerprint(self, channel_id: str) -> str | None:
        """Which peer fingerprint populated `remote_channel_presence`'s
        current entries for `channel_id`, if any -- lets a caller
        annotate a remote roster entry with which node it came from
        (issue #195), the same way Who's Online already annotates
        node-wide remote entries (`netbbs.net.login_flow.
        _who_entry_description`). `None` if not currently subscribed."""
        return self._remote_channel_presence_source.get(channel_id)

    async def _live_subscribers(self, channel: Channel) -> list[LinkRealtimeSession]:
        """Currently-registered subscriber sessions for `channel`,
        filtered by a fresh trust re-check (design doc §8.10.2:
        "Authorization is checked ... again at message delivery" --
        a subscription from before a peer was quarantined must stop
        receiving pushes, not just future subscribe attempts). A peer
        that no longer passes is dropped from the subscriber set
        outright, not merely skipped this once."""
        subscribers = self._subscribers.get(channel.channel_id)
        if not subscribers:
            return []
        live: list[LinkRealtimeSession] = []
        for fingerprint, session in list(subscribers.items()):
            allowed = await self._lane.run(
                lambda db: decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed
            )
            if allowed:
                live.append(session)
            else:
                subscribers.pop(fingerprint, None)
        if not subscribers:
            del self._subscribers[channel.channel_id]
        return live

    async def broadcast_local_message_live(self, channel: Channel, message: LocalChannelMessage) -> None:
        """Push a just-locally-authored channel message out to every
        currently live-subscribed peer session for `channel` -- the
        outbound half of `_handle_channel_message`. One slow/dead peer
        session degrades to just that session closing (`LinkRealtime
        Session.send` already handles a full queue) and must never block
        delivery to anyone else or to the local caller who just sent
        the message."""
        sessions = await self._live_subscribers(channel)
        if not sessions:
            return
        frame = build_channel_message_frame(
            channel.channel_id, message.author_label, message.author_label,
            message.body or "", message.created_at,
        )
        for session in sessions:
            try:
                await session.send(frame)
            except LinkTransportError:
                pass

    async def broadcast_local_presence_live(self, channel: Channel, *, change: str, username: str) -> None:
        sessions = await self._live_subscribers(channel)
        if not sessions:
            return
        frame = build_presence_delta_frame(channel.channel_id, change, username, username)
        for session in sessions:
            try:
                await session.send(frame)
            except LinkTransportError:
                pass

    async def _live_node_wide_sessions(self) -> list[LinkRealtimeSession]:
        """Every currently-connected session whose peer still passes a
        fresh `REALTIME` trust check -- the node-wide counterpart to
        `_live_subscribers`, which is inherently channel-scoped and so
        has no `_subscribers` entry for node-wide traffic to re-validate
        against. `registry` (not `_subscribers`) is the source of truth
        for "every peer this node currently holds a live session with";
        a peer that no longer passes is simply skipped for this
        broadcast, not force-disconnected -- the same session may still
        be legitimately open for other reasons the caller doesn't get
        to unilaterally tear down from here."""
        sessions = self._registry.all_sessions()
        if not sessions:
            return []
        live: list[LinkRealtimeSession] = []
        for session in sessions:
            if await self._node_realtime_allowed(session.remote_fingerprint):
                live.append(session)
        return live

    async def broadcast_node_presence_live(self, *, change: str, username: str) -> None:
        """Push a local login/logout out to every currently-connected
        peer, node-wide -- the outbound half of
        `_handle_node_presence_delta`. Call this from the same place
        `netbbs.chat.presence.PresenceRegistry.enter`/`leave` already
        fires (issue #164), not from a channel join/leave -- node-wide
        presence answers "who's online on this node, period," the same
        question local Who's Online already answers, not "who's watching
        this channel."""
        sessions = await self._live_node_wide_sessions()
        if not sessions:
            return
        frame = build_node_presence_delta_frame(change, username, username)
        for session in sessions:
            try:
                await session.send(frame)
            except LinkTransportError:
                pass


async def ensure_live_subscription(
    *,
    channel: Channel,
    node_identity: NodeIdentity,
    link_node: LinkNode,
    lane: DatabaseLane,
    registry: LinkRealtimeSessionRegistry,
    bridge: LiveChannelBridge,
    dial_timeout_seconds: float = 10.0,
) -> tuple[LinkRealtimeSession, str] | None:
    """
    If `channel` is Linked, ensure this node holds (or can establish) a
    live session to its origin node and has sent it a request-correlated
    `subscribe` --
    best-effort except for an authenticated protocol-version mismatch,
    which raises `RealtimeProtocolVersionError` so callers can distinguish
    an upgrade requirement from transient unavailability. A caller who can't get live delivery
    still has the existing async catch-up path (design doc §8.10.2:
    "the caller sees that live traffic may have been missed"), so
    degrading silently to `None` is the correct signal here, not a
    caller-visible error.

    Reuses an already-live session to the origin from `registry` if one
    exists (this node may already be connected to it for another
    channel); otherwise dials the origin's advertised real-time
    address(es) in order, first success wins.

    Takes `lane`, never a raw `Database` -- an interactive caller (a
    channel-join flow) must never touch a `sqlite3.Connection` directly
    off the event loop (see `netbbs.storage.execution.DatabaseLane`'s
    own docstring); the one synchronous read this needs (`channel_
    origin_fingerprint`) is dispatched through it like every other
    business-logic call from `netbbs.net`.
    """
    origin_fingerprint = await lane.run(channel_origin_fingerprint, channel)
    if origin_fingerprint is None or origin_fingerprint == node_identity.fingerprint:
        return None  # not linked, or this node is the origin -- nothing to dial
    session = registry.get(origin_fingerprint)
    if session is None:
        for host, port in dialable_realtime_addresses_for_peer(link_node, origin_fingerprint):
            try:
                session = await asyncio.wait_for(
                    dial_realtime_session(
                        host, port, node_identity, on_frame=bridge.on_frame, registry=registry,
                        lane=lane, enforce_trust_policy=True, expected_fingerprint=origin_fingerprint,
                    ),
                    timeout=dial_timeout_seconds,
                )
            except RealtimeProtocolVersionError:
                raise
            except Exception:
                continue
            await bridge.track_session(session)
            break
        else:
            return None
    request_id = bridge.begin_scrollback_request(channel.channel_id, origin_fingerprint)
    try:
        await session.send(build_subscribe_frame(channel.channel_id, message_id=request_id))
    except LinkTransportError:
        bridge.finish_scrollback_request(request_id)
        return None
    return session, request_id
