"""Activity days for probation graduation (design doc §12.4, issue #1035).

`record_activity` existed but nothing called it, so no remote node or caller
ever had a single activity day and none could graduate automatically: the only
ways off probation were a SysOp override and establishment."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import aiohttp

from netbbs.link.enforcement import ensure_node_subject, record_author_activity, record_direct_activity
from netbbs.link.events import build_board_genesis, build_board_post
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.transport import dial_hello, push_events
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    get_effective_trust_state,
    node_probation,
    recompute_all_trust_states,
    set_trust_override,
)
from tests.test_link_transport import _NodeDb, _hello_for, _run_server
from tests.test_link_trust import NOW, add_vouch, configure_reporter, db, stamp  # noqa: F401 -- db is a fixture


def _activity_days(db, subject: TrustSubject) -> int:
    return db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_activity_days WHERE subject_id = ?", (subject.subject_id,)
    ).fetchone()[0]


def _user_post(home: str, user: str = "nib") -> dict:
    return {"envelope": {"object_type": "board_post", "payload": {"author": {
        "kind": "node_vouched_user", "home_node_fingerprint": home, "local_user_id": user,
    }}}}


def test_a_node_with_direct_interaction_on_three_dates_graduates(db):
    peer = "p" * 32
    subject = TrustSubject.node(peer)
    ensure_node_subject(db, peer, accepted_at=stamp(NOW - timedelta(days=40)))
    configure_reporter(db, "reporter-a", "domain-a", node_vouch=True)
    configure_reporter(db, "reporter-b", "domain-b", node_vouch=True)
    add_vouch(db, subject, "reporter-a", 1)
    add_vouch(db, subject, "reporter-b", 2)

    record_direct_activity(db, peer, now_iso=stamp(NOW - timedelta(days=2)))
    # The same UTC date twice is one day, however often the node is met.
    record_direct_activity(db, peer, now_iso=stamp(NOW - timedelta(days=2, hours=-3)))
    record_direct_activity(db, peer, now_iso=stamp(NOW - timedelta(days=1)))
    assert _activity_days(db, subject) == 2
    assert get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY).state == TrustState.PROBATIONARY

    record_direct_activity(db, peer, now_iso=stamp(NOW))
    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert (state.state, state.reason_code) == (TrustState.ESTABLISHED, "automatic_graduation")


def test_a_user_with_accepted_activity_on_three_dates_graduates(db):
    home = "h" * 32
    subject = TrustSubject.user(home, "nib")
    configure_reporter(db, "reporter-a", "domain-a", user_vouch=True)

    first = NOW - timedelta(days=20)
    record_author_activity(db, _user_post(home), now_iso=stamp(first))
    add_vouch(db, subject, "reporter-a", 1)
    record_author_activity(db, _user_post(home), now_iso=stamp(first + timedelta(days=1)))
    record_author_activity(db, _user_post(home), now_iso=stamp(first + timedelta(days=1, hours=2)))
    assert _activity_days(db, subject) == 2

    record_author_activity(db, _user_post(home), now_iso=stamp(NOW))
    for dimension in TrustDimension:
        assert get_effective_trust_state(db, subject, dimension).state == TrustState.ESTABLISHED


def test_content_a_node_authored_is_not_counted_as_interaction(db):
    # A carrier bringing a node's own genesis is not an interaction with it.
    origin = bootstrap_node_identity("origin")
    genesis = build_board_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id="b-1", name="Inks", created_at="2026-07-26T00:00:00+00:00",
    )
    assert record_author_activity(db, genesis.to_dict(), now_iso=stamp(NOW)) == TrustSubject.node(origin.fingerprint)
    assert _activity_days(db, TrustSubject.node(origin.fingerprint)) == 0


def test_hellos_and_an_accepted_push_count_as_activity_over_real_transport(tmp_path):
    """Both ends of a hello count a day for the other, and an accepted push
    counts a day for each remote caller it carried."""
    dialer_identity = bootstrap_node_identity("activity-dialer")
    seed_identity = bootstrap_node_identity("activity-seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "activity-dialer")
    seed = _NodeDb(tmp_path, "activity-seed")
    genesis = build_board_genesis(
        signing_identity=dialer_identity.signing_key, origin_fingerprint=dialer_identity.fingerprint,
        board_id="b-activity", name="Inks", created_at="2026-08-14T12:00:00+00:00",
    )
    post = build_board_post(
        signing_identity=dialer_identity.signing_key, home_node_fingerprint=dialer_identity.fingerprint,
        local_user_id="nib", board_id="b-activity", subject="Hi", body="First post",
        created_at="2026-08-14T12:01:00+00:00",
    )

    async def scenario():
        server = await _run_server(
            seed_node, lambda: _hello_for(seed_node), seed.lane, enforce_trust_policy=True,
        )
        try:
            async with aiohttp.ClientSession() as session:
                base_url = f"http://127.0.0.1:{server.port}"
                await dial_hello(dialer_node, session, base_url, _hello_for(dialer_node), dialer.lane)
                # The hello alone is a day of direct interaction.
                assert _activity_days(seed.db, TrustSubject.node(dialer_identity.fingerprint)) == 1
                # A node on probation here may say hello but not publish.
                for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
                    set_trust_override(
                        seed.db, TrustSubject.node(dialer_identity.fingerprint), dimension,
                        TrustState.ESTABLISHED, reason="known peer",
                    )
                await push_events(dialer_node, session, base_url, [genesis, post])
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert _activity_days(seed.db, TrustSubject.node(dialer_identity.fingerprint)) == 1
        assert _activity_days(seed.db, TrustSubject.user(dialer_identity.fingerprint, "nib")) == 1
        # The probation screen (#844) reads the same count.
        seed.db.connection.execute(
            "DELETE FROM link_trust_overrides WHERE subject_id = ?",
            (TrustSubject.node(dialer_identity.fingerprint).subject_id,),
        )
        seed.db.connection.commit()
        recompute_all_trust_states(seed.db)
        probation = node_probation(seed.db, dialer_identity.fingerprint)
        assert probation is not None and probation.activity_days == 1
    finally:
        dialer.close()
        seed.close()


def test_a_sync_pass_counts_the_seed_it_reached(tmp_path):
    """The dialing side: a completed hello on this node's own sync pass is a
    day of direct interaction with the seed."""
    from netbbs.link.sync import run_link_sync
    from tests.link_sync_wait import run_sync_briefly

    dialer_identity = bootstrap_node_identity("pass-dialer")
    seed_identity = bootstrap_node_identity("pass-seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "pass-dialer")
    seed = _NodeDb(tmp_path, "pass-seed")

    async def scenario():
        server = await _run_server(seed_node, lambda: _hello_for(seed_node), seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(run_link_sync(
                    dialer_node, session, [f"http://127.0.0.1:{server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    enforce_trust_policy=True,
                ))
                await run_sync_briefly(task, settle=0.2)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert _activity_days(dialer.db, TrustSubject.node(seed_identity.fingerprint)) == 1
    finally:
        dialer.close()
        seed.close()
