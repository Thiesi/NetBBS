"""
End-to-end cross-subsystem regression tests for NetBBS Link (issue #80).

Every other Link test file proves one subsystem boundary at a time:
`test_link_protocol.py`/`test_link_convergence.py` prove verification/
gossip logic against the deterministic `ScriptedTransport` fake;
`test_link_sync.py`/`test_link_transport.py` prove the background loop
and HTTP adapter reach a real peer over a real socket; `test_link_boards.py`
proves carried-content materialization interacts correctly with
`netbbs.activity`'s unread state. Issue #69 was a bug that survived all
of that: `compose_link_message` never registered a composed message
into the sender's own `LinkNode.events`, so its acknowledgement was
unconditionally rejected -- caught only because one single test
(`test_link_sync.py`'s own acknowledgement round-trip test) happened to
exercise the complete sender -> receiver -> acknowledgement ->
sender-delivery-state chain over real transport with real
`compose_link_message`, not a hand-built event.

This file is the deliberate, named home for that class of test: a
complete real-transport, real-SQLite, real-domain-read-path vertical
slice per currently implemented Link product surface (linked boards,
Link mail), each asserting genuinely final user-visible state on both
sides (an ordinary inbox/board read, not just `known_event_ids`), each
covering restart-mid-scenario and duplicate-delivery for the same
reason issue #69 asks for it: a subsystem boundary that looks correct
in isolation can still drop a caller-visible guarantee at the seam
between two subsystems. Future Link vertical slices should extend this
file (design doc §14.1, issue #80's own acceptance criteria) rather
than being considered complete without an equivalent scenario here.

Helper functions (`_NodeDb`, `_hello_for`, `_run_server`,
`_run_sync_briefly`) are the same conventions `test_link_sync.py`/
`test_link_transport.py` already established -- duplicated here rather
than imported, matching this codebase's existing per-file convention
for these small fixtures (see e.g. those two files, which already
duplicate the identical helpers between themselves).
"""

from __future__ import annotations

import asyncio
import os

import aiohttp

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board, get_board_by_name
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.files.areas import create_file_area, get_file_area_by_name
from netbbs.files.entries import download_file, get_file, upload_file
from netbbs.link.boards import LinkContext, link_board, queue_board_post_if_linked
from netbbs.link.channels import link_channel, queue_channel_message_if_linked
from netbbs.link.file_transfer import apply_received_chunk, compute_transfer_id, get_transfer
from netbbs.link.files import get_remote_file, link_file_area, list_remote_files, queue_file_descriptor_if_linked
from netbbs.link.mail import compose_link_message, expire_link_message_delivery, unexpire_link_message_delivery
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import FileChunkRequest, LinkNode
from netbbs.link.store import load_link_node
from netbbs.link.sync import run_link_sync
from netbbs.link.transport import LinkServer, fetch_next_file_chunk, request_file_chunk
from netbbs.net import chat_flow, file_flow
from netbbs.net.char_input import InputHistory
from tests.test_chat_flow_moderation import FakeSession
from tests.test_chat_flow_picker_authorization import FakeSession as PickerFakeSession
from netbbs.link.work_items import (
    KIND_LINK_MAIL_DELIVERY,
    list_work_items,
    record_failure,
    replay_work_item,
)
from netbbs.mail import list_inbox, list_sent
from netbbs.search import search_channel_messages, search_posts
from netbbs.activity import board_read_cursor, record_board_seen, record_channel_seen, unread_channel_count, unread_post_count
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _hello_for(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00"):
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


def _dialable_hello(node: LinkNode, server: LinkServer, *, created_at: str = "2026-01-01T00:00:00+00:00"):
    return node.build_hello(
        addresses=[{"protocol": "http", "address": "127.0.0.1", "port": server.port}],
        outgoing_only=False, created_at=created_at,
    )


async def _run_server(node: LinkNode, lane: DatabaseLane) -> LinkServer:
    server = LinkServer(host="127.0.0.1", port=0, node=node, own_hello_provider=lambda: _hello_for(node), lane=lane)
    await server.start()
    return server


async def _run_sync_briefly(coro_task: asyncio.Task, *, settle: float = 0.2) -> None:
    await asyncio.sleep(settle)
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass


class _NodeDb:
    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


async def _one_pass(node, session, seeds, hello, lane, *, settle: float = 0.2, interval: float = 60.0) -> None:
    task = asyncio.create_task(run_link_sync(node, session, seeds, hello, lane, interval_seconds=interval))
    await _run_sync_briefly(task, settle=settle)


# -- Link mail: full vertical, compose through ordinary inbox to delivered status --


def test_link_mail_full_vertical_from_compose_to_ordinary_inbox_and_delivered_status(tmp_path):
    """The complete chain issue #69 fell through a gap in: alice composes
    on node A; a real sync pass pushes it over a real socket; bob's real
    inbox (`netbbs.mail.list_inbox`, the same function the mail UI
    calls) shows it; bob's own sync pass pushes his queued
    acknowledgement back; alice's own sync pass receives and accepts it.
    Asserts real user-visible state on *both* sides, not just
    `known_event_ids`/HTTP success -- the exact gap issue #80 names:
    existing tests proved delivery to the recipient, or proved the
    acknowledgement accepted, but never both together with the
    recipient's own ordinary inbox checked in the same scenario."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    bob = create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def scenario():
        # Both sides dialable -- the ack push never falls back to a
        # relay (netbbs.link.sync._push_pending_link_mail's own
        # docstring), so alice's own node must be reachable too. Each
        # hello provider closes over its own server variable, assigned
        # right after (late-binding closure -- the same self-referential
        # shape test_link_sync.py's own ack round-trip test already
        # uses, since the server object doesn't exist yet when the
        # lambda is defined but does by the time it's ever called).
        dialer_hello = lambda: _dialable_hello(dialer_node, dialer_server)  # noqa: E731
        recipient_hello = lambda: _dialable_hello(recipient_node, recipient_server)  # noqa: E731
        dialer_server = LinkServer(
            host="127.0.0.1", port=0, node=dialer_node, own_hello_provider=dialer_hello, lane=dialer.lane
        )
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node, own_hello_provider=recipient_hello, lane=recipient.lane
        )
        await dialer_server.start()
        await recipient_server.start()
        recipient_seed = f"http://127.0.0.1:{recipient_server.port}"
        dialer_seed = f"http://127.0.0.1:{dialer_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                # Pass 1: mutual hello so both sides know each other's
                # dialable address and signing key.
                await _one_pass(dialer_node, session, [recipient_seed], dialer_hello, dialer.lane)

                message = compose_link_message(
                    dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
                    node_identity=dialer_identity,
                )

                # Pass 2: dialer pushes the composed message.
                await _one_pass(dialer_node, session, [recipient_seed], dialer_hello, dialer.lane)
                # Pass 3: recipient pushes its queued acknowledgement back.
                await _one_pass(recipient_node, session, [dialer_seed], recipient_hello, recipient.lane)
        finally:
            await dialer_server.stop()
            await recipient_server.stop()
        return message

    try:
        message = asyncio.run(scenario())

        # Recipient's real, ordinary inbox -- the same read path the
        # mail UI itself calls -- shows the delivered message.
        bob_inbox = list_inbox(recipient.db, bob)
        assert [m.subject for m in bob_inbox] == ["hello"]
        assert bob_inbox[0].body == "world"
        assert bob_inbox[0].sender_label == f"alice@{dialer_identity.fingerprint}"

        # Sender's own sent folder still shows it too.
        alice_sent = list_sent(dialer.db, alice)
        assert [m.subject for m in alice_sent] == ["hello"]

        # The acknowledgement resolved the delivery state back on the
        # sender -- the one piece of state issue #69 broke, and the one
        # piece with no dedicated UI surface yet (design doc §10.3), so
        # this is still the most direct available check for it.
        row = dialer.db.connection.execute(
            "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
            (message.content_id,),
        ).fetchone()
        assert row["link_delivery_status"] == "delivered"
    finally:
        dialer.close()
        recipient.close()


def test_link_mail_duplicate_delivery_over_real_transport_is_idempotent(tmp_path):
    """The same signed link_message pushed twice over two real sync
    passes (a retried delivery after a dropped response, or a resent
    work item) must not create a second inbox row or otherwise double
    the recipient's visible mail -- known_event_ids dedup must actually
    hold over the real transport path, not just the in-process
    handle_events path test_link_convergence.py already proves it for."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    bob = create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def scenario():
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node,
            own_hello_provider=lambda: recipient_node.build_hello(
                addresses=[{"protocol": "http", "address": "127.0.0.1", "port": recipient_server.port}],
                outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
            ),
            lane=recipient.lane,
        )
        await recipient_server.start()
        seed_url = f"http://127.0.0.1:{recipient_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)

                compose_link_message(
                    dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
                    node_identity=dialer_identity,
                )

                # Two consecutive passes push the same still-pending
                # work item twice -- the work item only resolves once
                # record_success is recorded on the *first* one, but
                # both dispatch the identical signed event to the wire.
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
        finally:
            await recipient_server.stop()

    try:
        asyncio.run(scenario())
        bob_inbox = list_inbox(recipient.db, bob)
        assert len(bob_inbox) == 1
        assert bob_inbox[0].subject == "hello"
    finally:
        dialer.close()
        recipient.close()


def test_link_mail_delivery_survives_recipient_restart_using_only_persisted_state(tmp_path):
    """The recipient node process is stopped and a fresh `LinkNode` is
    reconstructed purely from disk (`netbbs.link.store.load_link_node`)
    between receiving the message and pushing its acknowledgement --
    proving the acknowledgement path works from genuinely reloaded
    state, not from the original in-memory LinkNode object happening to
    still be alive across the two stages (issue #80's own "restart-
    between-stages" acceptance criterion, for the mail vertical
    specifically -- test_link_transport.py's existing restart test only
    covers hello/peer persistence in general)."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    bob = create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def _deliver_stage():
        dialer_server = LinkServer(
            host="127.0.0.1", port=0, node=dialer_node,
            own_hello_provider=lambda: _dialable_hello(dialer_node, dialer_server), lane=dialer.lane,
        )
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node,
            own_hello_provider=lambda: _dialable_hello(recipient_node, recipient_server), lane=recipient.lane,
        )
        await dialer_server.start()
        await recipient_server.start()
        dialer_hello = lambda: _dialable_hello(dialer_node, dialer_server)  # noqa: E731
        recipient_seed = f"http://127.0.0.1:{recipient_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(dialer_node, session, [recipient_seed], dialer_hello, dialer.lane)
                message = compose_link_message(
                    dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
                    node_identity=dialer_identity,
                )
                await _one_pass(dialer_node, session, [recipient_seed], dialer_hello, dialer.lane)
        finally:
            await dialer_server.stop()
            await recipient_server.stop()
        return message, dialer_server.port

    async def _acknowledge_stage(restarted_recipient_node, dialer_port):
        dialer_server = LinkServer(
            host="127.0.0.1", port=dialer_port, node=dialer_node,
            own_hello_provider=lambda: _dialable_hello(dialer_node, dialer_server), lane=dialer.lane,
        )
        await dialer_server.start()
        dialer_seed = f"http://127.0.0.1:{dialer_port}"
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    restarted_recipient_node, session, [dialer_seed],
                    lambda: _hello_for(restarted_recipient_node), recipient.lane,
                )
        finally:
            await dialer_server.stop()

    try:
        message, dialer_port = asyncio.run(_deliver_stage())

        # The recipient "process" ends here: reconstruct entirely from
        # what was actually persisted to disk, discarding the original
        # in-memory recipient_node.
        restarted_recipient_node = load_link_node(recipient.db, recipient_identity)
        assert restarted_recipient_node is not recipient_node
        assert message.content_id in restarted_recipient_node.known_event_ids

        asyncio.run(_acknowledge_stage(restarted_recipient_node, dialer_port))

        bob_inbox = list_inbox(recipient.db, bob)
        assert [m.subject for m in bob_inbox] == ["hello"]
        row = dialer.db.connection.execute(
            "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
            (message.content_id,),
        ).fetchone()
        assert row["link_delivery_status"] == "delivered"
    finally:
        dialer.close()
        recipient.close()


def test_link_mail_dead_lettered_delivery_recovers_via_replay_and_real_transport(tmp_path):
    """Closes the loop `test_link_work_items.py` and `test_admin_flow.py`
    each leave open on their own (see issue #80's own survey): a real
    work item tied to a real `mail_messages` row is driven to
    `dead_lettered` (repeated real `record_failure` calls, the same
    mechanism a genuinely unreachable recipient would trigger), replayed
    (`netbbs.link.work_items.replay_work_item`, the same function the
    SysOp Outbox screen calls), and then an actual real sync pass -- the
    recipient now reachable -- delivers it, ending in the recipient's
    real inbox and the sender's real delivery-status column. Neither
    existing test drives a real message through a real dead-letter ->
    replay -> real-redelivery cycle end to end."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    bob = create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def _learn_recipient_and_compose():
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node,
            own_hello_provider=lambda: _dialable_hello(recipient_node, recipient_server), lane=recipient.lane,
        )
        await recipient_server.start()
        seed_url = f"http://127.0.0.1:{recipient_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
        finally:
            await recipient_server.stop()
        return compose_link_message(
            dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
            node_identity=dialer_identity,
        )

    async def _redeliver(seed_url):
        async with aiohttp.ClientSession() as session:
            await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)

    try:
        message = asyncio.run(_learn_recipient_and_compose())

        work_items = list_work_items(dialer.db, kind=KIND_LINK_MAIL_DELIVERY)
        assert len(work_items) == 1
        work_item = work_items[0]
        assert work_item.reference_id == message.content_id

        # Drive it to dead_lettered the same way genuinely repeated
        # delivery failures against an unreachable recipient would --
        # netbbs.link.sync._push_pending_link_mail calls record_failure
        # on every failed attempt and expires the message once the work
        # item itself reports dead_lettered.
        for _ in range(20):
            work_item = record_failure(dialer.db, work_item, error="simulated unreachable recipient")
            if work_item.status == "dead_lettered":
                break
        assert work_item.status == "dead_lettered"
        expire_link_message_delivery(dialer.db, message.content_id)
        expired_row = dialer.db.connection.execute(
            "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
            (message.content_id,),
        ).fetchone()
        assert expired_row["link_delivery_status"] == "expired"

        # replay_work_item only resets the work item itself -- undoing
        # the mail_messages 'expired' side effect back to 'pending' is
        # the *caller's* job (see that function's own docstring), the
        # same pairing netbbs.net.admin_flow's real Outbox replay
        # handler performs (replay_work_item then unexpire_link_message_
        # delivery, both against the same reference_id).
        replayed = replay_work_item(dialer.db, work_item.id, replayed_by=alice)
        unexpire_link_message_delivery(dialer.db, replayed.reference_id)
        replayed = list_work_items(dialer.db, kind=KIND_LINK_MAIL_DELIVERY)[0]
        assert replayed.status == "pending"
        pending_row = dialer.db.connection.execute(
            "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
            (message.content_id,),
        ).fetchone()
        assert pending_row["link_delivery_status"] == "pending"

        async def _restart_recipient_and_redeliver():
            recipient_server = LinkServer(
                host="127.0.0.1", port=0, node=recipient_node,
                own_hello_provider=lambda: _dialable_hello(recipient_node, recipient_server), lane=recipient.lane,
            )
            await recipient_server.start()
            try:
                await _redeliver(f"http://127.0.0.1:{recipient_server.port}")
            finally:
                await recipient_server.stop()

        asyncio.run(_restart_recipient_and_redeliver())

        bob_inbox = list_inbox(recipient.db, bob)
        assert [m.subject for m in bob_inbox] == ["hello"]
        final_row = dialer.db.connection.execute(
            "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
            (message.content_id,),
        ).fetchone()
        assert final_row["link_delivery_status"] == "pending"  # awaiting the ack, but genuinely re-delivered
    finally:
        dialer.close()
        recipient.close()


# -- Linked boards: full vertical, real materialization through ordinary reads --


def test_linked_board_post_full_vertical_materializes_and_is_visible_via_ordinary_read_paths(tmp_path):
    """A board_post reaches a real peer over a real socket, materializes
    into a real `posts` row (issue #73), and is then genuinely visible
    through every ordinary user-facing read path on the carrying node:
    `list_posts_page` (browsing), `netbbs.search.search_posts` ([F]ind),
    and `netbbs.activity.unread_post_count` (New Scan) -- not just a raw
    SQL row check, which is as far as `test_link_sync.py`'s own
    materialization test goes today (see issue #80's own survey)."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello world", "first post")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane,
                )
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())

        bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
        carried_board = get_board_by_name(seed.db, "general")

        # Ordinary browsing.
        page = list_posts_page(seed.db, carried_board, bob)
        assert [p.subject for p in page.posts] == ["hello world"]
        assert page.posts[0].author_label == f"alice@{dialer_identity.fingerprint}"

        # [F]ind.
        hits = search_posts(seed.db, bob, "hello")
        assert [h.subject for h in hits] == ["hello world"]

        # New Scan: never visited yet, so unread is None (distinct from
        # 0), then becomes 0 once "seen", matching the exact model
        # test_activity.py already proves for locally-created content.
        assert unread_post_count(seed.db, bob, carried_board) is None
        record_board_seen(seed.db, bob, carried_board, page.posts[0])
        assert unread_post_count(seed.db, bob, carried_board) == 0
        assert board_read_cursor(seed.db, bob, carried_board) == (
            page.posts[0].created_at, page.posts[0].post_id,
        )
    finally:
        dialer.close()
        seed.close()


def test_linked_board_duplicate_post_delivery_over_real_transport_is_idempotent(tmp_path):
    """The identical board_post pushed across two real sync passes must
    not create a second `posts` row or double-count as unread -- the
    real-transport counterpart to `test_link_convergence.py`'s in-process
    duplicate-delivery proof."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello world", "first post")
    queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                seed_url = f"http://127.0.0.1:{seed_server.port}"
                # Two full passes push the identical accepted genesis +
                # post again -- configured-seed sync deliberately resends
                # every owned event every pass (design doc §8.6).
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
        carried_board = get_board_by_name(seed.db, "general")
        page = list_posts_page(seed.db, carried_board, bob)
        assert len(page.posts) == 1
        # Not double-counted as unread either, once bob actually visits.
        record_board_seen(seed.db, bob, carried_board, page.posts[0])
        assert unread_post_count(seed.db, bob, carried_board) == 0
        # Not double-indexed for search either -- the same reindex_post
        # "delete then insert" call every write path uses, exercised
        # here twice over the real materialization path.
        assert len(search_posts(seed.db, bob, "hello")) == 1
    finally:
        dialer.close()
        seed.close()


def test_linked_board_state_survives_seed_restart_using_only_persisted_state(tmp_path):
    """The carrying node is fully reconstructed from disk
    (`load_link_node`) between receiving the genesis and receiving the
    post -- proving the second event materializes correctly against
    genuinely reloaded `BoardEventState`/`BoardLifecycleState`
    (issue #78), not an in-memory object that happened to survive."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    seed_node = LinkNode(identity=seed_identity)
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)

    async def _push(port):
        async with aiohttp.ClientSession() as session:
            await _one_pass(dialer_node, session, [f"http://127.0.0.1:{port}"], lambda: _hello_for(dialer_node), dialer.lane)

    async def _push_stage():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            await _push(seed_server.port)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(_push_stage())

        restarted_seed_node = load_link_node(seed.db, seed_identity)
        assert restarted_seed_node is not seed_node
        assert board.board_id in restarted_seed_node.boards

        post = create_post(dialer.db, board, creator, "hello world", "first post")
        queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)

        async def _push_post_stage():
            seed_server = LinkServer(
                host="127.0.0.1", port=0, node=restarted_seed_node,
                own_hello_provider=lambda: _hello_for(restarted_seed_node), lane=seed.lane,
            )
            await seed_server.start()
            try:
                await _push(seed_server.port)
            finally:
                await seed_server.stop()

        asyncio.run(_push_post_stage())

        bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
        carried_board = get_board_by_name(seed.db, "general")
        page = list_posts_page(seed.db, carried_board, bob)
        assert [p.subject for p in page.posts] == ["hello world"]
    finally:
        dialer.close()
        seed.close()


# -- multi-hop inventory catch-up (design doc §8.8, issue #85) --------------


def test_linked_board_multi_hop_catch_up_via_a_third_node_over_real_transport(tmp_path):
    """The actual multi-hop proof over real transport, real SQLite, and
    real materialization -- `test_link_protocol.py` already proves this
    at the in-memory protocol layer; this proves the full vertical.

    `puller` catches up on posts it missed while not talking to `origin`
    directly, by pulling them from `relay` instead -- relay never
    originated any of this content, only carries it, and has stayed in
    regular contact with origin the whole time puller has not. Puller
    already independently completed a real hello with origin (during
    the one direct sync pass that gave it the board in the first
    place) -- the real-world precondition design doc §8.8's own
    limitation note names: multi-hop relays *content*, it never
    substitutes for a receiving node's own prior identity verification
    of who ultimately signed it. This is the "board already carried,
    fell behind on later posts" case the design doc explicitly scopes
    this issue to -- not discovery of a wholly novel board purely
    through a relay, which needs no direct genesis ever (see that same
    limitation note for why this issue deliberately doesn't solve that
    separate case)."""
    origin_identity = bootstrap_node_identity("origin")
    relay_identity = bootstrap_node_identity("relay")
    puller_identity = bootstrap_node_identity("puller")
    origin_node = LinkNode(identity=origin_identity)
    relay_node = LinkNode(identity=relay_identity)
    puller_node = LinkNode(identity=puller_identity)
    origin = _NodeDb(tmp_path, "origin")
    relay = _NodeDb(tmp_path, "relay")
    puller = _NodeDb(tmp_path, "puller")

    creator = create_user(origin.db, "alice", password="hunter2", user_level=10)
    board = create_board(origin.db, "general", creator=creator)
    link_board(origin.db, board, node_identity=origin_identity)
    first_post = create_post(origin.db, board, creator, "hello world", "first post")
    queue_board_post_if_linked(origin.db, first_post, board, node_identity=origin_identity)

    async def scenario():
        # Stage 1: origin dials *puller*'s server directly (content
        # flows dialer -> dialed server, via ordinary push) -- gives
        # puller the genesis and the first post, and completes a real
        # hello between the two in the process (puller's own server
        # processes origin's incoming hello).
        puller_server = await _run_server(puller_node, puller.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    origin_node, session, [f"http://127.0.0.1:{puller_server.port}"],
                    lambda: _hello_for(origin_node), origin.lane,
                )
        finally:
            await puller_server.stop()

        # Origin then posts more, while puller never talks to it
        # again -- simulating puller's own connection to origin having
        # become unreliable.
        second_post = create_post(origin.db, board, creator, "second post", "still going")
        queue_board_post_if_linked(origin.db, second_post, board, node_identity=origin_identity)
        third_post = create_post(origin.db, board, creator, "third post", "and more")
        queue_board_post_if_linked(origin.db, third_post, board, node_identity=origin_identity)

        # Relay, unlike puller, stays in contact with origin and ends
        # up with everything -- origin dials relay's server the same
        # way it dialed puller's above. Kept running for stage 2 below
        # too (origin's own server is never started again at this
        # point, so anything puller ends up with genuinely came via
        # relay's inventory response, not a disguised direct push).
        relay_server = await _run_server(relay_node, relay.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    origin_node, session, [f"http://127.0.0.1:{relay_server.port}"],
                    lambda: _hello_for(origin_node), origin.lane,
                )

                # Stage 2: puller dials relay's server.
                await _one_pass(
                    puller_node, session, [f"http://127.0.0.1:{relay_server.port}"],
                    lambda: _hello_for(puller_node), puller.lane,
                )
        finally:
            await relay_server.stop()

    try:
        asyncio.run(scenario())

        bob = create_user(puller.db, "bob", password="hunter2", user_level=10)
        carried_board = get_board_by_name(puller.db, "general")
        page = list_posts_page(puller.db, carried_board, bob)
        assert {p.subject for p in page.posts} == {"hello world", "second post", "third post"}

        # Genuinely visible through the same ordinary read paths the
        # direct-delivery test above already checks, not just a raw
        # posts-table row.
        assert {h.subject for h in search_posts(puller.db, bob, "post")} >= {"second post", "third post"}
    finally:
        origin.close()
        relay.close()
        puller.close()


def test_linked_board_multi_hop_catch_up_is_idempotent_across_repeated_inventory_passes(tmp_path):
    """The identical missing posts pulled via inventory across two
    sync passes (e.g. `more_available` still true, or simply another
    pass running before the peer has anything new) must not create
    duplicate rows -- the multi-hop counterpart to this file's own
    direct-delivery duplicate-delivery test above."""
    origin_identity = bootstrap_node_identity("origin")
    relay_identity = bootstrap_node_identity("relay")
    puller_identity = bootstrap_node_identity("puller")
    origin_node = LinkNode(identity=origin_identity)
    relay_node = LinkNode(identity=relay_identity)
    puller_node = LinkNode(identity=puller_identity)
    origin = _NodeDb(tmp_path, "origin")
    relay = _NodeDb(tmp_path, "relay")
    puller = _NodeDb(tmp_path, "puller")

    creator = create_user(origin.db, "alice", password="hunter2", user_level=10)
    board = create_board(origin.db, "general", creator=creator)
    link_board(origin.db, board, node_identity=origin_identity)
    first_post = create_post(origin.db, board, creator, "hello world", "first post")
    queue_board_post_if_linked(origin.db, first_post, board, node_identity=origin_identity)

    async def scenario():
        # Stage 1: origin dials puller's server (push) -- gives puller
        # the genesis and first post, and a completed hello with origin.
        puller_server = await _run_server(puller_node, puller.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    origin_node, session, [f"http://127.0.0.1:{puller_server.port}"],
                    lambda: _hello_for(origin_node), origin.lane,
                )
        finally:
            await puller_server.stop()

        second_post = create_post(origin.db, board, creator, "second post", "still going")
        queue_board_post_if_linked(origin.db, second_post, board, node_identity=origin_identity)

        # Origin dials relay's server (push) -- relay ends up with
        # everything; kept running for stage 2 below too.
        relay_server = await _run_server(relay_node, relay.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    origin_node, session, [f"http://127.0.0.1:{relay_server.port}"],
                    lambda: _hello_for(origin_node), origin.lane,
                )

                # Stage 2: puller dials relay's server twice -- the
                # second pass's own inventory request already reports
                # the post pulled in the first pass as known, but
                # nothing here should break if it didn't.
                relay_url = f"http://127.0.0.1:{relay_server.port}"
                await _one_pass(puller_node, session, [relay_url], lambda: _hello_for(puller_node), puller.lane)
                await _one_pass(puller_node, session, [relay_url], lambda: _hello_for(puller_node), puller.lane)
        finally:
            await relay_server.stop()

    try:
        asyncio.run(scenario())

        bob = create_user(puller.db, "bob", password="hunter2", user_level=10)
        carried_board = get_board_by_name(puller.db, "general")
        page = list_posts_page(puller.db, carried_board, bob)
        assert sorted(p.subject for p in page.posts) == ["hello world", "second post"]
    finally:
        origin.close()
        relay.close()
        puller.close()


def test_linked_file_area_multi_hop_catch_up_via_a_third_node_over_real_transport(tmp_path):
    """The file-area-catalogue counterpart to the linked-board multi-hop
    test above (design doc §11, issue #93): puller already carries
    origin's file area (from an earlier direct sync) but misses a later
    `file_descriptor` while never talking to origin again -- relay,
    which stayed in contact with origin the whole time, is the only path
    puller ever reaches that descriptor through. Origin's own server is
    never started again for stage 2, so anything puller ends up with
    genuinely came via relay's inventory response, not a disguised
    direct push -- same proof shape the board version above already
    establishes, extended to catalogue metadata. Chunk bytes are
    deliberately out of scope here (design doc §11, issue #93's own
    "out of scope: automatic content mirroring/prefetching"): this test
    only proves the catalogue entry itself is recoverable, not that its
    bytes get fetched automatically."""
    origin_identity = bootstrap_node_identity("origin")
    relay_identity = bootstrap_node_identity("relay")
    puller_identity = bootstrap_node_identity("puller")
    origin_node = LinkNode(identity=origin_identity)
    relay_node = LinkNode(identity=relay_identity)
    puller_node = LinkNode(identity=puller_identity)
    origin = _NodeDb(tmp_path, "origin")
    relay = _NodeDb(tmp_path, "relay")
    puller = _NodeDb(tmp_path, "puller")

    creator = create_user(origin.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(origin.db, "downloads", creator=creator)
    link_file_area(origin.db, area, node_identity=origin_identity)
    first_file = upload_file(origin.db, area, creator, "first.bin", b"first file bytes")
    queue_file_descriptor_if_linked(origin.db, first_file, area, node_identity=origin_identity)

    async def scenario():
        # Stage 1: origin dials puller's server directly -- gives puller
        # the area genesis and the first descriptor, and completes a
        # real hello between the two.
        puller_server = await _run_server(puller_node, puller.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    origin_node, session, [f"http://127.0.0.1:{puller_server.port}"],
                    lambda: _hello_for(origin_node), origin.lane,
                )
        finally:
            await puller_server.stop()

        # Origin then catalogues a second file, while puller never talks
        # to it again.
        second_file = upload_file(origin.db, area, creator, "second.bin", b"second file bytes")
        queue_file_descriptor_if_linked(origin.db, second_file, area, node_identity=origin_identity)

        # Relay stays in contact with origin and ends up with
        # everything -- kept running for stage 2 below too. Origin's own
        # server is never started again at this point.
        relay_server = await _run_server(relay_node, relay.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    origin_node, session, [f"http://127.0.0.1:{relay_server.port}"],
                    lambda: _hello_for(origin_node), origin.lane,
                )

                # Stage 2: puller dials relay's server.
                await _one_pass(
                    puller_node, session, [f"http://127.0.0.1:{relay_server.port}"],
                    lambda: _hello_for(puller_node), puller.lane,
                )
        finally:
            await relay_server.stop()

    try:
        asyncio.run(scenario())

        carried_area = get_file_area_by_name(puller.db, "downloads")
        remote_files = list_remote_files(puller.db, carried_area)
        assert {rf.filename for rf in remote_files} == {"first.bin", "second.bin"}
        # Recovered purely as catalogue metadata -- never auto-fetched.
        assert all(rf.fetched_file_id is None for rf in remote_files)
    finally:
        origin.close()
        relay.close()
        puller.close()


# -- linked channels: full vertical (design doc §9.6, issue #87) ------------


def test_linked_channel_message_full_vertical_materializes_and_is_visible_via_ordinary_read_paths(tmp_path):
    """The channel-side counterpart to the linked-board full-vertical
    test above, per §14.1's own rule that a new Link vertical slice
    isn't complete without a scenario here: a channel_message reaches a
    real peer over a real socket, materializes into a real `channel_
    messages` row, and is genuinely visible through every ordinary
    user-facing read path on the carrying node -- `get_scrollback`
    (browsing), `netbbs.search.search_channel_messages` ([F]ind), and
    `netbbs.activity.unread_channel_count` (New Scan)."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    channel = create_channel(dialer.db, "lobby", creator=creator)
    link_channel(dialer.db, channel, node_identity=dialer_identity)
    message = record_message(dialer.db, channel, kind="message", author_label="alice", body="hello there")
    queue_channel_message_if_linked(dialer.db, message, channel, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane,
                )
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())

        bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
        carried_channel = get_channel_by_name(seed.db, "lobby")

        # Ordinary browsing.
        scrollback = get_scrollback(seed.db, carried_channel)
        assert [m.body for m in scrollback] == ["hello there"]
        assert scrollback[0].author_label == f"alice@{dialer_identity.fingerprint}"

        # [F]ind.
        hits = search_channel_messages(seed.db, bob, "hello", visible_channels=[carried_channel])
        assert [h.body for h in hits] == ["hello there"]

        # New Scan: never visited yet, so unread is None, then 0 once seen.
        assert unread_channel_count(seed.db, bob, carried_channel) is None
        record_channel_seen(seed.db, bob, carried_channel, scrollback[0])
        assert unread_channel_count(seed.db, bob, carried_channel) == 0
    finally:
        dialer.close()
        seed.close()


def test_linked_channel_message_duplicate_delivery_over_real_transport_is_idempotent(tmp_path):
    """The channel-side counterpart to the linked-board duplicate-
    delivery test above."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    channel = create_channel(dialer.db, "lobby", creator=creator)
    link_channel(dialer.db, channel, node_identity=dialer_identity)
    message = record_message(dialer.db, channel, kind="message", author_label="alice", body="hello there")
    queue_channel_message_if_linked(dialer.db, message, channel, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                seed_url = f"http://127.0.0.1:{seed_server.port}"
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
                await _one_pass(dialer_node, session, [seed_url], lambda: _hello_for(dialer_node), dialer.lane)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
        carried_channel = get_channel_by_name(seed.db, "lobby")
        scrollback = get_scrollback(seed.db, carried_channel)
        assert len(scrollback) == 1
        record_channel_seen(seed.db, bob, carried_channel, scrollback[0])
        assert unread_channel_count(seed.db, bob, carried_channel) == 0
        assert len(search_channel_messages(seed.db, bob, "hello", visible_channels=[carried_channel])) == 1
    finally:
        dialer.close()
        seed.close()


def test_linked_channel_state_survives_seed_restart_using_only_persisted_state(tmp_path):
    """The channel-side counterpart to the linked-board restart test
    above -- proves the second event materializes correctly against a
    genuinely reloaded `ChannelEventState` (issue #87), not an in-memory
    object that happened to survive."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    seed_node = LinkNode(identity=seed_identity)
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    channel = create_channel(dialer.db, "lobby", creator=creator)
    link_channel(dialer.db, channel, node_identity=dialer_identity)

    async def _push(port):
        async with aiohttp.ClientSession() as session:
            await _one_pass(dialer_node, session, [f"http://127.0.0.1:{port}"], lambda: _hello_for(dialer_node), dialer.lane)

    async def _push_stage():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            await _push(seed_server.port)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(_push_stage())

        restarted_seed_node = load_link_node(seed.db, seed_identity)
        assert restarted_seed_node is not seed_node
        assert channel.channel_id in restarted_seed_node.channels

        message = record_message(dialer.db, channel, kind="message", author_label="alice", body="hello there")
        queue_channel_message_if_linked(dialer.db, message, channel, node_identity=dialer_identity)

        async def _push_message_stage():
            seed_server = LinkServer(
                host="127.0.0.1", port=0, node=restarted_seed_node,
                own_hello_provider=lambda: _hello_for(restarted_seed_node), lane=seed.lane,
            )
            await seed_server.start()
            try:
                await _push(seed_server.port)
            finally:
                await seed_server.stop()

        asyncio.run(_push_message_stage())

        carried_channel = get_channel_by_name(seed.db, "lobby")
        scrollback = get_scrollback(seed.db, carried_channel)
        assert [m.body for m in scrollback] == ["hello there"]
    finally:
        dialer.close()
        seed.close()


def test_linked_channel_message_sent_via_the_live_interactive_chat_path_reaches_a_real_peer(tmp_path):
    """Design doc, issue #91: unlike the full-vertical test above (which
    builds the outbound event directly via `queue_channel_message_if_
    linked`), this one begins at the actual interactive send path --
    `netbbs.net.chat_flow._chat_loop`, driven with a scripted `FakeSession`
    exactly the way a real typed chat line would be -- and proves the
    remote node still materializes/displays the message after an ordinary
    sync pass. The dialer's own `_chat_loop` call is given a real
    `link_context`, so a plain typed line queues its own `channel_message`
    the same way `netbbs.net.login_flow._compose_new_post`'s board-post
    path already does for boards."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    channel = create_channel(dialer.db, "lobby", creator=creator)
    link_channel(dialer.db, channel, node_identity=dialer_identity)
    link_context = LinkContext(node_identity=dialer_identity, link_node=dialer_node)

    async def scenario():
        # Stage 1: the interactive send path, not a direct domain call --
        # a scripted session types one line, then /quit.
        chat_session = FakeSession(["hello there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(
                chat_session, dialer.lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(),
                channel, creator, link_context=link_context,
            ),
            timeout=2,
        )

        # Stage 2: an ordinary sync pass pushes whatever got queued.
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane,
                )
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())

        bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
        carried_channel = get_channel_by_name(seed.db, "lobby")

        scrollback = get_scrollback(seed.db, carried_channel)
        assert [m.body for m in scrollback if m.kind == "message"] == ["hello there"]
        assert unread_channel_count(seed.db, bob, carried_channel) is None
        record_channel_seen(seed.db, bob, carried_channel, scrollback[-1])
        assert unread_channel_count(seed.db, bob, carried_channel) == 0
    finally:
        dialer.close()
        seed.close()


# -- remote file catalogue + chunk transfer (design doc §11, issue #89) --


def test_remote_file_full_vertical_catalogue_then_chunked_fetch_over_real_transport(tmp_path):
    """Design doc §11/§14.1's own vertical-slice rule, applied to the
    genuinely new mechanism this issue adds: a file_area_genesis/file_
    descriptor pair reaches a real peer over a real socket via the
    ordinary push pass (catalogue discovery, no content fetched yet --
    the acceptance criterion's own "list without fetching"), then the
    seed pulls the actual bytes directly from the dialer's own server in
    bounded chunks, verifies them, and the result is genuinely visible
    through the ordinary local file-browsing read path (`get_file`/
    `download_file`), not just a raw row check."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    seed_node = LinkNode(identity=seed_identity)
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(dialer.db, "downloads", creator=creator)
    content = os.urandom(300_000)
    entry = upload_file(dialer.db, area, creator, "game.bin", content)
    link_file_area(dialer.db, area, node_identity=dialer_identity)
    queue_file_descriptor_if_linked(dialer.db, entry, area, node_identity=dialer_identity)

    async def scenario():
        dialer_server = await _run_server(dialer_node, dialer.lane)
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                # Stage 1: ordinary push pass -- catalogue discovery only.
                await _one_pass(
                    dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane,
                )

                # Confirm catalogue-only state before any fetch: listed,
                # but not yet downloadable.
                carried_area = get_file_area_by_name(seed.db, "downloads")
                catalogued = list_remote_files(seed.db, carried_area)
                assert len(catalogued) == 1
                remote_file = catalogued[0]
                assert remote_file.filename == "game.bin"
                assert remote_file.size_bytes == len(content)
                assert remote_file.fetched_file_id is None

                # Stage 2: seed pulls the actual bytes directly from the
                # dialer's own server, in small chunks (forcing several
                # round trips rather than one).
                dialer_base_url = f"http://127.0.0.1:{dialer_server.port}"
                transfer = await fetch_next_file_chunk(
                    seed_node, session, dialer_base_url, seed.lane, remote_file, chunk_size=100_000,
                )
                while transfer.status == "in_progress":
                    transfer = await fetch_next_file_chunk(
                        seed_node, session, dialer_base_url, seed.lane, remote_file, chunk_size=100_000,
                    )
                assert transfer.status == "completed"
        finally:
            await dialer_server.stop()
            await seed_server.stop()

    try:
        asyncio.run(scenario())

        # Ordinary local read path -- not a raw row check.
        fetched_remote = get_remote_file(seed.db, entry.file_id)
        assert fetched_remote.fetched_file_id == entry.file_id
        fetched_entry = get_file(seed.db, entry.file_id)
        assert fetched_entry.filename == "game.bin"
        assert download_file(fetched_entry) == content
    finally:
        dialer.close()
        seed.close()


def test_remote_file_duplicate_chunk_request_over_real_transport_is_idempotent(tmp_path):
    """The real-transport counterpart to test_link_file_transfer.py's
    in-process duplicate-chunk-request proof -- re-requesting an already-
    applied chunk over an actual socket must not corrupt the eventual
    reassembly."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    seed_node = LinkNode(identity=seed_identity)
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(dialer.db, "downloads", creator=creator)
    content = os.urandom(250_000)
    entry = upload_file(dialer.db, area, creator, "game.bin", content)
    link_file_area(dialer.db, area, node_identity=dialer_identity)
    queue_file_descriptor_if_linked(dialer.db, entry, area, node_identity=dialer_identity)

    async def scenario():
        dialer_server = await _run_server(dialer_node, dialer.lane)
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await _one_pass(
                    dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane,
                )
                carried_area = get_file_area_by_name(seed.db, "downloads")
                remote_file = list_remote_files(seed.db, carried_area)[0]
                dialer_base_url = f"http://127.0.0.1:{dialer_server.port}"

                # Fetch the first chunk, then re-request that exact same
                # chunk_index again over the real socket (a resent/
                # duplicate request, e.g. a client-side retry after a
                # dropped response) before continuing -- fetch_next_file_
                # chunk always advances to the next index, so the repeat
                # is built manually here via the lower-level request_file_
                # chunk/apply_received_chunk pair it's built from.
                first = await fetch_next_file_chunk(
                    seed_node, session, dialer_base_url, seed.lane, remote_file, chunk_size=100_000,
                )
                assert first.status == "in_progress"

                transfer_id = compute_transfer_id(remote_file.file_id, seed_identity.fingerprint)
                repeat_request = FileChunkRequest(
                    transfer_id=transfer_id, file_id=remote_file.file_id, chunk_index=0, max_chunk_size=100_000,
                )
                repeat_bytes, repeat_descriptor = await request_file_chunk(
                    seed_node, session, dialer_base_url, repeat_request,
                )
                transfer_before = await seed.lane.run(get_transfer, transfer_id)
                repeated = await seed.lane.run(
                    apply_received_chunk, transfer_before, chunk_index=0, chunk_bytes=repeat_bytes,
                    claimed_chunk_sha256=repeat_descriptor.payload["chunk_sha256"], is_last=False,
                    remote_file=remote_file,
                )
                assert repeated.bytes_received == first.bytes_received  # unchanged, not double-counted

                transfer = repeated
                while transfer.status == "in_progress":
                    transfer = await fetch_next_file_chunk(
                        seed_node, session, dialer_base_url, seed.lane, remote_file, chunk_size=100_000,
                    )
                assert transfer.status == "completed"
        finally:
            await dialer_server.stop()
            await seed_server.stop()

    try:
        asyncio.run(scenario())

        fetched_entry = get_file(seed.db, entry.file_id)
        assert download_file(fetched_entry) == content  # not corrupted by the duplicate request
    finally:
        dialer.close()
        seed.close()


def test_remote_file_browse_and_fetch_via_the_live_interactive_ui_flow(tmp_path):
    """Design doc, issue #92's own acceptance criterion: a full
    interactive-flow regression test proving browse -> fetch -> verify/
    promote -> ordinary download visibility, driven through the actual
    `netbbs.net.file_flow._show_area` UI (`/remote` command, `pick_item`
    selection, the fetch confirmation prompt), not a direct domain call."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    seed_node = LinkNode(identity=seed_identity)
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(dialer.db, "downloads", creator=creator)
    content = os.urandom(50_000)
    entry = upload_file(dialer.db, area, creator, "game.bin", content)
    link_file_area(dialer.db, area, node_identity=dialer_identity)
    queue_file_descriptor_if_linked(dialer.db, entry, area, node_identity=dialer_identity)

    ui_session = PickerFakeSession(["/remote", "0", "1", "y"])

    async def scenario():
        dialer_server = await _run_server(dialer_node, dialer.lane)
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                # Push the catalogue -- the dialer's own hello advertises
                # its real address, so the seed can dial it back directly
                # for the chunk fetch below (chunk transfer is never
                # relayed).
                await _one_pass(
                    dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                    lambda: _dialable_hello(dialer_node, dialer_server), dialer.lane,
                )

            bob = create_user(seed.db, "bob", password="hunter2", user_level=10)
            carried_area = get_file_area_by_name(seed.db, "downloads")
            link_context = LinkContext(node_identity=seed_identity, link_node=seed_node)

            # Not yet fetched, before the interactive flow.
            assert get_remote_file(seed.db, entry.file_id).fetched_file_id is None

            # The dialer's own server must still be running here --
            # chunk transfer is a direct pull against the file's origin,
            # dialed fresh, not carried over from the push pass above.
            await file_flow._show_area(ui_session, seed.lane, carried_area, bob, link_context=link_context)
        finally:
            await seed_server.stop()
            await dialer_server.stop()

    asyncio.run(scenario())

    try:
        output = "".join(ui_session.written)
        assert "fetched and verified" in output

        # Ordinary local read path -- not a raw row check.
        fetched_remote = get_remote_file(seed.db, entry.file_id)
        assert fetched_remote.fetched_file_id == entry.file_id
        fetched_entry = get_file(seed.db, entry.file_id)
        assert fetched_entry.filename == "game.bin"
        assert download_file(fetched_entry) == content
    finally:
        dialer.close()
        seed.close()
