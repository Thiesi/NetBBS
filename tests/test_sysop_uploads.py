"""Issue #728: a SysOp sends banner art and door files from inside NetBBS."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

aiohttp = pytest.importorskip("aiohttp")

from netbbs.auth.users import SYSOP_LEVEL, create_user, set_user_level  # noqa: E402
from netbbs.doors.registry import custom_doors_dir  # noqa: E402
from netbbs.moderation.log import list_recent_actions  # noqa: E402
from netbbs.net import admin_flow, zmodem  # noqa: E402
from netbbs.net.file_transfer import TransferGrants  # noqa: E402
from netbbs.net.welcome_banner import MAX_BANNER_SIZE_BYTES, banner_path, welcome_banner_status  # noqa: E402
from netbbs.sysop_uploads import (  # noqa: E402
    BANNER,
    DOOR_FILE,
    SysOpUploadError,
    SysOpUploadTarget,
    door_filename_error,
    install_upload,
)
from tests.test_admin_flow import (  # noqa: E402,F401 -- fixtures
    FakeSession,
    _announced_text,
    _visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)
from tests.test_file_flow_upload_integration import _BytePipe, _ClientSession, _ServerSession  # noqa: E402
from tests.test_file_transfer_http import _Node  # noqa: E402


def _target(tmp_path, name="art.ans", *, max_bytes=1024, kind=BANNER, replaces=False):
    return SysOpUploadTarget(
        kind=kind, destination=tmp_path / "dest" / name, max_bytes=max_bytes,
        label="the test piece", audit_action="upload_test", replaces=replaces,
    )


def _staged(tmp_path, data: bytes):
    source = tmp_path / "staged.bin"
    source.write_bytes(data)
    return source


# -- domain ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", ".hidden", "..", ".", "a/b", "a\\b", "x" * 101, "bad\x1bname",
                                  " padded", "c:evil"])
def test_door_filename_refuses_anything_but_a_plain_name(name):
    assert door_filename_error(name) is not None


@pytest.mark.parametrize("name", ["door.py", "My Game 2.lua", "run-me_v1.sh", "ünïcode.py"])
def test_door_filename_accepts_plain_names(name):
    assert door_filename_error(name) is None


def test_install_writes_the_destination_and_consumes_the_staged_file(tmp_path):
    target = _target(tmp_path)
    source = _staged(tmp_path, b"\x1b[31mart")

    assert install_upload(target, source) == len(b"\x1b[31mart")

    assert target.destination.read_bytes() == b"\x1b[31mart"
    assert not source.exists()
    assert [p.name for p in target.destination.parent.iterdir()] == ["art.ans"]  # no partial left


def test_install_refuses_an_oversize_file_and_leaves_the_old_one(tmp_path):
    target = _target(tmp_path, max_bytes=10, replaces=True)
    target.destination.parent.mkdir()
    target.destination.write_bytes(b"old")
    source = _staged(tmp_path, b"x" * 11)

    with pytest.raises(SysOpUploadError, match="over the 10 byte limit"):
        install_upload(target, source)

    assert target.destination.read_bytes() == b"old"
    assert not source.exists()


def test_install_does_not_replace_a_file_nobody_agreed_to_replace(tmp_path):
    """Consent is given against the destination as it was; a file that
    appeared since (another session's upload) is left alone."""
    target = _target(tmp_path, replaces=False)
    target.destination.parent.mkdir()
    target.destination.write_bytes(b"someone else's")

    with pytest.raises(SysOpUploadError, match="appeared since"):
        install_upload(target, _staged(tmp_path, b"mine"))

    assert target.destination.read_bytes() == b"someone else's"


@pytest.mark.skipif(os.name != "posix", reason="execute bits are a POSIX notion")
def test_replacing_keeps_the_old_files_permission_bits(tmp_path):
    target = _target(tmp_path, "door.sh", replaces=True)
    target.destination.parent.mkdir()
    target.destination.write_bytes(b"#!/bin/sh\necho old\n")
    os.chmod(target.destination, 0o750)

    install_upload(target, _staged(tmp_path, b"#!/bin/sh\necho new\n"))

    assert target.destination.read_bytes().endswith(b"new\n")
    assert target.destination.stat().st_mode & 0o777 == 0o750


def test_a_failing_cleanup_does_not_turn_a_landed_upload_into_a_failure(tmp_path, monkeypatch):
    import netbbs.sysop_uploads as module

    target = _target(tmp_path)
    source = _staged(tmp_path, b"ART")
    real_unlink = Path.unlink

    def _stubborn(self, *args, **kwargs):
        if self == source:
            raise PermissionError("held open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "unlink", _stubborn)

    assert install_upload(target, source) == 3
    assert target.destination.read_bytes() == b"ART"


def test_install_refuses_an_empty_file(tmp_path):
    with pytest.raises(SysOpUploadError, match="No file"):
        install_upload(_target(tmp_path), _staged(tmp_path, b""))


def test_install_refuses_a_directory_at_the_destination(tmp_path):
    target = _target(tmp_path)
    target.destination.mkdir(parents=True)
    with pytest.raises(SysOpUploadError, match="not a file"):
        install_upload(target, _staged(tmp_path, b"art"))


def test_install_does_not_write_through_a_symlink(tmp_path):
    target = _target(tmp_path)
    target.destination.parent.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    try:
        os.symlink(outside, target.destination)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symlinks")
    with pytest.raises(SysOpUploadError, match="symbolic link"):
        install_upload(target, _staged(tmp_path, b"art"))
    assert outside.read_bytes() == b"keep"


# -- HTTP: a SysOp grant redeemed by a real browser-shaped POST ----------------


def _post_file(url: str, name: str, data: bytes):
    form = aiohttp.FormData()
    form.add_field("file", data, filename=name, content_type="application/octet-stream")
    return form


@pytest.fixture
def node(tmp_path):
    running = _Node(tmp_path)
    yield running
    running.close()


def test_a_sysop_link_writes_the_chosen_destination_and_audits_it(node, tmp_path):
    sysop_user = create_user(node.db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    target = _target(tmp_path)

    async def scenario():
        async with node:
            grant = node.grants.issue_sysop_upload(user=sysop_user, target=target)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.get(url) as page:  # opening the link serves the form, spends nothing
                    assert page.status == 200 and "<form" in await page.text()
                async with client.post(url, data=_post_file(url, "../../etc/whatever.txt", b"ART")) as first:
                    body = await first.json()
                async with client.post(url, data=_post_file(url, "again.ans", b"AGAIN")) as second:
                    return body, second.status

    body, second_status = asyncio.run(scenario())

    assert body["filename"] == "art.ans" and body["size_bytes"] == 3
    assert target.destination.read_bytes() == b"ART"  # the sent name was ignored
    assert second_status == 404  # single use
    audit = [a for a in list_recent_actions(node.db) if a.action == "upload_test"]
    assert len(audit) == 1 and "3 bytes" in audit[0].detail and str(target.destination) in audit[0].detail


def test_a_sysop_link_stops_working_once_the_account_is_no_longer_sysop(node, tmp_path):
    sysop_user = create_user(node.db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    other = create_user(node.db, "other", password="hunter2", user_level=SYSOP_LEVEL)  # keeps a SysOp on the node
    target = _target(tmp_path)

    async def scenario():
        async with node:
            grant = node.grants.issue_sysop_upload(user=sysop_user, target=target)
            set_user_level(node.db, sysop_user, 10, changed_by=other)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.post(url, data=_post_file(url, "a.ans", b"ART")) as response:
                    return response.status

    assert asyncio.run(scenario()) == 403
    assert not target.destination.exists()


def test_a_sysop_link_refuses_more_than_the_target_allows(node, tmp_path):
    sysop_user = create_user(node.db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    target = _target(tmp_path, max_bytes=16)

    async def scenario():
        async with node:
            grant = node.grants.issue_sysop_upload(user=sysop_user, target=target)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.post(url, data=_post_file(url, "a.ans", b"x" * 100)) as response:
                    return response.status

    assert asyncio.run(scenario()) == 413
    assert not target.destination.exists()


# -- console screens -----------------------------------------------------------


def _browser_console(session, *, base_url="https://bbs.example.org") -> TransferGrants:
    session.supports_zmodem = False
    grants = TransferGrants(base_url=base_url)
    admin_flow._console_transfers[session] = grants
    return grants


def test_banner_upload_offers_a_link_for_exactly_that_piece(db, lane, sysop):
    session = FakeSession([])
    grants = _browser_console(session)

    asyncio.run(admin_flow._upload_banner_piece(
        session, lane, sysop, path_of=banner_path, label="the welcome banner",
        audit_action="upload_welcome_banner",
    ))

    announced = _announced_text(session)
    assert "https://bbs.example.org/transfer/" in announced
    assert f"saved as {banner_path(db)}" in " ".join(announced.split())
    (grant,) = grants._grants.values()
    assert grant.sysop_upload.destination == banner_path(db)
    assert grant.sysop_upload.max_bytes == MAX_BANNER_SIZE_BYTES
    assert not welcome_banner_status(db).enabled  # an upload never enables


def test_banner_upload_asks_before_replacing_and_no_means_no_link(db, lane, sysop):
    banner_path(db).write_bytes(b"current art")
    session = FakeSession(["n"])
    grants = _browser_console(session)

    asyncio.run(admin_flow._upload_banner_piece(
        session, lane, sysop, path_of=banner_path, label="the welcome banner",
        audit_action="upload_welcome_banner",
    ))

    assert "Replace the current" in _visible(_written_text(session))
    assert "Nothing uploaded" in _announced_text(session)
    assert len(grants._grants) == 0
    assert banner_path(db).read_bytes() == b"current art"


def test_upload_without_zmodem_or_a_web_listener_says_why(db, lane, sysop):
    session = FakeSession([])
    session.supports_zmodem = False
    admin_flow._console_transfers.pop(session, None)

    asyncio.run(admin_flow._upload_banner_piece(
        session, lane, sysop, path_of=banner_path, label="the welcome banner",
        audit_action="upload_welcome_banner",
    ))

    text = " ".join(_announced_text(session).split())
    assert "cannot carry a Zmodem transfer" in text and "web listener" in text


def test_no_public_url_mints_nothing_and_promises_nothing(db, lane, sysop):
    session = FakeSession([])
    grants = _browser_console(session, base_url=None)

    asyncio.run(admin_flow._upload_banner_piece(
        session, lane, sysop, path_of=banner_path, label="the welcome banner",
        audit_action="upload_welcome_banner",
    ))

    text = " ".join(_announced_text(session).split())
    assert "public_url" in text
    assert "saved as" not in text
    assert len(grants._grants) == 0


def test_no_public_url_is_refused_before_a_name_is_asked(db, lane, sysop):
    """A console that cannot be handed a link says so first, rather than
    after asking for a filename and a replacement it could never act on."""
    session = FakeSession([])  # no scripted input: any prompt would fail the test
    _browser_console(session, base_url=None)

    asyncio.run(admin_flow._upload_door_file_screen(session, lane, sysop))

    assert "public_url" in " ".join(_announced_text(session).split())


def test_a_session_torn_down_mid_install_still_audits_what_landed(db, lane, sysop, monkeypatch, tmp_path):
    """A running copy cannot be cancelled; the audit record of a file that
    lands anyway must not be skipped with the session."""
    import threading

    import netbbs.net.file_transfer as module

    release = threading.Event()
    started = threading.Event()
    real_install = module.install_upload

    def _slow_install(target, source):
        started.set()
        release.wait(5)
        return real_install(target, source)

    monkeypatch.setattr(module, "install_upload", _slow_install)
    target = _target(tmp_path)
    source = _staged(tmp_path, b"ART")

    async def scenario():
        task = asyncio.create_task(module.install_and_record(lane, sysop, target, source, sent_as="a.ans"))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert target.destination.read_bytes() == b"ART"
    assert [a.action for a in list_recent_actions(db)].count("upload_test") == 1


def test_banner_upload_over_zmodem_lands_audited_and_not_enabled(db, lane, sysop):
    payload = b"\x1b[1;33mNetBBS\x1b[0m\r\n" * 40
    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server = _ServerSession([], read_pipe=client_to_server, write_pipe=server_to_client)
    client = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(admin_flow._upload_banner_piece(
            server, lane, sysop, path_of=banner_path, label="the welcome banner",
            audit_action="upload_welcome_banner",
        ))
        client_task = asyncio.create_task(zmodem.send_file(client, "whatever-name.ans", payload))
        await asyncio.wait_for(server_task, timeout=10)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass

    asyncio.run(scenario())

    assert banner_path(db).read_bytes() == payload
    assert not welcome_banner_status(db).enabled
    audit = [a for a in list_recent_actions(db) if a.action == "upload_welcome_banner"]
    assert len(audit) == 1 and "'whatever-name.ans'" in audit[0].detail
    from netbbs.net.notices import pending_notices

    assert any("Uploaded" in line for line in pending_notices(server))


def test_door_upload_names_the_file_first(db, lane, sysop):
    session = FakeSession(["mygame.py"])
    grants = _browser_console(session)

    asyncio.run(admin_flow._upload_door_file_screen(session, lane, sysop))

    (grant,) = grants._grants.values()
    assert grant.sysop_upload.kind == DOOR_FILE
    assert grant.sysop_upload.destination == custom_doors_dir(db) / "mygame.py"
    assert "[F]rom disk" in _announced_text(session)


def test_door_upload_refuses_a_path_as_a_name(db, lane, sysop):
    session = FakeSession(["../escape.py"])
    grants = _browser_console(session)

    asyncio.run(admin_flow._upload_door_file_screen(session, lane, sysop))

    assert len(grants._grants) == 0
    assert "plain file name" in _announced_text(session)


def test_door_upload_confirms_replacing_an_existing_file(db, lane, sysop):
    directory = custom_doors_dir(db)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mygame.py").write_text("old", encoding="utf-8")
    session = FakeSession(["mygame.py", "y"])
    grants = _browser_console(session)

    asyncio.run(admin_flow._upload_door_file_screen(session, lane, sysop))

    assert "Replace the existing mygame.py" in _visible(_written_text(session))
    assert len(grants._grants) == 1


def test_menus_offer_upload(db, lane, sysop):
    session = FakeSession([])
    asyncio.run(admin_flow._draw_welcome_banner_menu(session, lane, "off", False, False, False))
    asyncio.run(admin_flow._draw_door_menu(session, "off", False, False, False, status_line=""))
    assert _visible(_written_text(session)).count("[U]pload") == 2


def test_demotion_while_the_body_arrives_stops_the_install(node, tmp_path, monkeypatch):
    """The account is checked again once the body is in: sending it can
    take minutes, and a SysOp locked out meanwhile must not publish."""
    import netbbs.net.file_transfer as module

    sysop_user = create_user(node.db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    other = create_user(node.db, "other", password="hunter2", user_level=SYSOP_LEVEL)
    target = _target(tmp_path)
    real_receive = module._receive_upload

    async def _receive_then_demote(request, temp_path, *, max_bytes):
        result = await real_receive(request, temp_path, max_bytes=max_bytes)
        set_user_level(node.db, sysop_user, 10, changed_by=other)
        return result

    monkeypatch.setattr(module, "_receive_upload", _receive_then_demote)

    async def scenario():
        async with node:
            grant = node.grants.issue_sysop_upload(user=sysop_user, target=target)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.post(url, data=_post_file(url, "a.ans", b"ART")) as response:
                    return response.status

    assert asyncio.run(scenario()) == 403
    assert not target.destination.exists()


def test_a_door_link_follows_a_lowered_upload_limit(db, sysop, tmp_path):
    from netbbs.config import set_max_upload_bytes
    from netbbs.net.file_transfer import resolve

    grants = TransferGrants()
    target = _target(tmp_path, "game.py", max_bytes=1_000_000, kind=DOOR_FILE)
    grant = grants.issue_sysop_upload(user=sysop, target=target)
    set_max_upload_bytes(db, 1000)

    assert resolve(db, grant).max_upload_bytes == 1000


def test_door_from_disk_hides_dot_files(db, lane, sysop):
    """An upload interrupted by a crash leaves a dot-named temporary copy
    beside its destination; it is not offered for registration."""
    directory = custom_doors_dir(db)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".game.py.upload-1a2b3c4d").write_text("half", encoding="utf-8")
    (directory / "game.py").write_text("print('hi')", encoding="utf-8")
    session = FakeSession(["b"])

    asyncio.run(admin_flow._door_filesystem_screen(session, lane, sysop, "off", False, False, False))

    text = _visible(_written_text(session))
    assert "game.py" in text and ".upload-" not in text
