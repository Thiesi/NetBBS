"""Real-name echoes (issue #1443, design doc §6.8, §18): a board carrying an
echo and asking for verified and displayed names sends its authors' verified
real names as the From name, refuses a post that could not go out that way,
and never sends one under the username or cut short. Netmail to a verified
real name reaches its account. Against real SQLite files."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from netbbs.attestation import attest_name, revoke_attestation
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import CrosspostRefused, PostError, approve_post, create_crosspost, create_post
from netbbs.communities import create_community
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.networks import FtnNetwork, save_network, set_board_area
from netbbs.ftn.packet import PacketHeader, build_packet, build_packet_from_packed, parse_packet
from netbbs.ftn.scanner import export_post_if_ftn, real_name_refusal, sends_real_names
from netbbs.ftn.tosser import toss_packet
from netbbs.mail import list_inbox
from netbbs.net import board_flow
from netbbs.net.crosspost_flow import crosspost_refusal
from netbbs.storage.database import Database
from tests.test_board_list_and_reader import FakeSession

OURS = FtnAddress(86, 1, 7)
UPLINK = FtnAddress(86, 1, 1)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "Carrier", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="nixNet", domain="nixnet", our_address=OURS, uplink_address=UPLINK, uplink_host="hub.example",
        enabled=True, origin_text="ReLink",
    ))


@pytest.fixture
def admin_echo(db, sysop, network):
    board = create_board(db, "nixNet admin", creator=sysop, name_requirement="verified_and_displayed")
    set_board_area(db, board, network.id, "NIX_ADMIN")
    return board


@pytest.fixture
def general_echo(db, sysop, network):
    board = create_board(db, "nixNet general", creator=sysop)
    set_board_area(db, board, network.id, "NIX_GENERAL")
    return board


def _unpack(packed: bytes) -> FtnMessage:
    header = PacketHeader(orig=OURS, dest=UPLINK, created=None)
    return decode_message(parse_packet(build_packet_from_packed(header, [packed])).messages[0])


def _sent(db, network) -> list[FtnMessage]:
    return [_unpack(row.packed) for row in queue.pending_outbound(db, network.id)]


# -- From ---------------------------------------------------------------------


def test_a_real_name_echo_sends_the_verified_name_and_another_echo_the_username(
    db, sysop, network, admin_echo, general_echo,
):
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)

    assert export_post_if_ftn(db, create_post(db, admin_echo, sysop, "Hello admins", "Hi."), admin_echo)
    assert export_post_if_ftn(db, create_post(db, general_echo, sysop, "Hello all", "Hi."), general_echo)

    admin, general = _sent(db, network)
    assert (admin.area, admin.from_name) == ("NIX_ADMIN", "Jane Carrier")
    assert (general.area, general.from_name) == ("NIX_GENERAL", "Carrier")


def test_a_community_asking_for_displayed_names_makes_its_echo_send_them(db, sysop, network):
    community = create_community(db, "nixNet", default_name_requirement="verified_and_displayed", creator=sysop)
    board = create_board(db, "admin", creator=sysop, community_id=community.id)
    set_board_area(db, board, network.id, "NIX_ADMIN")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)

    assert sends_real_names(db, board)
    export_post_if_ftn(db, create_post(db, board, sysop, "s", "b"), board)
    assert _sent(db, network)[0].from_name == "Jane Carrier"


def test_a_verified_requirement_alone_does_not_send_real_names(db, sysop, network):
    board = create_board(db, "verified", creator=sysop, name_requirement="verified")
    set_board_area(db, board, network.id, "NIX_VERIFIED")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    export_post_if_ftn(db, create_post(db, board, sysop, "s", "b"), board)
    assert _sent(db, network)[0].from_name == "Carrier"


def test_a_local_board_asking_for_displayed_names_sends_nothing(db, sysop):
    board = create_board(db, "local", creator=sysop, name_requirement="verified_and_displayed")
    assert not sends_real_names(db, board)
    assert real_name_refusal(db, board, sysop) is None


def test_a_reply_to_a_real_name_post_is_addressed_to_that_name(db, sysop, network, admin_echo):
    member = create_user(db, "night_owl", password="hunter2")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    attest_name(db, member, "Owen Night", verifier=sysop)
    parent = create_post(db, admin_echo, member, "Question", "How?")
    export_post_if_ftn(db, parent, admin_echo)

    reply = create_post(db, admin_echo, sysop, "Re: Question", "Thus.", parent_post_id=parent.post_id)
    export_post_if_ftn(db, reply, admin_echo)

    first, second = _sent(db, network)
    assert first.from_name == "Owen Night"
    assert (second.from_name, second.to_name) == ("Jane Carrier", "Owen Night")
    assert second.reply == first.msgid


def test_a_reply_is_addressed_to_the_name_its_parent_went_out_under(db, sysop, network, admin_echo):
    """The parent's frozen name, not its author's current one (§5.5)."""
    member = create_user(db, "night_owl", password="hunter2")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    attest_name(db, member, "Owen Night", verifier=sysop)
    parent = create_post(db, admin_echo, member, "Question", "How?")
    export_post_if_ftn(db, parent, admin_echo)
    attest_name(db, member, "Owen Knight", verifier=sysop)

    reply = create_post(db, admin_echo, sysop, "Re: Question", "Thus.", parent_post_id=parent.post_id)
    export_post_if_ftn(db, reply, admin_echo)

    assert _sent(db, network)[1].to_name == "Owen Night"


# -- no verified name ---------------------------------------------------------


def test_a_sysop_without_a_verified_name_is_refused_before_the_post_is_written(db, sysop, network, admin_echo):
    with pytest.raises(PostError, match="nixNet admin sends real names to nixNet, and your account has no verified"):
        create_post(db, admin_echo, sysop, "Hello", "Hi.")
    assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


def test_a_held_post_goes_out_under_the_name_it_was_written_under(db, sysop, network):
    """Design doc §5.5: the name frozen on the post when it was written is
    what the board shows and what the network gets, even when the author's
    attestation was revoked before a moderator approved the post."""
    board = create_board(db, "held admin", creator=sysop, name_requirement="verified_and_displayed", moderated=True)
    set_board_area(db, board, network.id, "NIX_ADMIN")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    post = create_post(db, board, sysop, "Hello", "Hi.")
    revoke_attestation(db, sysop, "name", actor=sysop)

    assert export_post_if_ftn(db, approve_post(db, post, approved_by=sysop), board)

    stored = db.connection.execute(
        "SELECT author_verified_name FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()[0]
    assert stored == "Jane Carrier"
    assert [message.from_name for message in _sent(db, network)] == [stored]


def test_a_post_written_before_its_board_sent_real_names_stays_local_with_a_warning(db, sysop, network, caplog):
    """A post with no frozen name -- written while the board displayed no
    verified names -- has no name to go out under, whatever its author has
    now: it is never sent under the username."""
    board = create_board(db, "held admin", creator=sysop, moderated=True)
    post = create_post(db, board, sysop, "Hello", "Hi.")
    db.connection.execute("UPDATE boards SET name_requirement = 'verified_and_displayed' WHERE id = ?", (board.id,))
    db.connection.commit()
    from netbbs.boards.boards import get_board_by_name

    board = get_board_by_name(db, "held admin")
    set_board_area(db, board, network.id, "NIX_ADMIN")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)

    with caplog.at_level("WARNING"):
        assert not export_post_if_ftn(db, approve_post(db, post, approved_by=sysop), board)

    assert _sent(db, network) == []
    assert "no verified real name" in caplog.text and "NIX_ADMIN" in caplog.text
    assert db.connection.execute("SELECT ftn_msgid FROM posts WHERE post_id = ?", (post.post_id,)).fetchone()[0] is None


def test_a_post_by_no_local_account_stays_local(db, sysop, network, admin_echo, caplog):
    """A door's label, or a post carried from elsewhere, has no verified name
    here: it is never sent to a real-name echo under its label."""
    from netbbs.boards.posts import create_labelled_post

    post = create_labelled_post(db, admin_echo, "Blacksite.door", "News", "Things happened.")
    with caplog.at_level("WARNING"):
        assert not export_post_if_ftn(db, post, admin_echo)
    assert _sent(db, network) == []


# -- too long -----------------------------------------------------------------


def test_a_name_too_long_for_the_from_field_is_refused_at_posting(db, sysop, network, admin_echo):
    attest_name(db, sysop, "Johanna Wilhelmina Carrier-Montgomery", verifier=sysop)  # 37 characters

    with pytest.raises(PostError, match="Your verified real name is 37 bytes; nixNet allows 35"):
        create_post(db, admin_echo, sysop, "Hello", "Hi.")


def test_a_name_replaced_after_posting_does_not_change_the_from_name(db, sysop, network):
    board = create_board(db, "held admin", creator=sysop, name_requirement="verified_and_displayed", moderated=True)
    set_board_area(db, board, network.id, "NIX_ADMIN")
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    post = create_post(db, board, sysop, "Hello", "Hi.")
    attest_name(db, sysop, "Johanna Wilhelmina Carrier-Montgomery", verifier=sysop)

    assert export_post_if_ftn(db, approve_post(db, post, approved_by=sysop), board)

    assert [message.from_name for message in _sent(db, network)] == ["Jane Carrier"]


def test_the_length_is_counted_in_the_character_set_the_message_goes_out_in(db, sysop, network, admin_echo):
    """34 characters, one of them é: 34 bytes in CP437, 35 in UTF-8 -- and a
    snowman in the body makes the whole message UTF-8."""
    name = "José Wilhelmina Carrier-Montgomery"
    assert len(name) == 34
    attest_name(db, sysop, name, verifier=sysop)
    assert real_name_refusal(db, admin_echo, sysop) is None

    export_post_if_ftn(db, create_post(db, admin_echo, sysop, "Plain", "Plain text."), admin_echo)
    export_post_if_ftn(db, create_post(db, admin_echo, sysop, "Snow", "It snowed ☃"), admin_echo)
    assert [message.from_name for message in _sent(db, network)] == [name, name]

    attest_name(db, sysop, name + "s", verifier=sysop)  # 35 characters: 35 bytes in CP437, 36 in UTF-8
    assert real_name_refusal(db, admin_echo, sysop) is None
    create_post(db, admin_echo, sysop, "Plain again", "Plain text.")
    with pytest.raises(PostError, match="36 bytes"):
        create_post(db, admin_echo, sysop, "Snow again", "It snowed ☃")


# -- every way onto the board ---------------------------------------------------


def test_a_cross_post_onto_a_real_name_echo_is_refused_naming_it(db, sysop, network, admin_echo, general_echo):
    with pytest.raises(CrosspostRefused) as refused:
        create_crosspost(db, [general_echo, admin_echo], sysop, "Both", "Hi.")
    assert refused.value.board.id == admin_echo.id
    assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert crosspost_refusal(db, sysop, admin_echo, layout="prose", own_fingerprint=None) == (
        "sends real names to nixNet; yours is not verified"
    )


def test_a_cross_post_with_a_verified_name_sends_it_only_to_the_real_name_echo(
    db, sysop, network, admin_echo, general_echo,
):
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    boards = [general_echo, admin_echo]
    for board, post in zip(boards, create_crosspost(db, boards, sysop, "Both", "Hi.")):
        export_post_if_ftn(db, post, board)
    assert {message.area: message.from_name for message in _sent(db, network)} == {
        "NIX_GENERAL": "Carrier", "NIX_ADMIN": "Jane Carrier",
    }


def test_the_board_page_says_why_a_sysop_without_a_name_cannot_post(db, sysop, network, admin_echo):
    session = FakeSession(["b"])
    asyncio.run(board_flow._show_board(session, db, admin_echo, sysop))
    screen = " ".join(session.visible().split())
    assert "Read only: nixNet admin sends real names to nixNet, and your account has no verified real name." in screen
    assert "[P]ost" not in screen


def test_the_board_page_offers_posting_once_the_name_is_verified(db, sysop, network, admin_echo):
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    session = FakeSession(["b"])
    asyncio.run(board_flow._show_board(session, db, admin_echo, sysop))
    assert "[P]ost" in session.visible()
    assert "Read only" not in session.visible()


# -- netmail to a verified real name --------------------------------------------


def _netmail(to: str) -> bytes:
    message = FtnMessage(
        to_name=to, from_name="Alice Example", subject="Hi", body="A private note",
        kludges=[("INTL", "86:1/7 86:1/1"), ("MSGID", f"86:1/1 {abs(hash(to)) % 0xFFFFFFFF:08x}")],
        date=datetime.datetime(2026, 10, 23, 9, 30), orig_net=1, orig_node=1, dest_net=1, dest_node=7,
    )
    header = PacketHeader(orig=UPLINK, dest=OURS, created=datetime.datetime(2026, 10, 23, 10, 0))
    return build_packet(header, [encode_message(message)])


def _toss(db, network, data):
    return toss_packet(db, network, data, secure=True, remote_address="86:1/1", file_name="0000abcd.pkt")


def test_netmail_to_a_verified_real_name_reaches_its_account(db, sysop, network):
    member = create_user(db, "night_owl", password="hunter2")
    attest_name(db, member, "Owen Night", verifier=sysop)

    _toss(db, network, _netmail("  owen NIGHT "))

    (letter,) = list_inbox(db, member)
    assert letter.body == "A private note"
    assert list_inbox(db, sysop) == []


def test_a_username_is_matched_before_a_verified_real_name(db, sysop, network):
    owner = create_user(db, "Owen", password="hunter2")
    other = create_user(db, "someone", password="hunter2")
    attest_name(db, other, "Owen", verifier=sysop)

    _toss(db, network, _netmail("Owen"))

    assert len(list_inbox(db, owner)) == 1
    assert list_inbox(db, other) == []


def test_a_real_name_two_accounts_share_goes_to_the_sysop(db, sysop, network):
    first = create_user(db, "owen1", password="hunter2")
    second = create_user(db, "owen2", password="hunter2")
    attest_name(db, first, "Owen Night", verifier=sysop)
    attest_name(db, second, "owen night", verifier=sysop)

    _toss(db, network, _netmail("Owen Night"))

    assert list_inbox(db, first) == list_inbox(db, second) == []
    (letter,) = list_inbox(db, sysop)
    assert letter.body.startswith("[This netmail was addressed to 'Owen Night'")


def test_netmail_to_an_unverified_display_name_goes_to_the_sysop(db, sysop, network):
    from netbbs.attestation import set_display_name

    member = create_user(db, "night_owl", password="hunter2")
    set_display_name(db, member, "Owen Night")

    _toss(db, network, _netmail("Owen Night"))

    assert list_inbox(db, member) == []
    assert len(list_inbox(db, sysop)) == 1


def test_the_refusal_tells_a_sysop_to_verify_themselves_and_a_member_whom_to_ask(db, sysop, network, admin_echo):
    member = create_user(db, "night_owl", password="hunter2")
    assert real_name_refusal(db, admin_echo, sysop).endswith("Verify it yourself with [V]erify on the main menu.")
    assert real_name_refusal(db, admin_echo, member).endswith("Ask the SysOp to verify it.")
