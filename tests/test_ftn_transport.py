"""FTN transport (design doc §6.8, issues #1467 and #1468): host and port
parsing, IPv6, and BinkP over TLS -- dialled, pinned, verified and
answered -- on real loopback sockets with a real database lane. The
certificates are self-signed ones made for the test, as hubs mostly use."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import socket
import ssl
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.config import set_config
from netbbs.ftn import listener as listener_module
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import OutgoingFile, SystemInfo, run_session
from netbbs.ftn.listener import FtnListener, listen_settings
from netbbs.ftn.mailer import FtnMailer
from netbbs.ftn.networks import FtnNetwork, FtnNetworkError, get_network, save_network, set_board_area, set_uplink_tls_pin
from netbbs.ftn.nodelist import _binkp_target
from netbbs.ftn.transport import (
    TLS_PINNED,
    TLS_VERIFIED,
    TransportError,
    dial,
    format_host_port,
    normalise_fingerprint,
    parse_host_port,
    parse_listen_hosts,
    show_fingerprint,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_ftn_mailer import _hub_mail

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


def _has_ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback here")


# --- addresses ----------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("hub.example", ("hub.example", None)),
    ("hub.example:24555", ("hub.example", 24555)),
    ("192.0.2.1", ("192.0.2.1", None)),
    ("192.0.2.1:24553", ("192.0.2.1", 24553)),
    ("2001:db8::1", ("2001:db8::1", None)),
    ("[2001:db8::1]", ("2001:db8::1", None)),
    ("[2001:db8::1]:24555", ("2001:db8::1", 24555)),
    ("  my_hub.example  ", ("my_hub.example", None)),
])
def test_hosts_are_read_as_a_sysop_or_a_nodelist_writes_them(text, expected):
    assert parse_host_port(text) == expected


@pytest.mark.parametrize("text", ["[2001:db8::1", "[hub.example]:1", "[2001:db8::1]x", "hub:0", "hub:99999",
                                  "hub:", "2001:db8::zz", "-bad-.example!", ""])
def test_a_host_that_is_not_one_is_refused(text):
    with pytest.raises(ValueError):
        parse_host_port(text)


def test_an_ipv6_host_is_shown_in_brackets():
    assert format_host_port("2001:db8::1", 24554) == "[2001:db8::1]:24554"
    assert format_host_port("hub.example", 24554) == "hub.example:24554"


def test_listen_addresses_are_a_list_of_ip_addresses():
    assert parse_listen_hosts("") == ()
    assert parse_listen_hosts("192.0.2.7, [2001:DB8::7]  ::1 192.0.2.7") == ("192.0.2.7", "2001:db8::7", "::1")
    with pytest.raises(ValueError, match="not an IP address"):
        parse_listen_hosts("bbs.example")


@pytest.mark.parametrize("flags, expected", [
    (["IBN:[2001:db8::1]:24555"], ("2001:db8::1", 24555)),
    (["IBN:[2001:db8::1]"], ("2001:db8::1", 24554)),
    (["INA:[2001:db8::2]", "IBN"], ("2001:db8::2", 24554)),
    (["INA:[2001:db8::2]", "IBN:24600"], ("2001:db8::2", 24600)),
    (["INA:192.0.2.123", "INA:[2001:db8::2]", "IBN"], ("192.0.2.123", 24554)),
    (["IBN:hub.example:24555"], ("hub.example", 24555)),
    (["IBN:[2001:db8::1"], (None, None)),
])
def test_nodelist_flags_carry_ipv6_addresses(flags, expected):
    assert _binkp_target(flags) == expected


def test_fingerprints_are_stored_bare_and_shown_in_pairs():
    pin = normalise_fingerprint("AB:" * 31 + "CD")
    assert pin == "ab" * 31 + "cd"
    assert show_fingerprint(pin).startswith("AB:AB:") and show_fingerprint(pin).endswith(":CD")
    assert normalise_fingerprint("") == ""
    with pytest.raises(ValueError):
        normalise_fingerprint("abc")


# --- network records ------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _network(db, **changes):
    values = dict(name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB,
                  uplink_host="127.0.0.1", session_password="SECRET", enabled=True)
    values.update(changes)
    return save_network(db, FtnNetwork(**values))


def test_a_port_written_into_the_uplink_host_is_the_uplink_port(db):
    network = _network(db, uplink_host="[2001:db8::1]:24555")
    assert (network.uplink_host, network.uplink_port) == ("2001:db8::1", 24555)
    stored = get_network(db, network.id)
    assert (stored.uplink_host, stored.uplink_port) == ("2001:db8::1", 24555)


def test_an_unreadable_uplink_host_is_refused(db):
    with pytest.raises(FtnNetworkError, match="IPv6"):
        _network(db, uplink_host="[2001:db8::1")


def test_tls_settings_are_stored_and_a_learned_pin_never_replaces_one(db):
    network = _network(db, uplink_tls=TLS_PINNED, uplink_tls_pin="")
    assert get_network(db, network.id).uplink_tls == TLS_PINNED
    assert set_uplink_tls_pin(db, network.id, "11" * 32) is True
    assert set_uplink_tls_pin(db, network.id, "22" * 32) is False
    assert get_network(db, network.id).uplink_tls_pin == "11" * 32
    with pytest.raises(FtnNetworkError):
        save_network(db, replace(get_network(db, network.id), uplink_tls="maybe"))


# --- TLS, dialled and answered ------------------------------------------------------


def _certificate(tmp_path, name: str, host: str = "localhost") -> tuple[str, str, str]:
    """A self-signed certificate for `host`: (cert file, key file, SHA-256)."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path), hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def _tls_hub(cert: str, key: str, received: list, files=(), host="127.0.0.1"):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    async def handle(reader, writer):
        try:
            result = await run_session(
                reader, writer, originating=False, our_addresses=[HUB], system=SystemInfo("Hub", "hubop"),
                password_for=lambda addresses: "SECRET", outgoing_for=lambda addresses, secure: list(files),
                timeout=5)
            received.extend(result.received)
        except Exception:  # noqa: BLE001 -- the test asserts on the node's side
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, 0, ssl=context)


def test_dialling_a_pinned_hub_learns_and_then_checks_its_certificate(tmp_path):
    cert, key, fingerprint = _certificate(tmp_path, "hub")

    async def run():
        server = await _tls_hub(cert, key, [])
        port = server.sockets[0].getsockname()[1]
        try:
            _, writer, seen = await dial("127.0.0.1", port, tls=TLS_PINNED)
            writer.close()
            _, writer, again = await dial("127.0.0.1", port, tls=TLS_PINNED, pin=seen)
            writer.close()
            with pytest.raises(TransportError, match="certificate changed"):
                await dial("127.0.0.1", port, tls=TLS_PINNED, pin="00" * 32)
            # A self-signed certificate is no certificate authority's.
            with pytest.raises(TransportError, match="did not verify"):
                await dial("localhost", port, tls=TLS_VERIFIED)
            return seen, again
        finally:
            server.close()
            await server.wait_closed()

    seen, again = asyncio.run(run())
    assert seen == again == fingerprint


def test_a_plain_dial_carries_no_fingerprint():
    async def run():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        try:
            _, writer, fingerprint = await dial("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.close()
            return fingerprint
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run()) is None


@needs_ipv6
def test_a_name_with_an_ipv6_address_is_dialled_over_ipv6():
    peers = []

    async def run():
        async def handle(reader, writer):
            peers.append(writer.get_extra_info("peername")[0])
            writer.close()

        server = await asyncio.start_server(handle, "::1", 0)
        try:
            _, writer, _ = await dial("::1", server.sockets[0].getsockname()[1])
            writer.close()
            await asyncio.sleep(0.1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    assert peers == ["::1"]


def _setup(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    network = _network(db)
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    return network


def _posts(db):
    return [row["body"] for row in db.connection.execute("SELECT body FROM posts ORDER BY id")]


def test_the_mailer_calls_over_tls_pins_the_hub_and_refuses_a_changed_certificate(db, tmp_path):
    network = _setup(db)
    cert, key, fingerprint = _certificate(tmp_path, "hub")
    other_cert, other_key, _ = _certificate(tmp_path, "impostor")

    async def run():
        received = []
        server = await _tls_hub(cert, key, received, files=[OutgoingFile("0000abcd.pkt", _hub_mail("Over TLS"))])
        port = server.sockets[0].getsockname()[1]
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane)
        try:
            current = save_network(db, replace(network, uplink_port=port, uplink_tls=TLS_PINNED))
            first = await mailer.poll(current)
            first_error = first.last_error
            pinned = get_network(db, network.id)
            server.close()
            await server.wait_closed()
            impostor = await _tls_hub(other_cert, other_key, [])
            moved = save_network(db, replace(pinned, uplink_port=impostor.sockets[0].getsockname()[1]))
            second = await mailer.poll(moved)
            impostor.close()
            await impostor.wait_closed()
            return first_error, pinned, second.last_error
        finally:
            await mailer.close()
            lane.close()

    first_error, pinned, second_error = asyncio.run(run())
    assert first_error is None
    assert pinned.uplink_tls_pin == fingerprint
    assert _posts(db) == ["Over TLS"]
    assert "certificate changed" in second_error


def test_the_listener_answers_over_tls_and_plain_alike(db, tmp_path):
    network = _setup(db)
    save_network(db, replace(network, answers_calls=True))
    cert, key, fingerprint = _certificate(tmp_path, "node")
    set_config(db, listener_module.HOST_CONFIG_KEY, "127.0.0.1")
    plain, tls = _free_ports(2)
    set_config(db, listener_module.PORT_CONFIG_KEY, str(plain))
    set_config(db, listener_module.TLS_PORT_CONFIG_KEY, str(tls))
    set_config(db, listener_module.TLS_CERT_CONFIG_KEY, cert)
    set_config(db, listener_module.TLS_KEY_CONFIG_KEY, key)

    async def run():
        lane = DatabaseLane(db.path)
        listener = FtnListener(lane)
        try:
            await listener.reconcile()
            assert listener.tls_listening_on == tls and listener.tls_error is None
            reader, writer, seen = await dial("127.0.0.1", tls, tls=TLS_PINNED)
            try:
                result = await run_session(reader, writer, originating=True, our_addresses=[HUB],
                                           system=SystemInfo("Hub", "op"), password="SECRET",
                                           outgoing=[OutgoingFile("in.pkt", _hub_mail("Came over TLS"))], timeout=5)
            finally:
                writer.close()
            for _ in range(500):
                if listener.recent:
                    break
                await asyncio.sleep(0.01)
            return seen, result, list(listener.recent)
        finally:
            await listener.close()
            lane.close()

    seen, result, recent = asyncio.run(run())
    assert seen == fingerprint and result.secure
    assert recent[-1].tls and recent[-1].secure
    assert _posts(db) == ["Came over TLS"]


def test_a_tls_port_without_usable_files_leaves_the_plain_port_answering(db, tmp_path):
    network = _setup(db)
    save_network(db, replace(network, answers_calls=True))
    plain, tls = _free_ports(2)
    set_config(db, listener_module.HOST_CONFIG_KEY, "127.0.0.1")
    set_config(db, listener_module.PORT_CONFIG_KEY, str(plain))
    set_config(db, listener_module.TLS_PORT_CONFIG_KEY, str(tls))
    set_config(db, listener_module.TLS_CERT_CONFIG_KEY, str(tmp_path / "missing.crt"))

    async def run():
        lane = DatabaseLane(db.path)
        listener = FtnListener(lane)
        try:
            await listener.reconcile()
            return listener.listening_on, listener.tls_listening_on, listener.tls_error
        finally:
            await listener.close()
            lane.close()

    where, tls_where, error = asyncio.run(run())
    assert where == ("127.0.0.1", plain) and tls_where is None
    assert f"cannot answer TLS on port {tls}" in error


@needs_ipv6
def test_the_listener_answers_on_ipv6_and_ipv4_together(db):
    network = _setup(db)
    save_network(db, replace(network, answers_calls=True))
    (port,) = _free_ports(1)
    set_config(db, listener_module.HOST_CONFIG_KEY, "127.0.0.1 ::1")
    set_config(db, listener_module.PORT_CONFIG_KEY, str(port))

    async def call(host):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            return await run_session(reader, writer, originating=True, our_addresses=[FtnAddress(21, 4, 4)],
                                     system=SystemInfo("Caller", "op"), password="", timeout=5)
        finally:
            writer.close()

    async def run():
        lane = DatabaseLane(db.path)
        listener = FtnListener(lane)
        try:
            await listener.reconcile()
            results = [await call("::1"), await call("127.0.0.1")]
            for _ in range(500):
                if len(listener.recent) >= 2:
                    break
                await asyncio.sleep(0.01)
            return listener.addresses, results, [call.peer for call in listener.recent]
        finally:
            await listener.close()
            lane.close()

    addresses, results, peers = asyncio.run(run())
    assert addresses == ("127.0.0.1", "::1")
    assert all(not result.secure for result in results)
    assert sorted(peers) == ["127.0.0.1", "::1"]


def test_a_hand_edited_listen_host_listens_everywhere_rather_than_failing(db):
    set_config(db, listener_module.HOST_CONFIG_KEY, "not an address")
    assert listen_settings(db).hosts == ()


def _free_ports(count: int) -> list[int]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for probe in sockets:
            probe.bind(("127.0.0.1", 0))
        return [probe.getsockname()[1] for probe in sockets]
    finally:
        for probe in sockets:
            probe.close()


# --- a call that fails part way ----------------------------------------------------


def test_a_call_cut_off_after_the_hub_sent_mail_still_tosses_it(db):
    """The hub hands over a packet, the node confirms it, and the line drops:
    the hub has let go of the packet, so the node must keep it."""
    from netbbs.ftn.binkp import M_ADR, M_FILE, M_GOT, M_OK, M_PWD, encode_frame, read_frame

    network = _setup(db)
    packet = _hub_mail("Kept although the call broke")

    async def hub(reader, writer):
        writer.write(encode_frame(M_ADR, b"21:1/100"))
        await writer.drain()
        while (await read_frame(reader, 5))[0] != M_PWD:
            pass
        writer.write(encode_frame(M_OK, b"secure") + encode_frame(M_FILE, f"0000abcd.pkt {len(packet)} 0 0".encode())
                     + encode_frame(None, packet) + encode_frame(M_FILE, b"cut.pkt 99 0 0"))
        await writer.drain()
        while (await read_frame(reader, 5))[0] != M_GOT:
            pass
        writer.close()

    async def run():
        server = await asyncio.start_server(hub, "127.0.0.1", 0)
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane)
        try:
            current = save_network(db, replace(network, uplink_port=server.sockets[0].getsockname()[1]))
            return await mailer.poll(current)
        finally:
            await mailer.close()
            lane.close()
            server.close()
            await server.wait_closed()

    status = asyncio.run(run())
    assert status.failures == 1 and "before that" in status.last_error
    assert _posts(db) == ["Kept although the call broke"]
