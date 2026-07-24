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

import aiohttp

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board, get_board_by_name
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.link.boards import link_board, queue_board_post_if_linked
from netbbs.link.channels import link_channel, queue_channel_message_if_linked
from netbbs.link.mail import compose_link_message, expire_link_message_delivery, unexpire_link_message_delivery
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.store import load_link_node
from netbbs.link.sync import run_link_sync
from netbbs.link.transport import LinkServer
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
