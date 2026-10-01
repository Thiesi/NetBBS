"""A terminal's colour depth from the terminal type it reports (issue #986).

SyncTERM sends no COLORTERM, but CTerm handles direct colour
(`CSI 38;2;R;G;B m`), so a session that reports `syncterm` gets
truecolor. An explicit COLORTERM still decides when a client sends one.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

from netbbs.auth.users import create_user
from netbbs.net.session import Session
from netbbs.net.telnet import IAC, NEW_ENVIRON, NEW_ENVIRON_IS, NEW_ENVIRON_VALUE, NEW_ENVIRON_VAR, SB, SE
from netbbs.net.terminal_detect import terminal_supports_truecolor
from tests.test_ssh import _run_server as _run_ssh_server
from tests.test_ssh import db  # noqa: F401 -- the SSH tests' database fixture
from tests.test_telnet import _run_server as _run_telnet_server
from tests.test_telnet import skip_initial_negotiation


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["syncterm"], True),
        (["SyncTERM"], True),
        (["ansi-bbs"], False),
        (["ansi"], False),
        (["xterm-256color"], False),
        (["dumb"], False),
        ([], False),
        (["something", "syncterm"], True),
        (["xterm", "syncterm"], False),
    ],
)
def test_terminal_supports_truecolor_by_first_recognised_name(names, expected):
    assert terminal_supports_truecolor(names) is expected


def _telnet_scenario(terminal_type: str, colorterm: bytes | None):
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["result"] = (session.supports_truecolor, session.truecolor_diagnostic)

    async def scenario():
        server = await _run_telnet_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader, writer, terminal_type=terminal_type)
            if colorterm is not None:
                body = bytes([NEW_ENVIRON_IS, NEW_ENVIRON_VAR]) + b"COLORTERM"
                if colorterm:
                    body += bytes([NEW_ENVIRON_VALUE]) + colorterm
                writer.write(bytes([IAC, SB, NEW_ENVIRON]) + body + bytes([IAC, SE]))
            writer.write(b"x\r\n")
            await writer.drain()
            try:
                await asyncio.wait_for(reader.read(), timeout=10)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    return captured["result"]


def test_telnet_syncterm_without_colorterm_gets_truecolor():
    supported, diagnostic = _telnet_scenario("syncterm", None)
    assert supported is True
    assert "syncterm" in diagnostic and "truecolor available" in diagnostic


def test_telnet_syncterm_keeps_truecolor_when_new_environ_has_no_colorterm():
    supported, diagnostic = _telnet_scenario("syncterm", b"")
    assert supported is True
    assert "truecolor available" in diagnostic


def test_telnet_explicit_colorterm_wins_over_the_terminal_type():
    assert _telnet_scenario("syncterm", b"256color") == (
        False, "Telnet NEW-ENVIRON reported COLORTERM=256color; using 256-color"
    )


def test_telnet_other_classic_terminals_stay_at_256_colours():
    supported, _diagnostic = _telnet_scenario("ansi-bbs", None)
    assert supported is False


def _ssh_scenario(db, term_type: str, colorterm: str | None):
    create_user(db, "alice", password="hunter2", user_level=10)
    results = []

    async def handler(session: Session):
        results.append((session.supports_truecolor, session.truecolor_diagnostic))

    async def scenario():
        server = await _run_ssh_server(db, handler)
        try:
            env = {"COLORTERM": colorterm} if colorterm is not None else {}
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="alice", password="hunter2", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type=term_type, term_size=(80, 24), encoding=None, env=env):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    return results


def test_ssh_syncterm_without_colorterm_gets_truecolor(db):
    [(supported, diagnostic)] = _ssh_scenario(db, "syncterm", None)
    assert supported is True
    assert "syncterm" in diagnostic and "truecolor available" in diagnostic


def test_ssh_explicit_colorterm_wins_over_the_terminal_type(db):
    assert _ssh_scenario(db, "syncterm", "256color") == [
        (False, "SSH environment reported COLORTERM=256color; using 256-color")
    ]
