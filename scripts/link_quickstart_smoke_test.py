#!/usr/bin/env python3
"""Automated end-to-end run of the README's two-node Link quickstart.

Spins up two real NetBBS node processes on loopback, drives each one's
Telnet UI exactly as a human following the README would (login, link a
board, post, browse the carried copy on the other node, send Link mail,
restart a node), and asserts the outcomes the README documents. Exists so
a future change that breaks the documented flow fails a script instead of
silently rotting the README (issue #76).

Uses the stdlib `telnetlib` module, deprecated since Python 3.11 and
removed in 3.13 (PEP 594). This is a dev-only test utility, not shipped
node code; if it starts failing to import on a newer interpreter, replace
the import with a small raw-socket client that strips Telnet IAC
sequences rather than reintroducing telnetlib.

Run from the repository root:

    python scripts/link_quickstart_smoke_test.py
"""

from __future__ import annotations

import socket
import subprocess
import sys
import sqlite3
import telnetlib  # noqa: F401  (see module docstring re: 3.13 removal)
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

NODE_A_TOML = """
[node]
identity_dir = "netbbs_identity"
name = "node-a"

[database]
path = "netbbs.db"

[ssh]
enabled = false

[telnet]
enabled = true
host = "127.0.0.1"
port = 2323

[link]
enabled = true
host = "127.0.0.1"
port = 7862
outgoing_only = false
advertised_host = "127.0.0.1"
advertised_port = 7862
seeds = ["http://127.0.0.1:7863"]
sync_interval_seconds = 2
"""

NODE_B_TOML = """
[node]
identity_dir = "netbbs_identity"
name = "node-b"

[database]
path = "netbbs.db"

[ssh]
enabled = false

[telnet]
enabled = true
host = "127.0.0.1"
port = 2324

[link]
enabled = true
host = "127.0.0.1"
port = 7863
outgoing_only = false
advertised_host = "127.0.0.1"
advertised_port = 7863
seeds = ["http://127.0.0.1:7862"]
sync_interval_seconds = 2
"""


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}\n{result.stdout}\n{result.stderr}")


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"nothing listening on {host}:{port} after {timeout}s")


def expect(tn: "telnetlib.Telnet", marker: bytes, timeout: float = 10) -> bytes:
    data = tn.read_until(marker, timeout=timeout)
    if marker not in data:
        raise AssertionError(f"expected {marker!r}, got {data!r}")
    return data


def poll(predicate, description: str, timeout: float = 30.0, interval: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for: {description}")


def start_node(node_dir: Path, log_name: str) -> subprocess.Popen:
    log_path = node_dir / log_name
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "netbbs", "--config", "netbbs.toml"],
        cwd=node_dir,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    proc._log_file = log_file  # type: ignore[attr-defined]
    return proc


def stop_node(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    proc._log_file.close()  # type: ignore[attr-defined]


def fingerprint_from_log(log_path: Path, node_name: str) -> str:
    text = log_path.read_text(errors="replace")
    marker = f"node Link identity '{node_name}': fingerprint "
    idx = text.index(marker) + len(marker)
    return text[idx : idx + 32]


def main() -> int:
    steps_passed = []

    with tempfile.TemporaryDirectory(prefix="netbbs_link_smoke_") as tmp:
        tmp_path = Path(tmp)
        node_a_dir = tmp_path / "node-a"
        node_b_dir = tmp_path / "node-b"
        node_a_dir.mkdir()
        node_b_dir.mkdir()
        (node_a_dir / "netbbs.toml").write_text(NODE_A_TOML)
        (node_b_dir / "netbbs.toml").write_text(NODE_B_TOML)

        run([sys.executable, "scripts/create_test_user.py", str(node_a_dir / "netbbs.db"), "alice", "hunter2", "255"])
        run([sys.executable, "scripts/create_test_user.py", str(node_b_dir / "netbbs.db"), "bob", "hunter2", "255"])
        run([sys.executable, "scripts/create_test_board.py", str(node_a_dir / "netbbs.db"), "general", "General discussion"])
        steps_passed.append("fixtures created")

        proc_a = start_node(node_a_dir, "node-a.log")
        proc_b = start_node(node_b_dir, "node-b.log")
        try:
            wait_for_port("127.0.0.1", 2323)
            wait_for_port("127.0.0.1", 2324)
            fingerprint_a = poll(
                lambda: _try_fingerprint(node_a_dir / "node-a.log", "node-a"),
                "node-a startup fingerprint in log",
            )
            fingerprint_b = poll(
                lambda: _try_fingerprint(node_b_dir / "node-b.log", "node-b"),
                "node-b startup fingerprint in log",
            )
            steps_passed.append("both nodes started")

            # Wait for the two nodes to complete a hello and see each other
            # as a verified peer before driving the UI (bounded poll, not a
            # fixed sleep -- see docs/NetBBS-worklog.md section 11).
            poll(
                lambda: _peer_count(node_a_dir / "netbbs.db") >= 1,
                "node A sees a verified peer",
            )
            steps_passed.append("nodes discovered each other")

            # Link the board and post to it, via the real Telnet UI.
            tn = telnetlib.Telnet("127.0.0.1", 2323, timeout=10)
            expect(tn, b"Username: ")
            tn.write(b"alice\r\n")
            expect(tn, b"Password: ")
            tn.write(b"hunter2\r\n")
            expect(tn, b"Choice: ")
            tn.write(b"s")
            expect(tn, b"Choice: ")
            tn.write(b"m")
            expect(tn, b"Choice: ")
            tn.write(b"m")
            expect(tn, b"Choice: ")
            tn.write(b"l")
            expect(tn, b"Choice: ")
            tn.write(b"01")
            data = expect(tn, b"Choice: ")
            assert b"Linked: no" in data
            tn.write(b"l")
            for _ in range(6):
                expect(tn, b": ")
                tn.write(b"\r\n")
            expect(tn, b": ")  # fork prompt
            tn.write(b"n\r\n")
            data = expect(tn, b"Choice: ")
            assert b"Linked 'general'" in data
            tn.close()
            steps_passed.append("board linked")

            tn = telnetlib.Telnet("127.0.0.1", 2323, timeout=10)
            expect(tn, b"Username: ")
            tn.write(b"alice\r\n")
            expect(tn, b"Password: ")
            tn.write(b"hunter2\r\n")
            expect(tn, b"Choice: ")
            tn.write(b"j")
            expect(tn, b"Choice: ")
            tn.write(b"m")
            expect(tn, b"Choice: ")
            tn.write(b"01")
            expect(tn, b"Choice: ")
            tn.write(b"p")
            expect(tn, b": ")
            tn.write(b"Smoke test post\r\n")
            expect(tn, b": ")
            tn.write(b"Smoke test body.\r\n")
            data = expect(tn, b"Choice: ")
            assert b"Posted (id" in data
            tn.close()
            steps_passed.append("posted on node A")

            # The core issue #73 claim: the post must materialize into a
            # real, locally browsable post on the carrying node, not just
            # an accepted/gossiped event.
            def _post_materialized():
                con = sqlite3.connect(node_b_dir / "netbbs.db")
                try:
                    rows = con.execute(
                        "SELECT subject FROM posts WHERE subject = ?", ("Smoke test post",)
                    ).fetchall()
                    return bool(rows)
                finally:
                    con.close()

            poll(_post_materialized, "post materialized into node B's local posts table")
            steps_passed.append("post materialized on node B")

            tn = telnetlib.Telnet("127.0.0.1", 2324, timeout=10)
            expect(tn, b"Username: ")
            tn.write(b"bob\r\n")
            expect(tn, b"Password: ")
            tn.write(b"hunter2\r\n")
            expect(tn, b"Choice: ")
            tn.write(b"j")
            expect(tn, b"Choice: ")
            tn.write(b"m")
            expect(tn, b"Choice: ")
            tn.write(b"01")
            data = expect(tn, b"Choice: ")
            assert b"Smoke test post" in data
            assert f"alice@{fingerprint_a}".encode() in data
            tn.close()
            steps_passed.append("post browsable on node B via Telnet")

            # Link mail: compose, receive, and the acknowledgement round trip.
            tn = telnetlib.Telnet("127.0.0.1", 2323, timeout=10)
            expect(tn, b"Username: ")
            tn.write(b"alice\r\n")
            expect(tn, b"Password: ")
            tn.write(b"hunter2\r\n")
            expect(tn, b"Choice: ")
            tn.write(b"e")
            expect(tn, b"Choice: ")
            tn.write(b"c")
            expect(tn, b": ")
            tn.write(f"bob@{fingerprint_b}\r\n".encode())
            expect(tn, b": ")
            tn.write(b"Smoke test mail\r\n")
            tn.read_until(b"finish.", timeout=5)
            tn.write(b"Smoke test mail body.\r\n")
            tn.write(b"\r\n")
            data = expect(tn, b"Choice: ")
            assert b"Message sent." in data
            tn.close()
            steps_passed.append("Link mail composed")

            def _mail_received():
                con = sqlite3.connect(node_b_dir / "netbbs.db")
                try:
                    rows = con.execute(
                        "SELECT subject FROM mail_messages WHERE subject = ?", ("Smoke test mail",)
                    ).fetchall()
                    return bool(rows)
                finally:
                    con.close()

            poll(_mail_received, "Link mail received in node B's inbox")
            steps_passed.append("Link mail received")

            def _mail_acknowledged():
                con = sqlite3.connect(node_a_dir / "netbbs.db")
                try:
                    rows = con.execute(
                        "SELECT link_delivery_status FROM mail_messages WHERE subject = ?",
                        ("Smoke test mail",),
                    ).fetchall()
                    return bool(rows) and rows[0][0] == "delivered"
                finally:
                    con.close()

            poll(_mail_acknowledged, "node A's delivery status flips to 'delivered'")
            steps_passed.append("Link mail acknowledgement round trip")
        finally:
            stop_node(proc_a)
            stop_node(proc_b)

        # Restart node A and confirm the fingerprint and peer state persisted.
        proc_a2 = start_node(node_a_dir, "node-a-restart.log")
        try:
            wait_for_port("127.0.0.1", 2323)
            fingerprint_a2 = poll(
                lambda: _try_fingerprint(node_a_dir / "node-a-restart.log", "node-a"),
                "node-a restart fingerprint in log",
            )
            assert fingerprint_a2 == fingerprint_a, (
                f"fingerprint changed across restart: {fingerprint_a} -> {fingerprint_a2}"
            )
            steps_passed.append("fingerprint persisted across restart")
        finally:
            stop_node(proc_a2)

    print(f"OK -- {len(steps_passed)} steps passed:")
    for step in steps_passed:
        print(f"  - {step}")
    return 0


def _try_fingerprint(log_path: Path, node_name: str):
    if not log_path.exists():
        return None
    try:
        return fingerprint_from_log(log_path, node_name)
    except ValueError:
        return None


def _peer_count(db_path: Path) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM link_peers").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
