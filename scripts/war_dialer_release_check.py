"""Rehearse an installed wheel with disposable state; never starts a BBS service."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-root', type=Path, required=True,
                        help='repository-local pip --target directory containing the wheel install')
    args = parser.parse_args()
    installed = args.installed_root.resolve()
    sys.path.insert(0, str(installed))
    import netbbs
    from netbbs.auth.users import create_user
    from netbbs.config import set_config
    from netbbs.doors import create_door, get_door_by_name
    from netbbs.doors.bundled import available_bundled_doors, war_dialer as wd
    from netbbs.doors.runtime import run_door, war_dialer_world_path
    from netbbs.net.session import Session, SessionClosedError
    from netbbs.rendering.reflow import print_wrapped
    from netbbs.storage.database import Database
    from netbbs.storage.execution import DatabaseLane

    assert Path(netbbs.__file__).resolve().is_relative_to(installed), 'Imported checkout instead of wheel'
    catalog = [(item, path) for item, path in available_bundled_doors() if item.key == 'war_dialer']
    assert len(catalog) == 1, 'Installed gallery is missing War Dialer'
    item, script = catalog[0]
    assert script.resolve().is_relative_to(installed)
    assert Path(wd.__file__).resolve() == script.resolve()

    class RehearsalSession(Session):
        def __init__(self, mode):
            self.mode = mode
            self.written = bytearray()
            self.pending = asyncio.Queue()
            self.ready = False

        async def write(self, text):
            await self.write_raw(text.encode())

        async def write_raw(self, data):
            self.written.extend(data)
            if not self.ready and b'SWITCHBOARD' in self.written:
                self.ready = True
                if self.mode == 'quit':
                    self.pending.put_nowait(ord('q'))
                elif self.mode == 'disconnect':
                    self.pending.put_nowait(None)

        async def read_byte(self):
            value = await self.pending.get()
            if value is None:
                raise SessionClosedError('rehearsal caller disconnected')
            return value

        async def read_line(self, echo=True):
            raise NotImplementedError

        async def read_key(self, echo=True):
            raise NotImplementedError

        async def read_editor_key(self):
            raise NotImplementedError

        async def close(self):
            self.pending.put_nowait(None)

    # Ignore a developer's world override: this check owns only temporary state.
    previous = os.environ.pop('WAR_DIALER_DB_PATH', None)
    try:
        with tempfile.TemporaryDirectory(prefix='war-dialer-wheel-') as directory:
            db = Database(Path(directory) / 'node.db')
            lane = None
            try:
                player = create_user(db, 'WheelCaller', password='temporary-rehearsal', user_level=255)
                set_config(db, 'war_dialer_owner', 'a' * 32)
                door = create_door(db, item.name, sys.executable, args=(script.as_posix(),),
                                   description=item.description, creator=player)
                assert get_door_by_name(db, item.name) == door
                world = war_dialer_world_path(db, door)
                conn = wd.connect(world)
                try:
                    wd.ensure_schema(conn)
                    wd.bind_world_owner(conn, 'a' * 32)
                    now = wd.now_utc()
                    wd.get_or_create_season_anchor(conn, now)
                    wd.ensure_exchanges_seeded(conn, 1, now)
                    wd.load_or_create_player(conn, player.id, player.username, now, 1)
                finally:
                    conn.close()
                lane = DatabaseLane(db.path)

                async def exercise():
                    for mode, expected in (('quit', 'exited'), ('timeout', 'timed_out'),
                                           ('disconnect', 'caller_disconnected')):
                        session = RehearsalSession(mode)
                        result = await run_door(session, lane, door, player,
                                                wall_time_limit_seconds=3 if mode == 'timeout' else 10)
                        assert session.ready, (mode, result)
                        assert result.reason == expected, (mode, result)
                        assert not result.diagnostic, (mode, result)
                        if mode == 'quit':
                            assert result.exit_code == 0
                        print_wrapped(f'Installed War Dialer {mode}: {result.reason}')
                asyncio.run(exercise())
                with wd.world_session(world, maintenance=True):
                    pass  # No child/session guard survives any exit mode.
            finally:
                if lane is not None:
                    lane.close()
                db.close()
    finally:
        if previous is not None:
            os.environ['WAR_DIALER_DB_PATH'] = previous
    print_wrapped('Installed gallery, registration and supervised exits passed. Live transports and target hosts remain manual checks.')


if __name__ == '__main__':
    main()
