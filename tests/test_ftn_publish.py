"""Publishing (design doc §6.8 "Publishing", issue #1480): the pointlist
(FTS-5002, Boss format) and the nodelist segment (FTS-5000) -- their CRC-16
checked by the nodelist importer's own code, the segment round-tripping
through `import_list`, the editions written as files and ZIPs, hatching
through the outbound queue, the schedule run by the mailer, and the
console -- against real SQLite files and a real lane."""

from __future__ import annotations

import asyncio
import datetime
import io
import zipfile
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.links import DISABLED, DOWNLINK, POINT, POLL_LINK, FtnLink, delete_link, save_link
from netbbs.ftn.networks import FtnNetwork, delete_network, save_network
from netbbs.ftn.nodelist import HOW_FULL, check_crc, direct_route, import_list, list_lines
from netbbs.ftn.outbound import pending_files
from netbbs.ftn.publish import (
    POINTLIST,
    SCHEDULE_DAILY,
    SCHEDULE_WEEKLY,
    SEGMENT,
    ListFields,
    PointEntry,
    PublishError,
    PublishSettings,
    SegmentNode,
    build_pointlist,
    build_segment,
    edition_names,
    get_settings,
    is_due,
    list_point_entries,
    list_segment_nodes,
    publish_now,
    publish_scheduled,
    save_point_entry,
    save_segment_node,
    save_settings,
    suggested_node,
)
from netbbs.ftn.tic import parse_tic
from netbbs.moderation.log import list_recent_actions
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
FRIDAY = datetime.datetime(2026, 10, 23, 9, 30)  # day 296
NEXT_FRIDAY = datetime.datetime(2026, 10, 30, 9, 30)  # day 303


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="hub.example",
        session_password="SECRET", tic_password="TICPW", enabled=True))


@pytest.fixture
def points(db, network):
    pat = save_link(db, FtnLink(network_id=network.id, kind=POINT, address=FtnAddress(21, 1, 199, 1),
                                session_password="P1", name="Pat Point"))
    sam = save_link(db, FtnLink(network_id=network.id, kind=POINT, address=FtnAddress(21, 1, 199, 2),
                                session_password="P2"))
    return pat, sam


@pytest.fixture
def downlink(db, network):
    return save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=FtnAddress(21, 1, 200),
                                 session_password="D1", name="Down Under BBS", flavour=POLL_LINK,
                                 host="down.example", port=24555))


def _segment_settings(db, network, **changes) -> PublishSettings:
    settings = PublishSettings(
        network_id=network.id, segment_on=True,
        host=ListFields("Net 1 Hub", "Wellington NZ", "Joe Host", flags="IBN,INA:hub.example"), **changes)
    return save_settings(db, network, settings)


# --- the lists ------------------------------------------------------------------------------


def test_the_pointlist_is_boss_format_with_a_crc_the_importer_verifies(db, network, points):
    pat, sam = points
    save_point_entry(db, PointEntry(sam, fields=ListFields(location="Auckland", sysop="Sam Smith",
                                                            flags="IBN,INA:sam.example")))
    data = build_pointlist(db, network, FRIDAY.date())
    lines = list_lines(data)
    assert data.endswith(b"\r\n\x1a") and b"\r\n" in data and b"\n\n" not in data
    header = check_crc(lines, required=True)  # the nodelist importer's own check
    assert header.day == 296 and header.crc is not None
    assert lines[0].startswith(b";A fsxNet pointlist for 21:1/199 -- Friday, October 23, 2026 -- Day number 296 : ")
    text = [line.decode("ascii") for line in lines]
    assert "Boss,21:1/199" in text
    boss = text.index("Boss,21:1/199")
    assert text[boss + 1] == ",1,Pat_Point,Unknown,Pat_Point,-Unpublished-,300"
    assert text[boss + 2] == ",2,Point_2,Auckland,Sam_Smith,-Unpublished-,300,IBN,INA:sam.example"


def test_an_unlisted_or_disabled_point_is_left_out_and_none_left_is_refused(db, network, points):
    pat, sam = points
    save_point_entry(db, PointEntry(pat, listed=False))
    save_link(db, replace(sam, status=DISABLED))
    with pytest.raises(PublishError, match="No point is listed"):
        build_pointlist(db, network, FRIDAY.date())
    save_point_entry(db, PointEntry(pat, listed=True))
    text = build_pointlist(db, network, FRIDAY.date()).decode("ascii")
    assert ",1,Pat_Point" in text and ",2," not in text


def test_the_segment_round_trips_through_import_list_with_its_crc_verified(db, network, downlink):
    _segment_settings(db, network)
    suggestion = suggested_node(db, network)
    assert suggestion.node == 200 and suggestion.fields.name == "Down_Under_BBS"
    assert suggestion.fields.flags == "IBN:24555,INA:down.example"
    save_segment_node(db, network, replace(suggestion, fields=replace(suggestion.fields, location="Sydney",
                                                                      sysop="Dee Down")))
    save_segment_node(db, network, SegmentNode(network.id, 7, ListFields("Old BBS", "Nowhere", "Gone"),
                                               keyword="Down"))
    data = build_segment(db, network, get_settings(db, network.id), FRIDAY.date())
    text = [line.decode("ascii") for line in list_lines(data)]
    assert text[0].startswith(";A fsxNet nodelist segment for net 1 -- Friday, October 23, 2026 -- Day number 296 : ")
    assert "Host,1,Net_1_Hub,Wellington_NZ,Joe_Host,-Unpublished-,300,IBN,INA:hub.example" in text
    assert text.index("Down,7,Old_BBS,Nowhere,Gone,-Unpublished-,300") < text.index(
        ",200,Down_Under_BBS,Sydney,Dee_Down,-Unpublished-,300,IBN:24555,INA:down.example")
    # The importer reads it as a list from the nodelist echo would be: the
    # CRC-16 required and checked.
    outcome = import_list(db, network.id, data, how=HOW_FULL, require_crc=True, zone=NODE.zone)
    assert outcome.day == 296 and outcome.crc == int(text[0].rsplit(":", 1)[1])
    assert outcome.nodes == 2  # the Host line and node 200; a Down node takes no calls
    assert direct_route(db, network.id, FtnAddress(21, 1, 200)) == (FtnAddress(21, 1, 200), "down.example", 24555)


def test_a_changed_byte_fails_the_check(db, network, downlink):
    _segment_settings(db, network)
    data = build_segment(db, network, get_settings(db, network.id), FRIDAY.date())
    tampered = data.replace(b"Wellington_NZ", b"Wellington_AU")
    with pytest.raises(Exception, match="CRC-16 mismatch"):
        check_crc(list_lines(tampered), required=True)


@pytest.mark.parametrize("fields, reason", [
    (ListFields("A, B", "X", "Y"), "comma"),
    (ListFields("Café", "X", "Y"), "ASCII"),
    (ListFields("A", "X", "Y" * 37), "36"),
    (ListFields("A", "X", "Y", phone="555 1234"), "Phone"),
    (ListFields("A", "X", "Y", speed="fast"), "Speed"),
    (ListFields("A", "X", "Y", flags="IBN,IN A:x"), "Flag"),
    (ListFields("A" * 60, "B" * 60, "C" * 36, flags="IBN"), "157"),
    (ListFields("", "X", "Y"), "required"),
])
def test_a_line_no_list_can_carry_is_refused(db, network, fields, reason):
    with pytest.raises(PublishError, match=reason):
        save_segment_node(db, network, SegmentNode(network.id, 5, fields))
    assert list_segment_nodes(db, network.id) == []


def test_node_numbers_are_unique_and_in_range(db, network):
    line = ListFields("A", "B", "C")
    save_segment_node(db, network, SegmentNode(network.id, 5, line))
    with pytest.raises(PublishError, match="already listed"):
        save_segment_node(db, network, SegmentNode(network.id, 5, line))
    with pytest.raises(PublishError, match="1 to 32767"):
        save_segment_node(db, network, SegmentNode(network.id, 0, line))


def test_settings_are_checked(db, network):
    base = PublishSettings(network_id=network.id)
    with pytest.raises(PublishError, match="1-8 letters"):
        save_settings(db, network, replace(base, pointlist_name="TOO_LONG_NAME"))
    with pytest.raises(PublishError, match="full path"):
        save_settings(db, network, replace(base, folder="relative/folder"))
    with pytest.raises(PublishError, match="System name is required"):
        save_settings(db, network, replace(base, segment_on=True))
    saved = save_settings(db, network, replace(base, pointlist_on=True, pointlist_name="pnt21",
                                               pointlist_echo="fsx_pnt"))
    assert saved.pointlist_name == "PNT21" and saved.pointlist_echo == "FSX_PNT"


def test_a_point_publishes_nothing(db):
    point_net = save_network(db, FtnNetwork(name="Other", domain="other", our_address=FtnAddress(21, 1, 199, 5),
                                            uplink_address=HUB, session_password="x"))
    with pytest.raises(PublishError, match="is a point"):
        save_settings(db, point_net, PublishSettings(network_id=point_net.id, pointlist_on=True))


# --- publishing ------------------------------------------------------------------------------


def test_publish_now_writes_the_edition_and_its_zip_and_records_it(db, network, points, tmp_path):
    folder = tmp_path / "published"
    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True, archive=True,
                                               folder=str(folder)))
    results = publish_now(db, network.id, actor=None, moment=FRIDAY)
    assert len(results) == 1 and results[0].problem == ""
    plain, zipped = edition_names("POINTS", FRIDAY.date())
    assert (plain, zipped) == ("POINTS.296", "POINTS.Z96")
    data = (folder / plain).read_bytes()
    with zipfile.ZipFile(io.BytesIO((folder / zipped).read_bytes())) as archive:
        assert archive.namelist() == [plain] and archive.read(plain) == data
    settings = get_settings(db, network.id)
    assert settings.pointlist_day == 296 and settings.pointlist_file == zipped
    assert settings.pointlist_crc == check_crc(list_lines(data), required=True).crc
    assert pending_files(db, network.id) == []  # no file echo: nothing hatched
    assert any(a.action == "publish_ftn_list" for a in list_recent_actions(db, limit=5))


def test_publishing_hatches_into_the_file_echo_with_replaces_for_the_last_edition(db, network, points, tmp_path):
    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True, archive=True,
                                               pointlist_echo="FSX_PNT", folder=str(tmp_path / "out")))
    first = publish_now(db, network.id, actor=None, moment=FRIDAY)[0]
    assert first.hatched_into == "FSX_PNT" and first.problem == ""
    queued = pending_files(db, network.id)
    assert [f.send_name for f in queued][0] == "POINTS.Z96" and queued[1].send_name.endswith(".TIC")
    assert all(f.reference_kind == "publish" and f.destination == HUB.four_d for f in queued)
    sent = (tmp_path / "out" / "POINTS.Z96").read_bytes()
    from pathlib import Path
    assert Path(queued[0].path).read_bytes() == sent
    tic = parse_tic(Path(queued[1].path).read_bytes())
    assert tic.area == "FSX_PNT" and tic.file == "POINTS.Z96" and tic.password == "TICPW"
    assert tic.replaces == ()
    import zlib
    assert tic.crc == zlib.crc32(sent) & 0xFFFFFFFF
    publish_now(db, network.id, actor=None, moment=NEXT_FRIDAY)
    tics = [f for f in pending_files(db, network.id) if f.send_name.endswith(".TIC")]
    assert parse_tic(Path(tics[-1].path).read_bytes()).replaces == ("POINTS.Z96",)


def test_a_refused_hatch_keeps_the_written_file_and_says_why(db, network, points, tmp_path):
    save_network(db, replace(network, tic_password=""))
    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True, pointlist_echo="FSX_PNT",
                                               folder=str(tmp_path)))
    result = publish_now(db, network.id, actor=None, moment=FRIDAY)[0]
    assert result.written == ("POINTS.296",) and "No TIC password" in result.problem
    assert (tmp_path / "POINTS.296").is_file() and pending_files(db, network.id) == []
    assert "No TIC password" in get_settings(db, network.id).failure


def test_one_list_failing_does_not_stop_the_other(db, network, downlink, tmp_path):
    _segment_settings(db, network, pointlist_on=True, folder=str(tmp_path))
    results = {r.kind: r for r in publish_now(db, network.id, actor=None, moment=FRIDAY)}
    assert "No point is listed" in results[POINTLIST].problem and not results[POINTLIST].written
    assert results[SEGMENT].written == ("NET1.296",) and (tmp_path / "NET1.296").is_file()


def test_nothing_on_is_refused(db, network):
    with pytest.raises(PublishError, match="Neither"):
        publish_now(db, network.id, actor=None, moment=FRIDAY)


# --- the schedule --------------------------------------------------------------------------


def test_is_due_follows_the_schedule_once_a_day():
    weekly = PublishSettings(network_id=1, pointlist_on=True, schedule=SCHEDULE_WEEKLY, weekday=4)
    assert is_due(weekly, FRIDAY.date())
    assert not is_due(weekly, FRIDAY.date() + datetime.timedelta(days=1))
    assert not is_due(replace(weekly, scheduled_on=FRIDAY.date().isoformat()), FRIDAY.date())
    assert not is_due(replace(weekly, pointlist_on=False), FRIDAY.date())
    daily = replace(weekly, schedule=SCHEDULE_DAILY)
    assert is_due(daily, FRIDAY.date() + datetime.timedelta(days=1))


def test_publish_scheduled_publishes_once_on_its_day(db, network, points, tmp_path):
    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True, schedule=SCHEDULE_WEEKLY,
                                               weekday=4, folder=str(tmp_path)))
    assert publish_scheduled(db, network.id, moment=FRIDAY - datetime.timedelta(days=1)) == []
    assert len(publish_scheduled(db, network.id, moment=FRIDAY)) == 1
    assert publish_scheduled(db, network.id, moment=FRIDAY + datetime.timedelta(hours=3)) == []
    assert (tmp_path / "POINTS.296").is_file()


def test_the_mailer_runs_the_schedule(db, network, points, tmp_path, monkeypatch):
    from netbbs.ftn import mailer as mailer_module
    from netbbs.ftn import publish as publish_module

    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True, schedule=SCHEDULE_DAILY,
                                               folder=str(tmp_path)))
    monkeypatch.setattr(publish_module, "local_today", lambda _db: FRIDAY)
    lane = DatabaseLane(db.path)
    try:
        mailer = mailer_module.FtnMailer(lane)

        async def _no_call(network, *args, **kwargs):
            return None

        monkeypatch.setattr(mailer, "poll", _no_call)
        asyncio.run(mailer.check_once())
    finally:
        lane.close()
    assert (tmp_path / "POINTS.296").is_file()


# --- records -------------------------------------------------------------------------------


def test_a_points_line_goes_with_its_link_and_the_settings_with_the_network(db, network, points):
    pat, _sam = points
    save_point_entry(db, PointEntry(pat, fields=ListFields(location="Here")))
    save_segment_node(db, network, SegmentNode(network.id, 5, ListFields("A", "B", "C")))
    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True))
    delete_link(db, pat.id)
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_publish_points").fetchone()[0] == 0
    assert [e.link.address.point for e in list_point_entries(db, network)] == [2]
    delete_network(db, network.id)
    for table in ("ftn_publish_settings", "ftn_segment_nodes"):
        assert db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# --- the console ---------------------------------------------------------------------------


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def test_the_publishing_screen_switches_the_pointlist_on_and_publishes(db, network, points, sysop, tmp_path,
                                                                        monkeypatch):
    from netbbs.net.ftn_console import ftn_networks_screen

    save_settings(db, network, PublishSettings(network_id=network.id, folder=str(tmp_path)))
    lane = DatabaseLane(db.path)
    try:
        # p: Publishing on the network; s: Settings; 01: pointlist on; s:
        # save; p: publish now (no echo, so no question); b, b, b.
        session = FakeSession(["p", "1", "s", "0", "1", "s", "p", "b", "b", "b"])
        asyncio.run(ftn_networks_screen(session, lane, sysop))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "Saved the publishing settings" in text
    assert "Pointlist POINTS." in text and "CRC-16" in text
    assert get_settings(db, network.id).pointlist_on
    assert list(tmp_path.glob("POINTS.*"))


def test_the_publishing_screen_asks_before_hatching(db, network, points, sysop, tmp_path):
    from netbbs.net.ftn_publish_console import ftn_publish_screen

    save_settings(db, network, PublishSettings(network_id=network.id, pointlist_on=True, pointlist_echo="FSX_PNT",
                                               folder=str(tmp_path)))
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["p", "n", "b"])
        asyncio.run(ftn_publish_screen(session, lane, sysop, network))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "hatch into FSX_PNT?" in text and "Nothing published." in text
    assert not list(tmp_path.glob("POINTS.*")) and pending_files(db, network.id) == []


def test_the_nodes_screen_creates_a_line_from_a_downlink(db, network, downlink, sysop):
    from netbbs.net.ftn_publish_console import ftn_publish_screen

    lane = DatabaseLane(db.path)
    try:
        # n: Nodes; c: create (node 200 from the downlink); s: refused, no
        # location; 04: location; 05: sysop; s: save.
        session = FakeSession(["n", "c", "s", "0", "4", "Sydney", "0", "5", "Dee Down", "s", "b", "b", "b"])
        asyncio.run(ftn_publish_screen(session, lane, sysop, network))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "Location is required" in text and "Saved node 1/200" in text
    [node] = list_segment_nodes(db, network.id)
    assert node.node == 200 and node.fields.location == "Sydney" and node.fields.flags == "IBN:24555,INA:down.example"


def test_the_points_screen_edits_a_points_line(db, network, points, sysop):
    from netbbs.net.ftn_publish_console import ftn_publish_screen

    lane = DatabaseLane(db.path)
    try:
        # e: Entries for points; 01: Pat; 03: location; s: save.
        session = FakeSession(["e", "0", "1", "0", "3", "Auckland", "s", "b", "b"])
        asyncio.run(ftn_publish_screen(session, lane, sysop, network))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "Saved point 21:1/199.1's line" in text
    pat = list_point_entries(db, network)[0]
    assert pat.fields.location == "Auckland"


@pytest.mark.parametrize("fields", [
    ListFields("A", "X", "Y", phone="1-８００-555"),
    ListFields("A", "X", "Y", speed="３００"),
])
def test_non_ascii_digits_are_refused(db, network, fields):
    with pytest.raises(PublishError):
        save_segment_node(db, network, SegmentNode(network.id, 5, fields))


def test_a_network_name_outside_ascii_still_publishes(db, network, tmp_path):
    named = save_network(db, replace(network, name="FidoNet Südwest"))
    _segment_settings(db, named, folder=str(tmp_path))
    data = build_segment(db, named, get_settings(db, named.id), FRIDAY.date())
    assert b"FidoNet S?dwest" in data
    check_crc(list_lines(data), required=True)
