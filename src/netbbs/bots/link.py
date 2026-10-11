"""Optional live Link bot requests and channel-scoped bot presence.

Chat messages (including signed catch-up) never dispatch a request. Only the
accepted local human input path creates bot_request, directly to the bot's
authenticated home node. Channel output still uses ordinary signed chat.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging

from netbbs.bots.registry import BotError, active_labels, allowed_channel, bot_for_label, list_bots, channel_readers
from netbbs.chat.bots import remote_bot_visible
from netbbs.link.channels import channel_origin_fingerprint, is_channel_linked
from netbbs.link.events import CHAT_BOTS_CAPABILITY, CHAT_BOT_READERS_CAPABILITY, CHAT_BOT_CALLERS_CAPABILITY, descriptor_has_capability
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action
from netbbs.link.node_profiles import identity_for_fingerprint, link_address_label
from netbbs.link.protocol import (
    LinkProtocolError, build_bot_frame, build_subscribe_frame, build_unsubscribe_frame,
    build_channel_message_frame,
)
from netbbs.link.realtime_channels import _decide_channel_subscribe_authorization, _live_author_allowed
from netbbs.link.transport import LinkTransportError
from netbbs.rendering import sanitize_text

_logger = logging.getLogger(__name__)
MAX_REQUESTS = 256
MAX_REMOTE_ROSTERS = 512


def _roster_author_allowed(db, author, label, source, *, readers):
    if readers:
        # A bot's user-level output filter cannot revoke its node's reading
        # access. The transport source is already authorized; relayed node
        # identities still need admission before they consume roster space.
        return author == source or decide_node_action(db, author, LinkPolicyAction.EVENTS).allowed
    return _live_author_allowed(db, author, label, relayed=author != source)


class BotLink:
    def __init__(self, runtime, node, bridge, direct, registry):
        self.runtime, self.node, self.bridge = runtime, node, bridge
        self.direct, self.registry = direct, registry
        self.lane, self.hub = runtime.lane, runtime.hub
        self.task = None
        self.channels = {}  # channel_id -> owned subscription task
        self.requests = set()
        self.pending = {}  # request_id -> (expected fingerprint, Future)
        self.remote = {}  # (channel_id, author fingerprint) -> (source session, labels)
        self.remote_readers = {}
        self.seen = OrderedDict()
        runtime.link = self
        bridge.bot_link = self
        bridge.register_frame_handler(self.owns_frame, self.handle_frame)

    def capable(self, fingerprint, capability=CHAT_BOTS_CAPABILITY):
        peer = self.node.peers.get(fingerprint)
        return peer is not None and peer.descriptor is not None and descriptor_has_capability(
            peer.descriptor, capability
        )

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="bot-link")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        for task in list(self.requests):
            task.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)
        self.requests.clear()
        self.pending.clear()
        self.remote.clear()
        self.remote_readers.clear()

    @staticmethod
    def owns_frame(session, frame):
        return frame.type in {"bot_request", "bot_result", "bot_presence", "bot_readers"}

    async def _send(self, session, frame):
        if frame.type == "bot_readers" and not self.capable(session.remote_fingerprint, CHAT_BOT_READERS_CAPABILITY):
            return
        if session.closed.is_set() or not self.capable(session.remote_fingerprint):
            return
        if not await self.lane.run(lambda db: decide_node_action(db, session.remote_fingerprint, LinkPolicyAction.REALTIME).allowed):
            return
        try:
            await session.send(frame)
        except LinkTransportError:
            pass

    def request(self, channel, user, participant_id, fingerprint, label, text):
        """Bounded background delivery; dialing never holds chat's input lock."""
        if not self.capable(fingerprint):
            return "That node does not advertise live bot support."
        if len(self.requests) >= MAX_REQUESTS:
            return "Bot requests are busy; try again shortly."
        try:
            frame = build_bot_frame("bot_request", {"channel_id": channel.channel_id, "bot": label,
                                                    "caller": user.username, "text": text})
        except LinkProtocolError:
            return "That bot request is too long or has an invalid address."
        task = asyncio.create_task(self._request(channel, participant_id, fingerprint, label, frame, user_id=user.id),
                                   name="remote-bot-request")
        self.requests.add(task)
        task.add_done_callback(self.requests.discard)
        return None

    async def _request(self, channel, participant_id, fingerprint, label, frame, *, user_id=None):
        reason = ""
        try:
            async with asyncio.timeout(75):
                if not await self.lane.run(is_channel_linked, channel):
                    raise BotError("Remote bots need a Linked channel carried by both nodes.")
                if not await self.lane.run(remote_bot_visible, channel, label, fingerprint):
                    raise BotError("This bot is muted or hidden by this channel's policy.")
                session = await self.direct.ensure_session(fingerprint)
                if not self.capable(fingerprint):
                    raise BotError("That node does not advertise live bot support.")
                if user_id is not None and self.capable(fingerprint, CHAT_BOT_CALLERS_CAPABILITY):
                    from netbbs.bots.model import caller_access
                    access = await self.lane.run(caller_access, user_id)
                    frame = build_bot_frame("bot_request", {**frame.payload, "access": access})
                done = asyncio.get_running_loop().create_future()
                self.pending[frame.message_id] = (fingerprint, done)
                await session.send(frame)
                reason = await done
        except asyncio.CancelledError:
            reason = "Bot requests stopped before the remote bot replied."
            raise
        except BotError as exc:
            reason = str(exc)
        except TimeoutError:
            reason = "The remote bot did not reply in time; check the channel before retrying."
        except Exception:
            reason = "The remote bot could not be reached; try again when Link is available."
        finally:
            self.pending.pop(frame.message_id, None)
            if reason:
                await self.hub.send_to(channel.name, participant_id, sanitize_text(f"{label}: {reason}"))

    async def handle_frame(self, session, frame):
        if frame.type == "bot_readers" and not self.capable(session.remote_fingerprint, CHAT_BOT_READERS_CAPABILITY):
            raise LinkProtocolError("Peer has not advertised chat_bot_readers_v1")
        if not self.capable(session.remote_fingerprint):
            raise LinkProtocolError("Peer has not advertised chat_bots_v1")
        if frame.type == "bot_result":
            pending = self.pending.get(frame.payload["request_id"])
            if pending and pending[0] == session.remote_fingerprint and not pending[1].done():
                pending[1].set_result(frame.payload["error"])
        elif frame.type == "bot_request":
            if "access" in frame.payload and not self.capable(session.remote_fingerprint, CHAT_BOT_CALLERS_CAPABILITY):
                raise LinkProtocolError("Peer has not advertised chat_bot_callers_v1")
            key = (session.remote_fingerprint, frame.message_id)
            if key in self.seen:
                return
            self.seen[key] = None
            if len(self.seen) > 1024:
                self.seen.popitem(last=False)
            await self._incoming(session, frame)
        else:
            await self._presence(session, frame)

    async def _incoming(self, session, frame):
        async def completed(error, message=None):
            # The requester may reach the bot while the channel origin is
            # offline. Deliver its public reply directly too; the bridge's
            # ordinary author/time/body dedup hides any origin-relayed copy.
            if message is not None:
                await self._send(session, build_channel_message_frame(
                    frame.payload["channel_id"], message.author_label, message.author_label,
                    message.body, message.created_at,
                ))
            await self._send(session, build_bot_frame("bot_result", {
                "request_id": frame.message_id, "error": error[:120],
            }))

        def authorize(db):
            p = frame.payload
            channel = _decide_channel_subscribe_authorization(
                db, channel_id=p["channel_id"], peer_fingerprint=session.remote_fingerprint)
            caller = p["caller"]
            if ("@" in caller or caller.lower().endswith((".bot", ".door")) or
                    sanitize_text(caller) != caller or
                    not _live_author_allowed(db, session.remote_fingerprint, caller, relayed=False)):
                raise BotError("This caller cannot address a bot here.")
            bot = bot_for_label(db, p["bot"])
            if bot is None:
                raise BotError("No running bot with that name is available here.")
            allowed_channel(db, bot.id, channel.name, check_rate=True)
            home = identity_for_fingerprint(db, session.remote_fingerprint).label
            return bot, channel, {"id": f"link:{session.remote_fingerprint}:{caller}",
                                  "name": link_address_label(caller, home),
                                  "access": p.get("access")}

        try:
            bot, channel, caller = await self.lane.run(authorize)
            await self.runtime.enqueue(bot, channel, caller, frame.payload["text"], completed=completed)
        except (BotError, LinkProtocolError):
            await completed("This bot is unavailable here, busy, or not permitted to respond.")

    def _desired(self, db):
        result = {}
        for bot in list_bots(db):
            if bot.id not in self.runtime.running_ids:
                continue
            for name in bot.linked_channels:
                try:
                    _, channel = allowed_channel(db, bot.id, name)
                except BotError:
                    continue
                if is_channel_linked(db, channel):
                    result[channel.channel_id] = channel
        return result

    async def _run(self):
        try:
            while True:
                try:
                    desired = await self.lane.run(self._desired)
                    for cid, task in list(self.channels.items()):
                        if cid not in desired or task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            del self.channels[cid]
                    for cid, channel in desired.items():
                        if cid not in self.channels:
                            self.channels[cid] = asyncio.create_task(self._channel(channel), name="bot-link-channel")
                except Exception:
                    _logger.exception("Could not reconcile Linked bots; retrying")
                await asyncio.sleep(self.runtime.poll_seconds)
        finally:
            for task in self.channels.values():
                task.cancel()
            await asyncio.gather(*self.channels.values(), return_exceptions=True)
            self.channels.clear()

    async def _channel(self, channel):
        upstream = None
        advertised = None
        advertised_readers = None
        cid = channel.channel_id
        self.bridge.register_local_interest(cid, id(self))
        try:
            while True:
                try:
                    origin = await self.lane.run(channel_origin_fingerprint, channel)
                    if origin != self.registry.own_fingerprint:
                        if origin is None:
                            return
                        if upstream is None or upstream.closed.is_set() or upstream.remote_fingerprint != origin:
                            async with asyncio.timeout(15):
                                upstream = await self.direct.ensure_session(origin)
                            await upstream.send(build_subscribe_frame(cid))
                            advertised = None
                            advertised_readers = None
                    labels = await self.lane.run(active_labels, channel, self.runtime.running_ids)
                    if labels != advertised:
                        await self._broadcast_presence(channel, self.registry.own_fingerprint, labels)
                        advertised = labels
                    readers = await self.lane.run(channel_readers, channel)
                    if readers != advertised_readers:
                        await self._broadcast_presence(channel, self.registry.own_fingerprint, readers, readers=True)
                        advertised_readers = readers
                    await asyncio.sleep(self.runtime.poll_seconds)
                except Exception:
                    upstream = None
                    await asyncio.sleep(5)
        finally:
            await self._broadcast_presence(channel, self.registry.own_fingerprint, [])
            await self._broadcast_presence(channel, self.registry.own_fingerprint, [], readers=True)
            if self.bridge.release_local_interest(cid, id(self)) and upstream is not None:
                try:
                    await upstream.send(build_unsubscribe_frame(cid))
                except LinkTransportError:
                    pass

    async def on_subscribe(self, session, channel):
        if not self.capable(session.remote_fingerprint):
            return
        labels = await self.lane.run(active_labels, channel, self.runtime.running_ids)
        await self._send_presence(session, channel.channel_id, self.registry.own_fingerprint, labels)
        readers = await self.lane.run(channel_readers, channel)
        await self._send_presence(session, channel.channel_id, self.registry.own_fingerprint, readers, readers=True)
        if await self.lane.run(channel_origin_fingerprint, channel) == self.registry.own_fingerprint:
            for (cid, author), (_, labels) in list(self.remote.items()):
                if cid == channel.channel_id and author != session.remote_fingerprint:
                    await self._send_presence(session, cid, author, labels)
            for (cid, author), (_, labels) in list(self.remote_readers.items()):
                if cid == channel.channel_id and author != session.remote_fingerprint:
                    await self._send_presence(session, cid, author, labels, readers=True)

    async def _send_presence(self, session, cid, author, labels, *, readers=False):
        await self._send(session, build_bot_frame("bot_readers" if readers else "bot_presence", {
            "channel_id": cid, "author_node_fingerprint": author, "labels": labels,
        }))

    async def _broadcast_presence(self, channel, author, labels, *, readers=False):
        sessions = await self.bridge._live_subscribers(channel)
        origin = await self.lane.run(channel_origin_fingerprint, channel)
        if origin != self.registry.own_fingerprint:
            upstream = self.registry.get(origin) if origin else None
            if upstream is not None:
                sessions.append(upstream)
        for session in sessions:
            if session.remote_fingerprint != author:
                await self._send_presence(session, channel.channel_id, author, labels, readers=readers)

    async def _presence(self, session, frame):
        p = frame.payload
        author = p["author_node_fingerprint"]
        if author == self.registry.own_fingerprint:
            return

        def authorize(db):
            channel = _decide_channel_subscribe_authorization(
                db, channel_id=p["channel_id"], peer_fingerprint=session.remote_fingerprint)
            origin = channel_origin_fingerprint(db, channel)
            if author != session.remote_fingerprint and origin != session.remote_fingerprint:
                raise LinkProtocolError("Only the channel origin can relay bot presence")
            # As with live channel lines, reject fabricated/blocked authors
            # before they consume retained state or are relayed onward.
            labels = [label for label in dict.fromkeys(p["labels"])
                      if _roster_author_allowed(db, author, label, session.remote_fingerprint,
                                                readers=frame.type == "bot_readers")]
            return channel, origin, labels

        channel, origin, labels = await self.lane.run(authorize)
        key = (channel.channel_id, author)
        readers = frame.type == "bot_readers"
        rosters = self.remote_readers if readers else self.remote
        previous = rosters.get(key, (None, []))[1]
        if labels:
            if key not in rosters and len(rosters) >= MAX_REMOTE_ROSTERS:
                return  # Local capacity is not a protocol fault by this peer.
            rosters[key] = (session, labels)
        else:
            rosters.pop(key, None)
        if readers and labels != previous:
            node = (await self.lane.run(identity_for_fingerprint, author)).label
            names = ", ".join(link_address_label(label, node) for label in labels)
            await self.hub.broadcast(channel.name, "[BOT] " + (
                names + " reads this channel." if labels else
                f"Whole-channel bot reading at {sanitize_text(node)} was withdrawn."
            ))
        if origin == self.registry.own_fingerprint:
            await self._broadcast_presence(channel, author, labels, readers=readers)

    async def on_disconnect(self, session):
        from netbbs.link.channels import get_channel_by_channel_id
        for readers, rosters in ((False, self.remote), (True, self.remote_readers)):
            for (cid, author), (source, _) in list(rosters.items()):
                if source is session:
                    rosters.pop((cid, author), None)
                    channel = await self.lane.run(get_channel_by_channel_id, cid)
                    if channel and await self.lane.run(channel_origin_fingerprint, channel) == self.registry.own_fingerprint:
                        await self._broadcast_presence(channel, author, [], readers=readers)

    def visible_roster(self, db, channel, *, readers=False):
        result = []
        rosters = self.remote_readers if readers else self.remote
        for (cid, author), (source, labels) in list(rosters.items()):
            if cid != channel.channel_id or source.closed.is_set():
                continue
            try:
                _decide_channel_subscribe_authorization(db, channel_id=cid, peer_fingerprint=source.remote_fingerprint)
            except LinkProtocolError:
                continue
            for label in labels:
                if ((readers or remote_bot_visible(db, channel, label, author)) and
                        _roster_author_allowed(db, author, label, source.remote_fingerprint, readers=readers)):
                    result.append((label, author))
        return result
