"""
`python -m netbbs [db_path]` — minimal runnable entry point.

Not the final node-startup story: no config file, no node identity
loading, no daemonization, no rc.d integration. This exists so Phase 1
work can actually be connected to and manually tested over a real Telnet
session, rather than only exercised through pytest. Those production
concerns belong with later Phase 1/connectivity work, once there's an
actual daemon lifecycle to design around.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from netbbs.chat import ChatHub
from netbbs.net.login_flow import handle_session
from netbbs.net.telnet import TelnetServer
from netbbs.storage.database import Database

# Port 2323, not the standard Telnet port 23: binding port 23 requires
# root/CAP_NET_BIND_SERVICE on POSIX systems, which is more privilege
# than a manual-test entry point should need or want. A real deployment
# would either run this behind a privilege-dropping wrapper, use a
# reverse proxy / port-forward rule (e.g. via the same kind of Apache
# setup already used elsewhere), or an inetd-style super-server — a
# decision for actual node-startup work, not this script.
DEFAULT_PORT = 2323
DEFAULT_HOST = "0.0.0.0"


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("netbbs.db")
    db = Database(db_path)

    # One hub for the whole node's lifetime, shared across every
    # connected session — this is what makes cross-session real-time
    # chat possible at all (see netbbs.chat.hub.ChatHub).
    hub = ChatHub()

    async def session_handler(session):
        await handle_session(session, db, hub)

    server = TelnetServer(host=DEFAULT_HOST, port=DEFAULT_PORT, session_handler=session_handler)
    await server.start()
    logging.info("NetBBS listening on %s:%d (database: %s)", DEFAULT_HOST, DEFAULT_PORT, db_path)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
