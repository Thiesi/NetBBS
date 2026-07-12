from __future__ import annotations

import asyncio

from netbbs.auth.users import AuthError, User
from netbbs.chat import ChatHub
from netbbs.net import login_flow


class FakeSession:
    def __init__(self, lines: list[str]):
        self._lines = iter(lines)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        raise AssertionError("main menu must not be entered")

    @property
    def output(self) -> str:
        return "".join(self.written)


def test_blocked_user_does_not_receive_failed_attempt_message(monkeypatch):
    user = User(
        id=1,
        username="blocked",
        user_level=0,
        fingerprint=None,
        created_at="2026-01-01T00:00:00+00:00",
        last_login_at=None,
    )

    monkeypatch.setattr(login_flow, "authenticate_password", lambda db, username, password: user)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: True)

    async def scenario() -> None:
        session = FakeSession(["blocked", "correct-password"])
        await login_flow.handle_session(session, object(), ChatHub())
        assert "Your access to this system has been revoked." in session.output
        assert "Too many failed attempts" not in session.output
        assert "Welcome, blocked" not in session.output

    asyncio.run(scenario())


def test_exhausted_attempts_receive_failed_attempt_message(monkeypatch):
    def reject(db, username, password):
        raise AuthError("login failed")

    monkeypatch.setattr(login_flow, "authenticate_password", reject)

    async def scenario() -> None:
        session = FakeSession(
            [
                "unknown-1",
                "wrong-1",
                "unknown-2",
                "wrong-2",
                "unknown-3",
                "wrong-3",
            ]
        )
        await login_flow.handle_session(session, object(), ChatHub())
        assert "Too many failed attempts. Goodbye." in session.output
        assert "Your access to this system has been revoked." not in session.output

    asyncio.run(scenario())
