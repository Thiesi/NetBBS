"""BinkP bounds found in review (#1139): a caller cannot hold a session
open by trickling information lines, a handshake has a deadline, a CRAM
reply past ASCII is refused rather than crashing, and an M_GET racing our
end of batch cannot strand a file."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.ftn import binkp
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import (
    M_ADR,
    M_NUL,
    M_PWD,
    BinkpError,
    SystemInfo,
    _password_matches,
    _Transfer,
    encode_frame,
    run_session,
)
from tests.test_ftn_binkp import _pair

HUB = FtnAddress(21, 1, 100)
SYSTEM = SystemInfo(name="Hub", sysop="op")


def _answer(timeout=5):
    async def answer(reader, writer):
        return await run_session(reader, writer, originating=False, our_addresses=[HUB], system=SYSTEM,
                                 password_for=lambda addresses: "SECRET", timeout=timeout)
    return answer


def test_a_caller_trickling_information_lines_is_cut_off(monkeypatch):
    monkeypatch.setattr(binkp, "MAX_INFO_FRAMES", 10)

    async def chatter(reader, writer):
        for _ in range(50):
            writer.write(encode_frame(M_NUL, b"TIME still here"))
            await writer.drain()
            await asyncio.sleep(0.01)

    answered, _ = asyncio.run(_pair(_answer(), chatter))
    assert isinstance(answered, BinkpError) and "information lines" in str(answered)


def test_a_handshake_that_never_finishes_is_cut_off(monkeypatch):
    monkeypatch.setattr(binkp, "HANDSHAKE_SECONDS", 0.5)

    async def slow(reader, writer):
        for _ in range(20):  # keeps the idle timeout fresh, never sends M_ADR
            writer.write(encode_frame(M_NUL, b"VER slow"))
            await writer.drain()
            await asyncio.sleep(0.1)

    answered, _ = asyncio.run(_pair(_answer(), slow))
    assert isinstance(answered, BinkpError) and "handshake took over" in str(answered)


def test_a_wait_cut_short_by_the_handshake_deadline_reports_that_deadline(monkeypatch):
    # Issue #1454: the event loop runs a timer up to a clock tick early, so
    # the read the handshake's deadline cut short could time out a moment
    # before that deadline, and the session was reported idle "for 0
    # seconds". Here the wait times out at once, as early as it can.
    monkeypatch.setattr(binkp, "HANDSHAKE_SECONDS", 1.0)
    real_read_frame = binkp.read_frame

    async def early(reader, timeout=binkp.IDLE_TIMEOUT):
        if timeout < 5:  # shortened to what is left of the handshake
            raise BinkpError(f"no data from the remote for {timeout:.0f} seconds") from TimeoutError()
        return await real_read_frame(reader, timeout)

    monkeypatch.setattr(binkp, "read_frame", early)

    async def silent(reader, writer):
        await asyncio.sleep(2)

    answered, _ = asyncio.run(_pair(_answer(), silent))
    assert isinstance(answered, BinkpError) and "handshake took over" in str(answered)


def test_a_session_has_a_deadline(monkeypatch):
    monkeypatch.setattr(binkp, "SESSION_SECONDS", 0.5)

    async def stalls(reader, writer):
        writer.write(encode_frame(M_ADR, b"21:1/199"))
        await writer.drain()
        await asyncio.sleep(3)

    answered, _ = asyncio.run(_pair(_answer(), stalls))
    assert isinstance(answered, BinkpError) and "ran over" in str(answered)


def test_a_cram_reply_past_ascii_is_a_mismatch_not_a_crash():
    assert _password_matches("CRAM-MD5-\xe9\xe9", "SECRET", "00112233445566778899aabbccddeeff") is False

    async def caller(reader, writer):
        writer.write(encode_frame(M_ADR, b"21:1/199") + encode_frame(M_PWD, b"CRAM-MD5-\xff\xfe"))
        await writer.drain()
        await asyncio.sleep(1)

    answered, _ = asyncio.run(_pair(_answer(), caller))
    assert isinstance(answered, BinkpError) and "did not match" in str(answered)


def test_an_m_get_once_end_of_batch_has_begun_queues_nothing():
    state = _Transfer([binkp.OutgoingFile("a.pkt", b"data")])
    state.to_send.clear()
    state.eob_started = True
    state.resend("a.pkt 4 0 0")
    assert state.to_send == []
