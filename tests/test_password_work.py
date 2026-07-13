"""Regression tests for bounded off-loop Argon2 work."""

from __future__ import annotations

import asyncio
import threading

import pytest

from netbbs.auth import users
from netbbs.auth.users import AuthError, authenticate_password_async, create_user_async
from netbbs.storage.database import Database


def test_async_password_verification_runs_off_event_loop_thread(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fake_verify(password: str, stored_hash: str) -> bool:
        worker_threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(users, "verify_password", fake_verify)

    async def scenario() -> None:
        with pytest.raises(AuthError, match="login failed"):
            await authenticate_password_async(db, "missing", "wrong")

    asyncio.run(scenario())
    assert len(worker_threads) == 1
    assert worker_threads[0] != event_loop_thread
    db.close()


def test_async_account_creation_hashes_off_loop_but_uses_database_on_loop(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "node.db")
    event_loop_thread = threading.get_ident()
    hash_threads: list[int] = []

    def fake_hash(password: str) -> str:
        hash_threads.append(threading.get_ident())
        return "prepared-test-hash"

    monkeypatch.setattr(users, "hash_password", fake_hash)

    async def scenario() -> None:
        user = await create_user_async(db, "alice", password="secret")
        assert user.username == "alice"
        stored = db.connection.execute(
            "SELECT password_hash FROM users WHERE username = ?", ("alice",)
        ).fetchone()
        assert stored["password_hash"] == "prepared-test-hash"

    asyncio.run(scenario())
    assert len(hash_threads) == 1
    assert hash_threads[0] != event_loop_thread
    db.close()


def test_password_work_never_exceeds_configured_concurrency(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    release_workers = threading.Event()
    two_workers_started = threading.Event()
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    monkeypatch.setattr(users, "_MAX_CONCURRENT_PASSWORD_WORK", 2)

    def fake_verify(password: str, stored_hash: str) -> bool:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                two_workers_started.set()
        try:
            assert release_workers.wait(timeout=2)
            return False
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(users, "verify_password", fake_verify)

    async def scenario() -> None:
        tasks = [
            asyncio.create_task(authenticate_password_async(db, f"missing-{index}", "wrong"))
            for index in range(4)
        ]
        assert await asyncio.to_thread(two_workers_started.wait, 1)
        await asyncio.sleep(0.05)
        assert maximum_active == 2
        release_workers.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, AuthError) for result in results)

    asyncio.run(scenario())
    assert maximum_active == 2
    db.close()


def test_cancelled_session_does_not_release_slot_before_worker_finishes(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "node.db")
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    call_count = 0
    state_lock = threading.Lock()

    monkeypatch.setattr(users, "_MAX_CONCURRENT_PASSWORD_WORK", 1)

    def fake_verify(password: str, stored_hash: str) -> bool:
        nonlocal call_count
        with state_lock:
            call_count += 1
            this_call = call_count
        if this_call == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
        return False

    monkeypatch.setattr(users, "verify_password", fake_verify)

    async def scenario() -> None:
        first = asyncio.create_task(authenticate_password_async(db, "first", "wrong"))
        assert await asyncio.to_thread(first_started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(authenticate_password_async(db, "second", "wrong"))
        await asyncio.sleep(0.05)
        assert not second_started.is_set()

        release_first.set()
        with pytest.raises(AuthError, match="login failed"):
            await second
        assert second_started.is_set()

    asyncio.run(scenario())
    db.close()
