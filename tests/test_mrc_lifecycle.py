"""
`netbbs.__main__.run()` owns the node's `MrcBridge` (issue #275): it
must start the hub connection only when a SysOp enabled MRC in the
database, and drain it cleanly on shutdown -- LOGOFF/SHUTDOWN reach
the hub, no task is left behind.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.channels import create_channel
from netbbs.mrc.settings import MrcSettings, save_mrc_settings, set_mrc_room
from netbbs.storage.database import Database
from tests.mrc_fake_hub import FakeMrcHub
from tests.test_main_lifecycle import _config


def _seed(config, *, hub_port: int | None) -> None:
    db = Database(config.db_path)
    try:
        sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
        lobby = create_channel(db, "lobby", creator=sysop)
        set_mrc_room(db, lobby, "lobby")
        if hub_port is not None:
            save_mrc_settings(db, MrcSettings(
                enabled=True, host="127.0.0.1", port=hub_port, tls=False, site_name="Lifecycle Node",
            ))
    finally:
        db.close()


def test_run_connects_to_the_hub_when_enabled_and_drains_on_shutdown(tmp_path):
    from netbbs.__main__ import run

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        try:
            config = _config(tmp_path, seed_sysop=False)
            _seed(config, hub_port=fake.port)
            shutdown_event = asyncio.Event()
            task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
            await fake.wait_for(lambda p: p.body.startswith("IMALIVE:"), timeout=5)
            assert fake.handshakes[0].startswith("Lifecycle Node~NetBBS_")
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=15)
            assert fake.packets(body_prefix="SHUTDOWN")
            assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mrc-")]
        finally:
            await fake.close()

    asyncio.run(scenario())


def test_run_never_dials_when_mrc_is_disabled(tmp_path):
    from netbbs.__main__ import run

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        try:
            config = _config(tmp_path, seed_sysop=False)
            _seed(config, hub_port=None)
            shutdown_event = asyncio.Event()
            task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
            await asyncio.sleep(0.5)
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=15)
            assert fake.connections == 0
        finally:
            await fake.close()

    asyncio.run(scenario())


def test_mrc_warnings_reach_the_diagnostic_log_without_link(tmp_path):
    """Review of #275: the diagnostic log handler used to be attached
    only when Link was enabled, so an MRC-only node persisted none of
    the bridge's hub warnings."""
    import sqlite3

    from netbbs.__main__ import run

    async def scenario():
        config = _config(tmp_path, seed_sysop=False)
        _seed(config, hub_port=1)  # nothing listens: every dial fails with a real connection error
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        row = None
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while row is None:
                assert asyncio.get_event_loop().time() < deadline, "no MRC diagnostic row appeared"
                conn = sqlite3.connect(str(config.db_path))
                try:
                    rows = conn.execute("SELECT level, logger_name, message FROM link_diagnostic_log").fetchall()
                finally:
                    conn.close()
                row = next((r for r in rows if r[1] == "netbbs.mrc"), None)
                if row is None:
                    await asyncio.sleep(0.05)
        finally:
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=15)
        assert row[0] == "WARNING"
        assert "MRC hub 127.0.0.1:1" in row[2]

    asyncio.run(scenario())
