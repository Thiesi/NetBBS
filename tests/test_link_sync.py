"""
Tests for `netbbs.link.sync` (design doc §12, round 119) — the
background loop that makes a node *originate* outbound Link activity.
Drives real `LinkServer` instances (`tests/test_link_transport.py`'s
own real-server/real-client convention) rather than `ScriptedTransport`,
since the whole point is proving the loop actually reaches a peer over
a real socket, pushes real events, and tolerates a real peer being
unreachable or rejecting it.

Round 120: `run_link_sync`/`dial_hello` persist through a
`DatabaseLane`, so every node here gets a real, separately-opened
`Database` file too -- see `tests/test_link_transport.py`'s module
docstring for why a `Database`/`DatabaseLane` pair, not just one.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, edit_post
from netbbs.link.boards import link_board, queue_board_post_edit_if_linked, queue_board_post_if_linked
from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.mail import compose_link_message
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import LinkNode
from netbbs.link.seedlist import set_cached_supplementary_seeds
from netbbs.link.sync import run_link_sync
from netbbs.link.transport import LinkServer
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _hello_for(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00"):
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


async def _run_server(node: LinkNode, lane: DatabaseLane) -> LinkServer:
    server = LinkServer(
        host="127.0.0.1", port=0, node=node, own_hello_provider=lambda: _hello_for(node), lane=lane
    )
    await server.start()
    return server


async def _run_sync_briefly(coro_task: asyncio.Task, *, settle: float = 0.2) -> None:
    """Lets a run_link_sync task run for a bit, then cancels it cleanly
    -- mirrors how netbbs.__main__ will eventually cancel this same
    task on node shutdown."""
    await asyncio.sleep(settle)
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass


class _NodeDb:
    """A node's paired `Database` (test assertions) and `DatabaseLane`
    (what the code under test dispatches through) against the same
    file -- see this module's docstring."""

    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


def test_sync_completes_a_hello_and_pushes_events_to_a_real_seed(tmp_path):
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            rotated = rotate_operational_key(dialer_identity, purpose="signing")
            dialer_node.identity = rotated

            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

        return seed_node

    try:
        seed_node_after = asyncio.run(scenario())
        assert dialer_identity.fingerprint in seed_node_after.peers
        peer_record = seed_node_after.peers[dialer_identity.fingerprint]
        # Both halves of the rotation (revoke + authorize, design doc round
        # 116's own ordering note) reached the seed -- via the hello (which
        # already carried them, since the rotation happened before the
        # first sync pass) and via push_events (round 119: pushes *all* of
        # identity.transitions, including the not-yet-seen transport-purpose
        # transition, which lands after them in the flat tuple -- round
        # 121: no longer a duplicate-signing_orig rejection, so this
        # assertion checks membership, not tuple position, which was never
        # a meaningful proxy for "current head" once more than one purpose
        # is interleaved in the same flat PeerRecord.transitions tuple).
        peer_content_ids = {t.content_id for t in peer_record.transitions}
        for transition in dialer_node.identity.transitions:
            assert transition.content_id in peer_content_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_requests_and_persists_a_seeds_peer_list(tmp_path):
    """Round 95: _sync_one_seed also asks the seed who else it knows,
    right after the hello -- the seed here already has carol as a
    completed peer of its own; one sync pass should leave the dialer
    with carol as a recorded (unverified) candidate, on disk too."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    carol_identity = bootstrap_node_identity("carol")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    carol_hello = _hello_for(LinkNode(identity=carol_identity))
    seed_node.handle_hello(carol_hello)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert carol_identity.fingerprint in dialer_node.candidate_descriptors
        assert carol_identity.fingerprint not in dialer_node.peers
        row = dialer.db.connection.execute("SELECT fingerprint FROM link_peer_candidates").fetchone()
        assert row["fingerprint"] == carol_identity.fingerprint
    finally:
        dialer.close()
        seed.close()


def test_sync_dials_a_live_cached_supplementary_seed_not_in_the_operator_list(tmp_path):
    """Round 97: a seed the operator never configured, but that a
    (simulated) live seed-list refresh already cached, still gets
    dialed -- proves the per-pass merge actually happens, not just that
    the cache-read function exists."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        seed_url = f"http://127.0.0.1:{seed_server.port}"
        # Not passed as an operator-configured seed below -- only cached,
        # as if a prior run_scheduled_seed_refresh pass had already
        # fetched it.
        set_cached_supplementary_seeds(dialer.db, [seed_url])
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [],  # no operator-configured seeds at all
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_identity.fingerprint in seed_node.peers  # the dial actually reached the seed
    finally:
        dialer.close()
        seed.close()


# -- candidate fallback (design doc round 95) --------------------------------


def _seed_candidate(dialer_node: LinkNode, candidate_identity, *, port: int) -> None:
    """Directly populates a candidate descriptor for a real running
    server, as if an earlier peer-list exchange had already discovered
    it -- no protocol round trip needed to set up this test state."""
    descriptor = build_endpoint_descriptor(
        signing_identity=candidate_identity.signing_key,
        subject_fingerprint=candidate_identity.fingerprint,
        addresses=[{"protocol": "http", "address": "127.0.0.1", "port": port}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    dialer_node.candidate_descriptors[candidate_identity.fingerprint] = descriptor


def test_sync_falls_back_to_a_candidate_when_the_only_seed_fails(tmp_path):
    dialer_identity = bootstrap_node_identity("dialer")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    candidate = _NodeDb(tmp_path, "candidate")

    async def scenario():
        candidate_server = await _run_server(candidate_node, candidate.lane)
        _seed_candidate(dialer_node, candidate_identity, port=candidate_server.port)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, ["http://127.0.0.1:1"],  # the only seed, dead
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=3.0)
        finally:
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert candidate_identity.fingerprint in dialer_node.peers  # reached via fallback
        assert candidate_identity.fingerprint not in dialer_node.candidate_descriptors  # promoted, not left behind
        assert dialer_identity.fingerprint in candidate_node.peers  # the dial really landed
    finally:
        dialer.close()
        candidate.close()


def test_sync_falls_back_when_no_seeds_are_configured_at_all(tmp_path):
    """The brand-new-node case round 97/95 both name as the one this
    resilience path matters most for."""
    dialer_identity = bootstrap_node_identity("dialer")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    candidate = _NodeDb(tmp_path, "candidate")

    async def scenario():
        candidate_server = await _run_server(candidate_node, candidate.lane)
        _seed_candidate(dialer_node, candidate_identity, port=candidate_server.port)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [],  # zero seeds, zero cached supplementary seeds
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=3.0)
        finally:
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert candidate_identity.fingerprint in dialer_node.peers
    finally:
        dialer.close()
        candidate.close()


def test_sync_does_not_fall_back_when_a_seed_succeeds(tmp_path):
    """A candidate must never be dialed just because it's known -- only
    when every seed this pass genuinely failed."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")
    candidate = _NodeDb(tmp_path, "candidate")

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        candidate_server = await _run_server(candidate_node, candidate.lane)
        _seed_candidate(dialer_node, candidate_identity, port=candidate_server.port)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_identity.fingerprint in seed_node.peers  # the real seed was reached
        assert candidate_identity.fingerprint not in dialer_node.peers  # candidate never dialed
        assert dialer_identity.fingerprint not in candidate_node.peers
    finally:
        dialer.close()
        seed.close()
        candidate.close()


def test_sync_respects_the_fallback_attempt_cap(tmp_path, monkeypatch):
    import netbbs.link.sync as sync_module

    monkeypatch.setattr(sync_module, "_MAX_CANDIDATE_FALLBACK_ATTEMPTS", 1)

    dialer_identity = bootstrap_node_identity("dialer")
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")

    # Two candidates, both genuinely undialable (dead ports) -- with the
    # cap patched to 1, only one of the two should ever be attempted.
    # Since neither can succeed, the observable proxy here is that the
    # pass completes and returns (proven by _run_sync_briefly not
    # timing out) rather than hanging trying every candidate forever --
    # a weak assertion on its own, strengthened by counting attempts
    # via a wrapped dial.
    first_identity = bootstrap_node_identity("first")
    second_identity = bootstrap_node_identity("second")
    _seed_candidate(dialer_node, first_identity, port=1)
    _seed_candidate(dialer_node, second_identity, port=2)

    attempted_urls: list[str] = []
    original_dial_hello = sync_module.dial_hello

    async def counting_dial_hello(node, session, base_url, *args, **kwargs):
        attempted_urls.append(base_url)
        return await original_dial_hello(node, session, base_url, *args, **kwargs)

    monkeypatch.setattr(sync_module, "dial_hello", counting_dial_hello)

    async def scenario():
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                run_link_sync(
                    dialer_node, session, ["http://127.0.0.1:1"],  # the only "seed", also dead
                    lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                )
            )
            await _run_sync_briefly(task, settle=3.0)

    try:
        asyncio.run(scenario())
        # One call for the dead seed itself, plus at most one fallback
        # candidate attempt (the cap) -- never both candidates.
        candidate_urls = [u for u in attempted_urls if u != "http://127.0.0.1:1"]
        assert len(candidate_urls) <= 1
    finally:
        dialer.close()


def test_sync_pushes_own_linked_board_genesis_and_post_to_a_real_seed(tmp_path):
    """Round 128: `_sync_one_seed` also pushes this node's own `board_
    genesis`/`board_post` events, read fresh off the `boards`/`posts`
    tables (`netbbs.link.boards.load_own_board_events`) via the same
    `lane` already used for `dial_hello`'s own persistence -- proves
    they actually reach a real peer over a real socket, not just that
    the query returns the right rows."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert genesis.content_id in seed_node.known_event_ids
        assert board.board_id in seed_node.boards
        assert board_post.content_id in seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_pushes_a_self_authored_board_post_edit_to_a_real_seed(tmp_path):
    """Round 130: `load_own_board_events` also gathers this node's own
    `board_post_edit` events (stored on the edited revision's own
    `posts.link_event_json` column) -- proves one actually reaches a
    real peer and lands correctly in `seed_node.post_edits`."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)
    edited = edit_post(dialer.db, post, board, subject="hello (edited)", body="world, edited", edited_by=creator)
    edit = queue_board_post_edit_if_linked(dialer.db, edited, board, node_identity=dialer_identity, edited_by=creator)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert board_post.content_id in seed_node.known_event_ids
        assert edit.content_id in seed_node.known_event_ids
        assert seed_node.post_edits[board_post.content_id][-1].content_id == edit.content_id
    finally:
        dialer.close()
        seed.close()


def test_sync_dials_every_configured_seed_in_one_pass(tmp_path):
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    seed_a_node = LinkNode(identity=bootstrap_node_identity("seed-a"))
    seed_b_node = LinkNode(identity=bootstrap_node_identity("seed-b"))
    dialer = _NodeDb(tmp_path, "dialer")
    seed_a = _NodeDb(tmp_path, "seed-a")
    seed_b = _NodeDb(tmp_path, "seed-b")

    async def scenario():
        seed_a_server = await _run_server(seed_a_node, seed_a.lane)
        seed_b_server = await _run_server(seed_b_node, seed_b.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session,
                        [f"http://127.0.0.1:{seed_a_server.port}", f"http://127.0.0.1:{seed_b_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_a_server.stop()
            await seed_b_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_node.identity.fingerprint in seed_a_node.peers
        assert dialer_node.identity.fingerprint in seed_b_node.peers
    finally:
        dialer.close()
        seed_a.close()
        seed_b.close()


def test_sync_skips_an_unreachable_seed_without_crashing_the_loop(tmp_path):
    """A dead seed (port 1, nothing listening) must not prevent a
    *later* reachable seed in the same pass from being dialed. A
    generous settle window -- how long a real "connection refused" to
    a privileged port takes to surface at the OS level isn't something
    this test controls, and a short one flaked here on a sandbox where
    it took longer than expected."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    reachable_node = LinkNode(identity=bootstrap_node_identity("reachable"))
    dialer = _NodeDb(tmp_path, "dialer")
    reachable = _NodeDb(tmp_path, "reachable")

    async def scenario():
        reachable_server = await _run_server(reachable_node, reachable.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session,
                        ["http://127.0.0.1:1", f"http://127.0.0.1:{reachable_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=3.0)
        finally:
            await reachable_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_node.identity.fingerprint in reachable_node.peers
    finally:
        dialer.close()
        reachable.close()


def test_sync_runs_a_second_pass_after_the_interval_elapses(tmp_path):
    """A short interval must produce a *second* completed hello, not
    just the immediate first-pass one -- proves the sleep-then-repeat
    shape actually repeats, not just runs once."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    hello_count = 0
    real_handle_hello = seed_node.handle_hello

    def _counting_handle_hello(message):
        nonlocal hello_count
        hello_count += 1
        return real_handle_hello(message)

    seed_node.handle_hello = _counting_handle_hello

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=0.05,
                    )
                )
                await _run_sync_briefly(task, settle=0.3)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert hello_count >= 2
    finally:
        dialer.close()
        seed.close()


def test_sync_is_cleanly_cancellable_mid_sleep(tmp_path):
    """Cancelling during the interval sleep (not mid-dial) must still
    propagate CancelledError cleanly, the same contract netbbs.__main__
    already relies on for its other background tasks (e.g. the
    daybreak announcer)."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    dialer = _NodeDb(tmp_path, "dialer")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                run_link_sync(
                    dialer_node, session, [], lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0
                )
            )
            await asyncio.sleep(0.05)  # past the (empty) seed pass, into the sleep
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(scenario())
    finally:
        dialer.close()


def test_sync_pushes_pending_link_mail_directly_to_its_known_recipient(tmp_path):
    """Round 93's routing decision, proved over a real socket: a pending
    `link_message` is pushed straight to its own recipient node using
    the address already on file for it (from a prior hello), not to
    whichever seeds happen to be configured. Uses the recipient itself
    as the configured "seed" for the first pass -- exactly what lets
    the dialer resolve its signing key (to compose to it) and its
    address (to reach it directly) in the first place, per this
    module's own docstring on why a target must already be a known
    peer."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def scenario():
        # Unlike _hello_for/_run_server's own outgoing_only=True default
        # (fine for every other test here, which only ever pushes *to* a
        # statically-configured seed URL), the recipient must advertise
        # a real, dialable address in its own hello -- that's the only
        # way the dialer's later _dialable_address lookup has anything
        # to find for it.
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
                # First pass: just the hello, so the dialer learns
                # recipient's signing key/address.
                first_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [seed_url], lambda: _hello_for(dialer_node),
                        dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(first_pass)

                message = compose_link_message(
                    dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
                    node_identity=dialer_identity,
                )

                # Second pass: the pending message should now reach
                # recipient directly.
                second_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [seed_url], lambda: _hello_for(dialer_node),
                        dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(second_pass)
        finally:
            await recipient_server.stop()

        return message

    try:
        message = asyncio.run(scenario())
        assert message.content_id in recipient_node.known_event_ids
        row = recipient.db.connection.execute(
            "SELECT subject, body, link_source_event_id FROM mail_messages"
        ).fetchone()
        assert row["subject"] == "hello"
        assert row["body"] == "world"
        assert row["link_source_event_id"] == message.content_id
    finally:
        dialer.close()
        recipient.close()
