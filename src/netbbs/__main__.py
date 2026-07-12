"""
`python -m netbbs [db_path]` — minimal runnable entry point.

Not the final node-startup story: no config file, no node identity
loading, no daemonization, no rc.d integration. This exists so Phase 1
work can actually be connected to and manually tested over a real Telnet
or SSH session, rather than only exercised through pytest. Those
production concerns belong with later Phase 1/connectivity work, once
there's an actual daemon lifecycle to design around.
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
# decision for actual node-startup work, not this script. Same reasoning
# for SSH's port (2222, not 22) and the web server's (8080, an
# unprivileged default rather than 80).
DEFAULT_TELNET_PORT = 2323
DEFAULT_SSH_PORT = 2222
DEFAULT_WEB_PORT = 8080
DEFAULT_HOST = "0.0.0.0"


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("netbbs.db")
    db = Database(db_path)

    # One hub for the whole node's lifetime, shared across every
    # connected session, regardless of which transport it arrived
    # through — this is what makes cross-session real-time chat possible
    # at all (see netbbs.chat.hub.ChatHub).
    hub = ChatHub()

    async def session_handler(session):
        await handle_session(session, db, hub)

    telnet_server = TelnetServer(
        host=DEFAULT_HOST, port=DEFAULT_TELNET_PORT, session_handler=session_handler
    )
    await telnet_server.start()
    logging.info(
        "NetBBS listening on %s:%d (Telnet, database: %s)",
        DEFAULT_HOST,
        DEFAULT_TELNET_PORT,
        db_path,
    )

    servers = [telnet_server]

    # SSH is an optional extra (design doc round 22: asyncssh pulls in
    # `cryptography`, deliberately not a core dependency — see
    # pyproject.toml's `ssh` extra) — a Telnet-only node should still
    # start normally without it installed, just without an SSH listener.
    try:
        from netbbs.net.ssh import SSHServer
    except ImportError:
        logging.info("asyncssh not installed — skipping SSH listener (pip install netbbs[ssh])")
    else:
        ssh_server = SSHServer(
            host=DEFAULT_HOST, port=DEFAULT_SSH_PORT, db=db, session_handler=session_handler
        )
        await ssh_server.start()
        logging.info("NetBBS listening on %s:%d (SSH)", DEFAULT_HOST, DEFAULT_SSH_PORT)
        servers.append(ssh_server)

    # Web is likewise an optional extra (design doc round 22/25's `web`
    # extra) — aiohttp isn't needed at all for a Telnet/SSH-only node.
    try:
        from netbbs.net.web import WebServer
    except ImportError:
        logging.info("aiohttp not installed — skipping web listener (pip install netbbs[web])")
    else:
        web_server = WebServer(host=DEFAULT_HOST, port=DEFAULT_WEB_PORT, session_handler=session_handler)
        await web_server.start()
        logging.info("NetBBS listening on %s:%d (web)", DEFAULT_HOST, DEFAULT_WEB_PORT)
        servers.append(web_server)

    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
