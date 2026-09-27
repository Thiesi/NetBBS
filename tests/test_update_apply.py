"""Issue #731: installing a newer release from Settings -> Update."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
from importlib import metadata
from pathlib import Path

import pytest

from netbbs import update_apply
from netbbs.net import admin_flow
from netbbs.net.admin_flow import admin_menu
from netbbs.selfupdate import ReleaseInfo, get_last_check_summary, save_release_cache
from netbbs.update_apply import (
    MAX_WHEEL_BYTES,
    RESTART_EXIT_CODE,
    ApplyError,
    InstallEnvironment,
    ReleaseWheel,
    download_wheel,
    fetch_release_wheel,
    get_recorded_install,
    get_restart_mode,
    inspect_install_environment,
    installed_extras,
    pip_command,
    reconcile_install_at_startup,
    record_install,
    release_wheel_from_json,
    restart_exit_requested,
    restarts_after_install,
    run_bounded,
    run_restart_shutdown,
    set_restart_mode,
)
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _node_controls,
    _normalized_visible,
    _visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)


@pytest.fixture(autouse=True)
def _no_restart_request_leaks():
    update_apply.cancel_restart_exit()
    yield
    update_apply.cancel_restart_exit()


WHEEL_BYTES = b"PK\x03\x04 not really a wheel, but bytes with a known digest" * 10
WHEEL_SHA = hashlib.sha256(WHEEL_BYTES).hexdigest()


def _release_json(tag="v7.99.0", **asset_overrides) -> bytes:
    version = tag.lstrip("v")
    asset = {
        "name": f"netbbs-{version}-py3-none-any.whl",
        "size": len(WHEEL_BYTES),
        "digest": f"sha256:{WHEEL_SHA}",
        "browser_download_url": f"https://github.com/Thiesi/NetBBS/releases/download/{tag}/netbbs-{version}-py3-none-any.whl",
    }
    asset.update(asset_overrides)
    return json.dumps({
        "tag_name": tag, "draft": False, "prerelease": False,
        "assets": [asset, {"name": f"netbbs-{version}.tar.gz", "size": 10, "digest": "sha256:" + "0" * 64,
                           "browser_download_url": "https://github.com/x"}],
    }).encode()


def _wheel(**overrides) -> ReleaseWheel:
    values = dict(
        tag="v7.99.0", version="7.99.0", name="netbbs-7.99.0-py3-none-any.whl",
        url="https://github.com/Thiesi/NetBBS/releases/download/v7.99.0/netbbs-7.99.0-py3-none-any.whl",
        size=len(WHEEL_BYTES), sha256=WHEEL_SHA,
    )
    values.update(overrides)
    return ReleaseWheel(**values)


class _Response:
    def __init__(self, body: bytes, url: str = "https://objects.githubusercontent.com/x"):
        self._body = io.BytesIO(body)
        self._url = url

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- the release and its wheel -----------------------------------------------


def test_release_wheel_is_picked_with_its_published_digest():
    wheel = release_wheel_from_json("v7.99.0", _release_json())
    assert wheel.name == "netbbs-7.99.0-py3-none-any.whl"
    assert wheel.sha256 == WHEEL_SHA and wheel.size == len(WHEEL_BYTES)


@pytest.mark.parametrize("overrides, fragment", [
    ({"digest": None}, "publishes no SHA-256 digest"),
    ({"digest": "md5:abcd"}, "publishes no SHA-256 digest"),
    ({"browser_download_url": "https://evil.example/netbbs.whl"}, "unexpected address"),
    ({"browser_download_url": "http://github.com/Thiesi/NetBBS/releases/download/v7.99.0/x.whl"}, "unexpected address"),
    ({"size": MAX_WHEEL_BYTES + 1}, "implausible size"),
    ({"name": "something-else.whl"}, "has no netbbs-7.99.0-py3-none-any.whl"),
])
def test_release_wheel_refusals(overrides, fragment):
    with pytest.raises(ApplyError, match=fragment):
        release_wheel_from_json("v7.99.0", _release_json(**overrides))


def test_draft_or_mismatched_release_is_refused():
    draft = json.loads(_release_json())
    draft["draft"] = True
    with pytest.raises(ApplyError, match="draft or pre-release"):
        release_wheel_from_json("v7.99.0", json.dumps(draft).encode())
    with pytest.raises(ApplyError, match="did not return release"):
        release_wheel_from_json("v8.0.0", _release_json())


def test_only_plain_version_tags_are_fetched():
    calls = []
    with pytest.raises(ApplyError, match="not a release tag"):
        fetch_release_wheel("v1.0/../../evil", fetch=lambda url, token: calls.append(url) or b"")
    assert calls == []


def test_fetch_uses_the_tag_endpoint_and_token():
    seen = []

    def fetch(url, token):
        seen.append((url, token))
        return _release_json()

    fetch_release_wheel("v7.99.0", token="tok", fetch=fetch)
    assert seen == [("https://api.github.com/repos/Thiesi/NetBBS/releases/tags/v7.99.0", "tok")]


# -- download -----------------------------------------------------------------


def test_download_verifies_and_keeps_the_wheel(tmp_path):
    path = download_wheel(_wheel(), tmp_path, open_url=lambda request, timeout: _Response(WHEEL_BYTES))
    assert path.read_bytes() == WHEEL_BYTES
    assert not (tmp_path / (path.name + ".part")).exists()


def test_download_with_wrong_digest_leaves_nothing(tmp_path):
    with pytest.raises(ApplyError, match="does not match"):
        download_wheel(_wheel(sha256="0" * 64), tmp_path, open_url=lambda request, timeout: _Response(WHEEL_BYTES))
    assert list(tmp_path.iterdir()) == []


def test_download_larger_than_announced_is_cut_off(tmp_path):
    with pytest.raises(ApplyError, match="larger than"):
        download_wheel(_wheel(size=10), tmp_path, open_url=lambda request, timeout: _Response(WHEEL_BYTES))
    assert list(tmp_path.iterdir()) == []


def test_short_download_is_refused(tmp_path):
    with pytest.raises(ApplyError, match="ended after"):
        download_wheel(_wheel(size=len(WHEEL_BYTES) + 5), tmp_path,
                       open_url=lambda request, timeout: _Response(WHEEL_BYTES))


def test_redirect_off_https_is_refused(tmp_path):
    with pytest.raises(ApplyError, match="off HTTPS"):
        download_wheel(_wheel(), tmp_path,
                       open_url=lambda request, timeout: _Response(WHEEL_BYTES, url="http://objects.example/x"))
    assert list(tmp_path.iterdir()) == []


def test_slow_download_times_out(tmp_path):
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    with pytest.raises(ApplyError, match="longer than"):
        download_wheel(_wheel(), tmp_path, open_url=lambda request, timeout: _Response(WHEEL_BYTES),
                       clock=lambda: next(ticks), timeout_seconds=10)


def test_local_io_failures_are_apply_errors(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ApplyError, match="could not create"):
        download_wheel(_wheel(), blocker / "updates", open_url=lambda request, timeout: _Response(WHEEL_BYTES))


def test_network_failure_is_an_apply_error(tmp_path):
    def broken(request, timeout):
        raise OSError("connection reset")

    with pytest.raises(ApplyError, match="download failed"):
        download_wheel(_wheel(), tmp_path, open_url=broken)


# -- the installation ------------------------------------------------------------


class _FakeDist:
    def __init__(self, requires, *, direct_url=None, version="7.11.2", root="/venv/lib/site-packages"):
        self.requires = requires
        self._direct_url = direct_url
        self.version = version
        self._root = root

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None

    def locate_file(self, name):
        return Path(self._root) / name


def test_installed_extras_are_those_fully_present(monkeypatch):
    installed = {"pynacl", "asyncssh", "aiohttp"}

    def distribution(name):
        if name.lower() in installed:
            return object()
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(update_apply.metadata, "distribution", distribution)
    dist = _FakeDist([
        "pynacl>=1.5",
        'pytest>=7.0; extra == "dev"',
        'asyncssh>=2.24; extra == "dev"',
        'tzdata; (sys_platform == "win32") and extra == "tzdata"',
        'asyncssh>=2.24; extra == "ssh"',
        'aiohttp>=3.9; extra == "web"',
    ])
    assert installed_extras(dist) == ("ssh", "web")


def _loaded(dist):
    return dict(loaded_from=Path(dist.locate_file("netbbs")).resolve(), loaded_version=dist.version)


def test_install_environment_refusals():
    ok_dist = _FakeDist([])
    with pytest.raises(ApplyError, match="not running from a virtual environment"):
        inspect_install_environment(prefix="/usr", base_prefix="/usr", distribution=lambda n: ok_dist)
    editable = _FakeDist([], direct_url=json.dumps({"url": "file:///src", "dir_info": {"editable": True}}))
    with pytest.raises(ApplyError, match="development checkout"):
        inspect_install_environment(prefix="/venv", base_prefix="/usr", distribution=lambda n: editable)
    with pytest.raises(ApplyError, match="cannot write"):
        inspect_install_environment(prefix="/venv", base_prefix="/usr", distribution=lambda n: ok_dist,
                                    writable=lambda path: False, **_loaded(ok_dist))


def test_install_environment_refuses_metadata_for_another_copy():
    """A checkout on PYTHONPATH beside an installed copy: installing would
    replace the dormant copy and change nothing that runs (Codex review)."""
    dist = _FakeDist([])
    with pytest.raises(ApplyError, match="installing would not change what runs"):
        inspect_install_environment(prefix="/venv", base_prefix="/usr", distribution=lambda n: dist,
                                    writable=lambda path: True, loaded_from=Path("/src/checkout/netbbs").resolve(),
                                    loaded_version=dist.version)
    with pytest.raises(ApplyError, match="installing would not change what runs"):
        inspect_install_environment(prefix="/venv", base_prefix="/usr", distribution=lambda n: dist,
                                    writable=lambda path: True, loaded_from=_loaded(dist)["loaded_from"],
                                    loaded_version="7.0.0")

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    with pytest.raises(ApplyError, match="not installed as a package"):
        inspect_install_environment(prefix="/venv", base_prefix="/usr", distribution=missing)


def test_install_environment_accepts_a_plain_venv_install():
    dist = _FakeDist([], direct_url=json.dumps({"url": "file:///tmp/netbbs.whl", "archive_info": {}}))
    env = inspect_install_environment(
        prefix="/venv", base_prefix="/usr", executable="/venv/bin/python",
        distribution=lambda n: dist, writable=lambda path: True, **_loaded(dist),
    )
    assert env.python == "/venv/bin/python" and env.version == "7.11.2"


def test_pip_command_carries_the_extras(tmp_path):
    env = InstallEnvironment(python="/venv/bin/python", site_packages=tmp_path, version="7.11.2", extras=("ssh", "web"))
    command = pip_command(env, tmp_path / "netbbs-7.99.0-py3-none-any.whl")
    assert command[:4] == ["/venv/bin/python", "-m", "pip", "install"]
    assert command[-1].endswith("netbbs-7.99.0-py3-none-any.whl[ssh,web]")
    assert "--no-input" in command
    bare = pip_command(InstallEnvironment("/p", tmp_path, "1", ()), tmp_path / "w.whl")
    assert bare[-1].endswith("w.whl")


def test_run_bounded_keeps_only_the_tail():
    script = "for i in range(2000): print('line %04d' % i)"
    status, log = asyncio.run(run_bounded([sys.executable, "-c", script], timeout_seconds=60, max_chars=200))
    assert status == 0
    assert log.splitlines()[-1] == "line 1999"
    assert len(log) <= 200


def test_run_bounded_kills_a_process_that_overruns():
    async def scenario():
        with pytest.raises(ApplyError, match="did not finish"):
            await run_bounded([sys.executable, "-c", "import time; time.sleep(60)"], timeout_seconds=0.5)

    asyncio.run(scenario())


# -- restarting --------------------------------------------------------------


def test_restart_mode_declaration_wins_over_detection(db):
    assert get_restart_mode(db) == "auto"
    assert restarts_after_install(db, {"INVOCATION_ID": "abc"})
    assert not restarts_after_install(db, {})
    set_restart_mode(db, "no")
    assert not restarts_after_install(db, {"INVOCATION_ID": "abc"})
    set_restart_mode(db, "yes")
    assert restarts_after_install(db, {})
    with pytest.raises(ValueError):
        set_restart_mode(db, "sometimes")


def test_restart_exit_is_requested_only_while_the_restart_shutdown_holds():
    async def scenario():
        async def finishes():
            return None

        await run_restart_shutdown(finishes)
        assert restart_exit_requested()

        update_apply.cancel_restart_exit()
        gate = asyncio.Event()

        async def waits():
            await gate.wait()

        task = asyncio.create_task(run_restart_shutdown(waits))
        await asyncio.sleep(0)
        assert restart_exit_requested()
        task.cancel()  # a SysOp cancel, or SIGTERM replacing it
        await asyncio.gather(task, return_exceptions=True)
        assert not restart_exit_requested()

    asyncio.run(scenario())


def test_a_failed_restart_shutdown_withdraws_the_request():
    """A later ordinary SIGTERM must not exit 75 and be restarted (Codex review)."""
    async def boom():
        raise RuntimeError("broadcast failed")

    with pytest.raises(RuntimeError):
        asyncio.run(run_restart_shutdown(boom))
    assert not restart_exit_requested()


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_run_bounded_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "child-alive"
    child = f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
    script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"

    async def scenario():
        with pytest.raises(ApplyError):
            await run_bounded([sys.executable, "-c", script], timeout_seconds=0.5)
        await asyncio.sleep(2.5)

    asyncio.run(scenario())
    assert not marker.exists()


def test_main_exits_with_the_restart_status_after_a_restart_shutdown(monkeypatch, tmp_path):
    import netbbs.__main__ as node_main
    from netbbs.net.nodeconfig import load_config

    config = load_config(["--db", str(tmp_path / "node.db")])
    monkeypatch.setattr(node_main, "load_config", lambda argv: config)
    monkeypatch.setattr(node_main, "_install_signal_handlers", lambda *a, **k: None)
    monkeypatch.setattr(node_main, "_create_log_file_handler", lambda path: __import__("logging").NullHandler())

    async def fake_run(config, **kwargs):
        update_apply.request_restart_exit()

    monkeypatch.setattr(node_main, "run", fake_run)
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(node_main.main())
    assert exc_info.value.code == RESTART_EXIT_CODE

    async def plain_run(config, **kwargs):
        return None

    update_apply.cancel_restart_exit()
    monkeypatch.setattr(node_main, "run", plain_run)
    asyncio.run(node_main.main())  # an ordinary stop exits normally


def test_startup_reports_whether_the_install_came_back(db):
    assert reconcile_install_at_startup(db, "7.99.0") is None

    record_install(db, from_version="7.11.2", to_version="v7.99.0", restarting=True)
    assert reconcile_install_at_startup(db, "7.99.0") == "installed v7.99.0 and restarted into it"
    assert get_recorded_install(db) is None
    assert get_last_check_summary(db)[1] == "installed v7.99.0 and restarted into it"

    record_install(db, from_version="7.11.2", to_version="v7.99.0", restarting=True)
    outcome = reconcile_install_at_startup(db, "7.11.2")
    assert "started as 7.11.2" in outcome
    assert get_recorded_install(db) is None


# -- the Update screen ---------------------------------------------------------------


def _cache_newer_release(db, tag="v99.0.0"):
    save_release_cache(db, None, ReleaseInfo(tag_name=tag, tarball_url="https://x", published_at="2026-09-27T00:00:00Z"))


def _environment(tmp_path) -> InstallEnvironment:
    return InstallEnvironment(python="/venv/bin/python", site_packages=tmp_path, version="7.11.2", extras=("web",))


def _install_fakes(monkeypatch, tmp_path, *, pip_status=0, pip_log="Successfully installed netbbs-99.0.0",
                   reported_version="99.0.0"):
    calls = []
    monkeypatch.setattr(admin_flow, "inspect_install_environment", lambda: _environment(tmp_path))
    monkeypatch.setattr(admin_flow, "fetch_release_wheel",
                        lambda tag, token=None: calls.append(("fetch", tag)) or _wheel(
                            tag=tag, version=tag.lstrip("v"), name=f"netbbs-{tag.lstrip('v')}-py3-none-any.whl"))

    def fake_download(wheel, directory):
        calls.append(("download", wheel.tag))
        path = Path(directory) / wheel.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(WHEEL_BYTES)
        return path

    monkeypatch.setattr(admin_flow, "download_wheel", fake_download)

    async def fake_backup(*, db_path, identity_dir, destination):
        calls.append(("backup", destination))
        return destination

    monkeypatch.setattr(admin_flow, "_create_live_backup_owned", fake_backup)

    async def fake_run(command, *, timeout_seconds, **kwargs):
        if command[1:4] == ["-m", "pip", "install"]:
            calls.append(("pip", command[-1]))
            return pip_status, pip_log
        calls.append(("version", command[0]))
        return 0, reported_version + "\n"

    monkeypatch.setattr(admin_flow, "run_bounded", fake_run)
    return calls


def test_install_key_needs_a_newer_release_and_a_live_node(db, lane, sysop):
    _cache_newer_release(db)

    standalone = FakeSession(["s", "u", "b", "b", "b"])
    asyncio.run(admin_menu(standalone, lane, sysop))
    text = _normalized_visible(_written_text(standalone))
    assert "[I]nstall" not in text
    assert "Installing runs from the live node" in text

    live = FakeSession(["s", "u", "b", "b", "b"])
    asyncio.run(admin_menu(live, lane, sysop, node_controls=_node_controls()))
    assert "[I]nstall v99.0.0" in _visible(_written_text(live))


def test_restart_key_cycles_and_persists(db, lane, sysop):
    session = FakeSession(["s", "u", "r", "r", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert get_restart_mode(db) == "no"
    assert "stop after installing; you restart the service" in _normalized_visible(_written_text(session))


def test_install_refusal_is_explained_and_changes_nothing(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)

    def refuse():
        raise ApplyError("This NetBBS runs from a development checkout (an editable install); update the checkout instead.")

    monkeypatch.setattr(admin_flow, "inspect_install_environment", refuse)
    session = FakeSession(["s", "u", "i", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path)))

    text = _normalized_visible(_written_text(session))
    assert "not possible here" in text and "development checkout" in text
    assert get_recorded_install(db) is None


def test_install_without_restart_says_to_restart_the_service(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    set_restart_mode(db, "no")
    calls = _install_fakes(monkeypatch, tmp_path)
    controls = _node_controls(backup_identity_dir=tmp_path)

    session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=controls))

    assert [c[0] for c in calls] == ["fetch", "download", "backup", "pip", "version"]
    assert calls[3][1].endswith("netbbs-99.0.0-py3-none-any.whl[web]")
    text = _normalized_visible(_written_text(session))
    assert "Installed v99.0.0. Restart the service to run it" in text
    assert not controls.shutdown_scheduler.is_scheduled()
    assert get_recorded_install(db) == {"from": __import__("netbbs").__version__, "to": "v99.0.0", "restarting": False}
    assert "restart the service to run it" in get_last_check_summary(db)[1]


def test_install_with_restart_shuts_down_and_requests_the_restart_status(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    set_restart_mode(db, "yes")
    _install_fakes(monkeypatch, tmp_path)

    async def scenario():
        controls = _node_controls(backup_identity_dir=tmp_path)
        controls = type(controls)(**{**controls.__dict__, "graceful_delay_seconds": 0.01})
        session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b"])
        await admin_menu(session, lane, sysop, node_controls=controls)
        await asyncio.wait_for(controls.shutdown_event.wait(), timeout=10)
        return session, controls

    session, controls = asyncio.run(scenario())
    assert restart_exit_requested()
    assert controls.maintenance.is_active()
    assert "The node goes down in 0s" in _normalized_visible(_written_text(session))
    assert get_recorded_install(db)["restarting"] is True


def test_declining_the_final_question_installs_nothing(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    calls = _install_fakes(monkeypatch, tmp_path)
    session = FakeSession(["s", "u", "i", "i", "n", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path)))
    assert calls == []
    assert "Cancelled -- nothing was installed." in _normalized_visible(_written_text(session))


def test_failed_pip_shows_its_output_and_records_the_failure(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    set_restart_mode(db, "yes")
    _install_fakes(monkeypatch, tmp_path, pip_status=1, pip_log="Collecting x\nERROR: No matching distribution for aiohttp>=99")
    controls = _node_controls(backup_identity_dir=tmp_path)

    session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=controls))

    text = _normalized_visible(_written_text(session))
    assert "Failed at: install" in text
    assert "No matching distribution for aiohttp>=99" in text
    assert not controls.shutdown_scheduler.is_scheduled()
    assert get_recorded_install(db) is None
    assert get_last_check_summary(db)[1].startswith("install of v99.0.0 failed (install)")


def test_environment_reporting_another_version_counts_as_failure(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    _install_fakes(monkeypatch, tmp_path, reported_version="7.11.2")
    session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path)))
    assert "the environment reports 7.11.2" in _normalized_visible(_written_text(session))
    assert "Failed at: install" in _normalized_visible(_written_text(session))
    assert get_last_check_summary(db)[1].startswith("install of v99.0.0 failed (install)")


def test_version_mismatch_after_pip_keeps_the_restart_warning(db, lane, sysop, monkeypatch, tmp_path):
    """pip returned 0, so the environment changed: the screen must keep
    saying so rather than show an ordinary failure (Codex review)."""
    _cache_newer_release(db)
    _install_fakes(monkeypatch, tmp_path, reported_version="7.11.2")
    session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path)))
    recorded = get_recorded_install(db)
    assert recorded is not None and "restart the service or roll back" in recorded["note"]

    again = FakeSession(["s", "u", "b", "b", "b"])
    asyncio.run(admin_menu(again, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path)))
    assert "restart the service or roll back by hand" in _normalized_visible(_written_text(again))


def test_cancelled_restart_brings_the_restart_warning_back(db, lane, sysop):
    record_install(db, from_version="7.11.2", to_version="v99.0.0", restarting=True)
    session = FakeSession(["s", "u", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls()))
    assert "v99.0.0 -- restart the service to run it" in _normalized_visible(_written_text(session))


def test_install_is_refused_while_a_shutdown_is_scheduled(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    calls = _install_fakes(monkeypatch, tmp_path)

    async def scenario():
        controls = _node_controls(backup_identity_dir=tmp_path)
        pending = asyncio.create_task(asyncio.sleep(60))
        controls.shutdown_scheduler.schedule(pending, deadline=asyncio.get_running_loop().time() + 60, message=None)
        session = FakeSession(["s", "u", "i", "i", "b", "b", "b"])
        try:
            await admin_menu(session, lane, sysop, node_controls=controls)
        finally:
            controls.shutdown_scheduler.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        return session

    session = asyncio.run(scenario())
    assert calls == []
    assert "A shutdown is already scheduled; cancel it first." in _normalized_visible(_written_text(session))


def test_second_concurrent_install_is_refused(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    calls = _install_fakes(monkeypatch, tmp_path)

    async def scenario():
        await admin_flow._INSTALL_IN_PROGRESS.acquire()
        try:
            session = FakeSession(["s", "u", "i", "i", "b", "b", "b"])
            await admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path))
            return session
        finally:
            admin_flow._INSTALL_IN_PROGRESS.release()

    session = asyncio.run(scenario())
    assert calls == []
    assert "Another SysOp is installing a release right now" in _normalized_visible(_written_text(session))


def test_standalone_check_does_not_advertise_install(db, lane, sysop, monkeypatch):
    async def fake_check(*, known_etag=None, known_release=None, token=None, fetch=None):
        return ReleaseInfo(tag_name="v99.0.0", tarball_url="https://x", published_at="2026-09-27T00:00:00Z"), None

    monkeypatch.setattr(admin_flow, "check_latest_release", fake_check)
    session = FakeSession(["s", "u", "c", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _normalized_visible(_written_text(session))
    assert "[I]nstall v99.0.0 installs it." not in text
    assert "Install it from the live node's Settings -> Update" in text


def test_cancelling_the_session_mid_pip_lets_the_install_finish_and_record(db, lane, sysop, monkeypatch, tmp_path):
    """Who, or a shutdown's disconnect_all, cancelling the SysOp's session
    must not kill pip or skip the record (Codex review)."""
    _cache_newer_release(db)
    set_restart_mode(db, "yes")
    calls = _install_fakes(monkeypatch, tmp_path)
    pip_started = None
    pip_release = None

    async def slow_run(command, *, timeout_seconds, **kwargs):
        if command[1:4] == ["-m", "pip", "install"]:
            calls.append(("pip", command[-1]))
            pip_started.set()
            await pip_release.wait()
            return 0, "ok"
        calls.append(("version", command[0]))
        return 0, "99.0.0\n"

    monkeypatch.setattr(admin_flow, "run_bounded", slow_run)

    async def scenario():
        nonlocal pip_started, pip_release
        pip_started, pip_release = asyncio.Event(), asyncio.Event()
        controls = _node_controls(backup_identity_dir=tmp_path)
        session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b"])
        menu = asyncio.create_task(admin_menu(session, lane, sysop, node_controls=controls))
        await asyncio.wait_for(pip_started.wait(), timeout=10)
        menu.cancel()
        await asyncio.sleep(0.05)
        assert not menu.done()  # still waiting for the install to finish
        pip_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(menu, timeout=10)
        return controls

    controls = asyncio.run(scenario())
    assert [c[0] for c in calls] == ["fetch", "download", "backup", "pip", "version"]
    recorded = get_recorded_install(db)
    assert recorded is not None and recorded["to"] == "v99.0.0"
    assert not controls.shutdown_scheduler.is_scheduled()


def test_a_shutdown_scheduled_during_the_install_is_not_replaced(db, lane, sysop, monkeypatch, tmp_path):
    """A SIGTERM's stop that arrives while pip runs must stay a stop: the
    restart is armed only if nothing else holds the shutdown (Claude review)."""
    _cache_newer_release(db)
    set_restart_mode(db, "yes")
    _install_fakes(monkeypatch, tmp_path)
    stop_task = None

    async def run_then_sigterm(command, *, timeout_seconds, **kwargs):
        nonlocal stop_task
        if command[1:4] == ["-m", "pip", "install"]:
            stop_task = asyncio.create_task(asyncio.sleep(60))
            controls.shutdown_scheduler.schedule(
                stop_task, deadline=asyncio.get_running_loop().time() + 60, message=None,
                source="sigterm", cancellable=False,
            )
            return 0, "ok"
        return 0, "99.0.0\n"

    monkeypatch.setattr(admin_flow, "run_bounded", run_then_sigterm)
    controls = _node_controls(backup_identity_dir=tmp_path)

    async def scenario():
        session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b"])
        try:
            await admin_menu(session, lane, sysop, node_controls=controls)
            assert controls.shutdown_scheduler.source() == "sigterm"
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        return session

    session = asyncio.run(scenario())
    assert not restart_exit_requested()
    assert "The node is already shutting down" in _normalized_visible(_written_text(session))
    assert get_recorded_install(db)["restarting"] is False


def test_failed_download_stops_before_the_backup(db, lane, sysop, monkeypatch, tmp_path):
    _cache_newer_release(db)
    calls = _install_fakes(monkeypatch, tmp_path)

    def bad_download(wheel, directory):
        raise ApplyError("the downloaded wheel does not match the release's SHA-256 digest; nothing was installed")

    monkeypatch.setattr(admin_flow, "download_wheel", bad_download)
    session = FakeSession(["s", "u", "i", "i", "y", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=tmp_path)))
    assert [c[0] for c in calls] == ["fetch"]
    assert "Failed at: download" in _normalized_visible(_written_text(session))
