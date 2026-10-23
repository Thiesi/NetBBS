"""FTN robots and echo-list import (design doc §6.8, issues #1463, #1465,
#1459): FileFix next to AreaFix, robot names, the request rules, Change
robot password, and importing a hub's .NA/.FA list -- the rules directly,
the screens through the real console with the scripted `FakeSession` of
`tests.test_admin_flow`. The echo lists are nixNet's, from its infopack."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board, get_board_by_name, list_boards
from netbbs.communities import create_community
from netbbs.files.areas import create_file_area, list_file_areas
from netbbs.ftn import echolist as echolist_module
from netbbs.ftn import networks as ftn_networks
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.areafix import (
    AREAFIX,
    FILEFIX,
    MAX_COMMANDS,
    AreaFixError,
    queue_password_change,
    queue_robot_requests,
    robot_commands,
)
from netbbs.ftn.echolist import (
    ECHOMAIL,
    FILE_ECHOES,
    EchoListEntry,
    EchoListError,
    import_echoes,
    imported_name,
    parse_echolist,
)
from netbbs.ftn.message import decode_message
from netbbs.ftn.networks import (
    FtnNetwork,
    FtnNetworkError,
    board_area,
    get_network,
    list_networks,
    save_network,
    set_board_area,
)
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed, parse_packet
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.ftn_console import ftn_networks_screen, ftn_status_screen
from netbbs.net.ftn_robots_console import edit_echolist_import
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
FIXTURES = Path(__file__).parent / "fixtures" / "ftn"


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


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _network(db, **changes):
    values = dict(name="nixNet", domain="nixnet", our_address=NODE, uplink_address=HUB,
                  uplink_host="hub.example", enabled=True, areafix_password="FIXPW")
    values.update(changes)
    return save_network(db, FtnNetwork(**values))


def _controls() -> NodeControls:
    return NodeControls(session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
                        shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0)


def _sent(db, network_id):
    """The queued netmails, decoded."""
    header = PacketHeader(orig=NODE, dest=HUB, created=None)
    return [decode_message(parse_packet(build_packet_from_packed(header, [row.packed])).messages[0])
            for row in queue.pending_outbound(db, network_id)]


# --- the request rules ---------------------------------------------------------


def test_known_commands_are_checked_and_tags_normalised():
    assert robot_commands("fsx_gen +FSX_BOT -fsx_old %Query %linked %unlinked %avail %help %pause %resume") == [
        "+FSX_GEN", "+FSX_BOT", "-FSX_OLD", "%QUERY", "%LINKED", "%UNLINKED", "%AVAIL", "%HELP", "%PAUSE",
        "%RESUME"]
    # A command that takes nothing ends where it is: the next word is a tag.
    assert robot_commands("%LIST nix_bsd") == ["%LIST", "+NIX_BSD"]


def test_rescan_is_never_a_link_request_and_keeps_its_arguments():
    # Issue #1459: %RESCAN used to go to the hub as "+%RESCAN".
    assert robot_commands("%RESCAN") == ["%RESCAN"]
    assert robot_commands("+nix_bsd %rescan nix_bsd R=10") == ["+NIX_BSD", "%RESCAN NIX_BSD R=10"]
    assert robot_commands("%rescan R=10") == ["%RESCAN R=10"]
    assert robot_commands("%rescan nix_bsd /R=10 %list") == ["%RESCAN NIX_BSD /R=10", "%LIST"]


def test_an_unknown_percent_command_goes_verbatim_on_one_line():
    assert robot_commands("%Compress zip +nix_bbs") == ["%Compress zip +nix_bbs"]
    assert robot_commands("+nix_bbs\n%Notify off\n-nix_auto") == ["+NIX_BBS", "%Notify off", "-NIX_AUTO"]
    with pytest.raises(AreaFixError, match="robot command, not an echo"):
        robot_commands("+%RESCAN")
    with pytest.raises(AreaFixError, match="robot command, not an echo"):
        robot_commands("-%PAUSE")
    with pytest.raises(AreaFixError, match="name right after"):
        robot_commands("% LIST")


@pytest.mark.parametrize("command", ["%PWD secret", "%password new", "%PktPwd x", "%TICPWD y"])
def test_password_commands_are_refused_and_name_the_action(command):
    with pytest.raises(AreaFixError, match="Change robot password"):
        robot_commands(command)


def test_a_request_holds_at_most_fifty_commands():
    assert len(robot_commands(" ".join(f"TAG{n}" for n in range(MAX_COMMANDS)))) == MAX_COMMANDS
    with pytest.raises(AreaFixError, match=f"At most {MAX_COMMANDS}"):
        robot_commands(" ".join(f"TAG{n}" for n in range(MAX_COMMANDS + 1)))


# --- FileFix and the robots' names -----------------------------------------------


def test_filefix_uses_the_areafix_password_until_it_has_its_own(db, sysop):
    network = _network(db)
    queue_robot_requests(db, network, sysop, FILEFIX, ["+NIX_LIST"])
    network = save_network(db, FtnNetwork(**{**network.__dict__, "filefix_password": "FILEPW"}))
    queue_robot_requests(db, network, sysop, FILEFIX, ["%LIST"])
    first, second = _sent(db, network.id)
    assert (first.to_name, first.subject, first.body) == ("FileFix", "FIXPW", "+NIX_LIST")
    assert (second.to_name, second.subject) == ("FileFix", "FILEPW")


def test_robot_names_are_the_networks_and_are_checked(db, sysop):
    network = _network(db, areafix_robot="  AreaMgr ", filefix_robot="AllFix")
    assert (network.areafix_robot, network.filefix_robot) == ("AreaMgr", "AllFix")
    queue_robot_requests(db, network, sysop, AREAFIX, ["%LIST"])
    queue_robot_requests(db, network, sysop, FILEFIX, ["%LIST"])
    assert [m.to_name for m in _sent(db, network.id)] == ["AreaMgr", "AllFix"]
    for bad in ("", "x" * 36, "Área"):
        with pytest.raises(FtnNetworkError, match="robot's name"):
            _network(db, name=f"Other {len(bad)}", domain="other", areafix_robot=bad)


def test_a_new_database_and_an_upgraded_one_default_the_robots(db):
    network = _network(db)
    stored = get_network(db, network.id)
    assert (stored.areafix_robot, stored.filefix_robot, stored.filefix_password) == ("AreaFix", "FileFix", "")


def test_over_fifty_commands_are_split_into_several_requests(db, sysop):
    network = _network(db)
    assert queue_robot_requests(db, network, sysop, AREAFIX, [f"+TAG{n}" for n in range(120)]) == 3
    assert [len(m.body.split("\n")) for m in _sent(db, network.id)] == [50, 50, 20]


def test_filefix_needs_a_password_and_an_enabled_network(db, sysop):
    with pytest.raises(AreaFixError, match="AreaFix or FileFix password"):
        queue_robot_requests(db, _network(db, areafix_password=""), sysop, FILEFIX, ["%LIST"])
    with pytest.raises(AreaFixError, match="Enable the network"):
        queue_robot_requests(db, _network(db, name="Off", domain="off", enabled=False, uplink_host=""),
                             sysop, FILEFIX, ["%LIST"])


def test_the_status_screen_sends_a_filefix_request(db, lane, sysop):
    network = _network(db, filefix_robot="FileMgr")
    session = FakeSession(["f", "+nix_list %rescan nix_list", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = _visible(_written_text(session))
    assert "FileFix request queued: +NIX_LIST %RESCAN NIX_LIST" in text
    (message,) = _sent(db, network.id)
    assert (message.to_name, message.from_name, message.subject) == ("FileMgr", "sysop", "FIXPW")
    assert message.body.split("\n") == ["+NIX_LIST", "%RESCAN NIX_LIST"]


def test_the_status_screen_refuses_pwd_and_says_where_to_go(db, lane, sysop):
    network = _network(db)
    session = FakeSession(["a", "%PWD newsecret", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    assert "Change robot password" in _visible(_written_text(session))
    assert _sent(db, network.id) == []


def test_the_network_editor_edits_the_robots(db, lane, sysop):
    _network(db)
    # s: Settings, e: FTN networks, 1: the network, 17: AreaFix name, 19:
    # FileFix password typed unseen, s: save, then back out.
    session = FakeSession(["s", "e", "0", "1", "1", "7", "AllFix", "1", "9", "FILEPW", "s",
                           "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    (network,) = list_networks(db)
    assert (network.areafix_robot, network.filefix_robot, network.filefix_password) == ("AllFix", "FileFix", "FILEPW")


# --- Change robot password --------------------------------------------------------


def test_change_robot_password_sends_pwd_and_stores_it(db, sysop):
    network = _network(db)
    assert queue_password_change(db, network, sysop, FILEFIX, "NEWFILE") == ""
    (message,) = _sent(db, network.id)
    # Sent under the old password (FileFix's fallback), naming the new one.
    assert (message.to_name, message.subject, message.body) == ("FileFix", "FIXPW", "%PWD NEWFILE")
    stored = get_network(db, network.id)
    assert (stored.areafix_password, stored.filefix_password) == ("FIXPW", "NEWFILE")


def test_changing_areafix_keeps_filefix_on_the_old_password(db, sysop):
    network = _network(db)
    note = queue_password_change(db, network, sysop, AREAFIX, "NEWFIX")
    assert "FileFix keeps the old password" in note
    stored = get_network(db, network.id)
    assert (stored.areafix_password, stored.effective_filefix_password) == ("NEWFIX", "FIXPW")


def test_a_refused_change_stores_nothing(db, sysop, monkeypatch):
    network = _network(db)
    with pytest.raises(AreaFixError, match="already"):
        queue_password_change(db, network, sysop, AREAFIX, "FIXPW")
    with pytest.raises(AreaFixError, match="without spaces"):
        queue_password_change(db, network, sysop, AREAFIX, "two words")
    monkeypatch.setattr(queue, "MAX_PENDING_PER_NETWORK", 0)
    monkeypatch.setattr("netbbs.ftn.areafix.MAX_PENDING_PER_NETWORK", 0)
    with pytest.raises(queue.FtnQueueFullError):
        queue_password_change(db, network, sysop, AREAFIX, "NEWFIX")
    assert get_network(db, network.id).areafix_password == "FIXPW"
    assert _sent(db, network.id) == []


def test_the_networks_screen_changes_a_robot_password_typed_twice(db, lane, sysop):
    network = _network(db)
    # p: Change robot password on network 1, robot 1 (AreaFix), the new
    # password twice, then Back.
    session = FakeSession(["p", "1", "0", "1", "NEWFIX", "NEWFIX", "b"])
    asyncio.run(ftn_networks_screen(session, lane, sysop))
    assert "AreaFix password change queued and stored" in _visible(_written_text(session))
    assert get_network(db, network.id).areafix_password == "NEWFIX"
    (message,) = _sent(db, network.id)
    assert (message.subject, message.body) == ("FIXPW", "%PWD NEWFIX")
    assert any(a.action == "change_ftn_robot_password" and "NEWFIX" not in (a.detail or "")
               for a in list_recent_actions(db, limit=10))


def test_mismatched_passwords_send_nothing(db, lane, sysop):
    network = _network(db)
    session = FakeSession(["p", "1", "0", "1", "NEWFIX", "OTHER", "b"])
    asyncio.run(ftn_networks_screen(session, lane, sysop))
    assert "did not match" in _visible(_written_text(session))
    assert get_network(db, network.id).areafix_password == "FIXPW"
    assert _sent(db, network.id) == []


# --- echo lists -----------------------------------------------------------------


def test_nixnets_lists_parse():
    na = parse_echolist((FIXTURES / "NIXNET.NA").read_bytes())
    assert [e.tag for e in na.entries][:3] == ["NIX_ADMIN", "NIX_LINUX", "NIX_BSD"]
    assert len(na.entries) == 10 and na.skipped == 0
    assert na.entries[2].description == "FreeBSD, OpenBSD, NetBSD, etc."
    fa = parse_echolist((FIXTURES / "NIXNET.FA").read_bytes())
    assert [(e.tag, e.description) for e in fa.entries] == [
        ("NIX_LIST", "NixNet Nodelists"), ("NIX_INFO", "NixNet InfoPacks"), ("NIX_BINKD", "NixNet BinkD Nodelists")]


def test_comments_bad_lines_and_repeats_are_skipped_and_counted():
    data = ("; nixNet echoes\r\n\r\nnix_bsd\tBSD talk\r\n%RESCAN not an echo\r\nNIX_BSD again\r\n"
            "bad\x01tag x\r\nNIX_BARE\r\n\x1aafter EOF\r\n").encode("cp437")
    parsed = parse_echolist(data)
    assert [(e.tag, e.description) for e in parsed.entries] == [("NIX_BSD", "BSD talk"), ("NIX_BARE", "")]
    assert parsed.skipped == 3


def test_an_echo_list_is_bounded():
    with pytest.raises(EchoListError, match="at most 1024 KiB"):
        parse_echolist(b"x" * (1024 * 1024 + 1))
    with pytest.raises(EchoListError, match="10,000 lines"):
        parse_echolist(b"A\n" * 10_001)


def test_a_name_comes_from_the_description_cut_at_a_word_or_the_tag():
    assert imported_name(EchoListEntry("NIX_BSD", "FreeBSD, OpenBSD, NetBSD, etc.")) == "FreeBSD, OpenBSD, NetBSD, etc."
    long = EchoListEntry("NIX_ADMIN", "NixNet Administration (SysOps/Real Names Only)")
    assert imported_name(long) == "NixNet Administration (SysOps/Real Names"
    assert len(imported_name(long)) <= 40
    assert imported_name(EchoListEntry("NIX_BARE", "")) == "NIX_BARE"


def test_import_creates_maps_and_requests_in_one_go(db, sysop):
    network = _network(db)
    community = create_community(db, "nixNet", creator=sysop)
    entries = list(parse_echolist((FIXTURES / "NIXNET.NA").read_bytes()).entries)

    result = import_echoes(db, network, sysop, ECHOMAIL, entries, community.id)

    assert result.requests == 1 and len(result.created) == 10
    board = get_board_by_name(db, "FreeBSD, OpenBSD, NetBSD, etc.")
    assert board.community_id == community.id
    assert board_area(db, board).tag == "NIX_BSD"
    (message,) = _sent(db, network.id)
    assert message.to_name == "AreaFix" and message.body.split("\n")[:2] == ["+NIX_ADMIN", "+NIX_LINUX"]
    assert any(a.action == "import_ftn_echolist" for a in list_recent_actions(db, limit=30))


def test_a_long_list_is_requested_in_requests_of_fifty(db, sysop):
    network = _network(db)
    entries = [EchoListEntry(f"BIG_{n:03d}", f"Big echo {n}") for n in range(60)]
    assert import_echoes(db, network, sysop, ECHOMAIL, entries, None).requests == 2
    assert [len(m.body.split("\n")) for m in _sent(db, network.id)] == [50, 10]


def test_a_taken_name_falls_back_to_the_tag(db, sysop):
    network = _network(db)
    create_board(db, "BSD talk", creator=sysop)
    import_echoes(db, network, sysop, ECHOMAIL, [EchoListEntry("NIX_BSD", "BSD talk")], None)
    assert board_area(db, get_board_by_name(db, "NIX_BSD")).tag == "NIX_BSD"


def test_an_import_that_cannot_finish_writes_nothing(db, sysop, monkeypatch):
    network = _network(db)
    create_board(db, "BSD talk", creator=sysop)
    create_board(db, "NIX_BSD", creator=sysop)
    before = {b.name for b in list_boards(db)}
    entries = [EchoListEntry("NIX_LINUX", "Linux"), EchoListEntry("NIX_BSD", "BSD talk")]
    with pytest.raises(EchoListError, match="already exists"):
        import_echoes(db, network, sysop, ECHOMAIL, entries, None)
    assert {b.name for b in list_boards(db)} == before

    # A failure after the boards were made removes them again.
    monkeypatch.setattr("netbbs.ftn.areafix.MAX_PENDING_PER_NETWORK", 0)
    with pytest.raises(EchoListError, match="Nothing was imported"):
        import_echoes(db, network, sysop, ECHOMAIL, [EchoListEntry("NIX_LINUX", "Linux")], None)
    assert {b.name for b in list_boards(db)} == before
    assert _sent(db, network.id) == []


def test_mapped_tags_cant_be_imported_again(db, sysop):
    network = _network(db)
    set_board_area(db, create_board(db, "Linux", creator=sysop), network.id, "NIX_LINUX")
    with pytest.raises(EchoListError, match="Already mapped on nixNet: NIX_LINUX"):
        import_echoes(db, network, sysop, ECHOMAIL, [EchoListEntry("NIX_LINUX", "Linux again")], None)


def test_a_file_echo_list_makes_file_areas_and_asks_filefix(db, sysop, monkeypatch):
    mapped = []
    monkeypatch.setattr(ftn_networks, "set_file_area_tic",
                        lambda db, area, network_id, tag: mapped.append((area.name, network_id, tag)), raising=False)
    network = _network(db)
    entries = list(parse_echolist((FIXTURES / "NIXNET.FA").read_bytes()).entries)

    result = import_echoes(db, network, sysop, FILE_ECHOES, entries, None)

    assert result.mapped and result.requests == 1
    assert {a.name for a in list_file_areas(db)} >= {"NixNet Nodelists", "NixNet InfoPacks"}
    assert mapped == [("NixNet Nodelists", network.id, "NIX_LIST"), ("NixNet InfoPacks", network.id, "NIX_INFO"),
                      ("NixNet BinkD Nodelists", network.id, "NIX_BINKD")]
    (message,) = _sent(db, network.id)
    assert (message.to_name, message.body) == ("FileFix", "+NIX_LIST\n+NIX_INFO\n+NIX_BINKD")


def test_without_file_echo_mapping_file_areas_are_made_unmapped(db, sysop, monkeypatch):
    monkeypatch.delattr(ftn_networks, "set_file_area_tic", raising=False)
    network = _network(db)
    create_file_area(db, "Elsewhere", creator=sysop)
    result = import_echoes(db, network, sysop, FILE_ECHOES, [EchoListEntry("NIX_INFO", "NixNet InfoPacks")], None)
    assert not result.mapped and result.created == ("NixNet InfoPacks",)
    assert not echolist_module.file_echo_mapping_available()


def test_the_import_screen_checks_echoes_and_saves(db, lane, sysop, tmp_path):
    network = _network(db)
    set_board_area(db, create_board(db, "Linux", creator=sysop), network.id, "NIX_LINUX")
    path = tmp_path / "NIXNET.NA"
    path.write_bytes((FIXTURES / "NIXNET.NA").read_bytes())
    # i: Import echo list; the path; 02: Echoes -- row 01 is NIX_ADMIN, and
    # NIX_LINUX (mapped) has no number, so 02 is NIX_BSD; b: back to the
    # draft; s: save; b: leave the status screen.
    session = FakeSession(["i", str(path), "0", "2", "0", "1", "0", "2", "b", "s", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = _visible(_written_text(session))
    assert "NIXNET.NA: 10 echoes" in text
    assert "[=] NIX_LINUX" in text
    assert "Created 2 boards and queued 1 AreaFix request" in text
    assert board_area(db, get_board_by_name(db, "FreeBSD, OpenBSD, NetBSD, etc.")).tag == "NIX_BSD"
    (message,) = _sent(db, network.id)
    assert message.body == "+NIX_ADMIN\n+NIX_BSD"


def test_backing_out_of_the_import_writes_nothing(db, lane, sysop):
    network = _network(db)
    parsed = parse_echolist((FIXTURES / "NIXNET.NA").read_bytes())
    before = len(list_boards(db))
    session = FakeSession(["b"])
    assert asyncio.run(edit_echolist_import(session, lane, sysop, network, parsed, "NIXNET.NA", ECHOMAIL,
                                            None)) is None
    assert len(list_boards(db)) == before and _sent(db, network.id) == []


def test_saving_with_nothing_checked_keeps_the_draft(db, lane, sysop):
    network = _network(db)
    parsed = parse_echolist((FIXTURES / "NIXNET.NA").read_bytes())
    session = FakeSession(["s", "b"])
    asyncio.run(edit_echolist_import(session, lane, sysop, network, parsed, "NIXNET.NA", ECHOMAIL, None))
    assert "Check at least one echo" in _visible(_written_text(session))
