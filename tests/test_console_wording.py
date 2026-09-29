"""First-day SysOp console wording (issue #845, the 2026-09-28 field test).

Each test names the finding it covers.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import pathlib
import struct

import aiohttp
import nacl.signing
import pytest

from netbbs.admin.__main__ import _bootstrap_first_sysop
from netbbs.auth.users import SYSOP_LEVEL, create_user, list_ssh_keys
from netbbs.net.ssh_key_screen import manage_ssh_keys_screen
from netbbs.net.web import WebServer
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _raw_key() -> bytes:
    return bytes(nacl.signing.SigningKey.generate().verify_key)


def _openssh_line(raw: bytes, comment: str = "kai@laptop") -> str:
    def _string(data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + data

    blob = _string(b"ssh-ed25519") + _string(raw)
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


# -- F011: first-account setup ------------------------------------------------


def test_choosing_password_is_not_asked_whether_to_skip_it(db, lane):
    session = FakeSession(["margo", "p", "hunter2", "hunter2", "n", "n"])
    asyncio.run(_bootstrap_first_sysop(session, lane))
    text = _visible(_written_text(session))
    assert "Password: " in text
    assert "skip" not in text


def test_choosing_a_key_is_not_asked_whether_to_skip_it(db, lane):
    session = FakeSession(["margo", "k", base64.b64encode(_raw_key()).decode(), "n", "n"])
    asyncio.run(_bootstrap_first_sysop(session, lane))
    assert "skip" not in _visible(_written_text(session))


# -- F109: adding an SSH key --------------------------------------------------


def _key_owner(db):
    return create_user(db, "kai", password="hunter2")


def test_add_key_asks_for_the_key_before_its_label(db, lane):
    owner = _key_owner(db)
    raw = _raw_key()
    session = FakeSession(["a", _openssh_line(raw), "laptop", "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, owner, changed_by=owner))
    text = _visible(_written_text(session))
    assert text.index("Public key") < text.index("Label for this key")
    keys = list_ssh_keys(db, owner)
    assert [key.label for key in keys] == ["laptop"]


def test_a_key_pasted_as_the_label_is_refused(db, lane):
    owner = _key_owner(db)
    line = _openssh_line(_raw_key())
    session = FakeSession(["a", line, line, "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, owner, changed_by=owner))
    assert "looks like a key, not a label" in _visible(_written_text(session))
    assert list_ssh_keys(db, owner) == []


def test_a_label_merely_starting_with_ssh_is_accepted(db, lane):
    owner = _key_owner(db)
    session = FakeSession(["a", _openssh_line(_raw_key()), "ssh-laptop", "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, owner, changed_by=owner))
    assert [key.label for key in list_ssh_keys(db, owner)] == ["ssh-laptop"]


def test_escape_at_the_label_adds_nothing(db, lane):
    from netbbs.net.char_input import InputCancelled

    class _EscAtLabel(FakeSession):
        async def read_line(self, echo=True, history=None, completer=None, **kwargs):
            if kwargs.get("cancellable") and kwargs.get("initial") == "kai@laptop":
                self._inputs.pop(0)
                raise InputCancelled()
            return await super().read_line(echo, history, completer, **kwargs)

    owner = _key_owner(db)
    session = _EscAtLabel(["a", _openssh_line(_raw_key()), "ESC", "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, owner, changed_by=owner))
    assert list_ssh_keys(db, owner) == []


def test_key_list_puts_the_fingerprint_on_its_own_line(db, lane):
    owner = _key_owner(db)
    session = FakeSession(["a", _openssh_line(_raw_key()), "laptop", "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, owner, changed_by=owner))
    lines = _visible(_written_text(session)).splitlines()
    label_line = next(line for line in lines if "1. laptop" in line)
    assert "added" not in label_line
    assert "added" in lines[lines.index(label_line) + 1]


# -- F129 / F024: no developer references on screen ---------------------------

_SCREEN_MODULES = (
    "netbbs/net/admin_flow.py",
    "netbbs/net/nodeconfig.py",
    "netbbs/attestation.py",
    "netbbs/auth/users.py",
)


def _non_docstring_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


@pytest.mark.parametrize("module", _SCREEN_MODULES)
def test_screen_text_names_no_design_doc_sections_or_issues(module):
    import netbbs

    path = pathlib.Path(netbbs.__file__).parent / module.removeprefix("netbbs/")
    offenders = [
        (line, text[:80])
        for line, text in _non_docstring_strings(path)
        if "design doc" in text or "issue #" in text or "(issues #" in text
    ]
    assert offenders == []


# -- F102: the web server does not advertise its stack ------------------------


async def _idle_handler(session) -> None:
    return None


def test_web_server_names_itself_without_versions():
    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=_idle_handler)
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{server.port}/") as response:
                    return response.headers.get("Server")
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == "NetBBS"


def test_plain_get_on_the_websocket_path_gets_one_plain_sentence():
    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=_idle_handler)
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{server.port}/ws") as response:
                    return response.status, await response.text(), response.headers.get("Server")
        finally:
            await server.stop()

    status, body, server_header = asyncio.run(scenario())
    assert status == 400
    assert body == "This address is for the NetBBS browser terminal."
    assert server_header == "NetBBS"
