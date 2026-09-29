"""Who may use mail, and who may be sent it (issue #816, design doc §6.4).

Mail has a SysOp-set level like the node's other gates, and the guest
account (issue #531) never has mail: every guest signs in as the same
account, so its inbox would be read by strangers and its letters sent under
one shared name. Nothing delivers into the guest's mailbox either -- not
local mail, not Link mail, not a moderator's rejection notice.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, delete_post
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.config import get_mail_min_level, set_mail_min_level
from netbbs.guest import set_guest_user
from netbbs.link.mail import bounce_reason_text, deliver_link_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import (
    GUEST_MAIL_REFUSAL,
    MailRecipientRefused,
    list_inbox,
    list_sent,
    mail_access_refusal,
    mail_recipient_refusal,
    send_mail,
)
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import InputHistory
from netbbs.net.mail_flow import browse_mail
from netbbs.net.main_menu import _main_menu
from netbbs.net.notices import take_notices
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession as AdminSession, _visible, _written_text as _admin_text
from tests.test_link_mail import _incoming_message
from tests.test_first_time_caller import _MenuSession
from tests.test_mail_flow import FakeSession, _visible_text
from tests.test_new_scan import _visible_text as _menu_visible_text


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
    return create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


@pytest.fixture
def guest(db):
    account = create_user(db, "visitor", password="hunter2pw", user_level=10)
    set_guest_user(db, account)
    return account


def _menu(db, lane, user, keys):
    session = FakeSession(keys=keys, lines=["y"])
    asyncio.run(_main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane))
    return session


# -- who may open mail ---------------------------------------------------------


def test_mail_is_open_to_every_account_by_default(db, alice):
    assert get_mail_min_level(db) == 0
    assert mail_access_refusal(db, alice) is None


def test_the_guest_account_has_no_mail_whatever_its_level(db, alice, guest):
    set_mail_min_level(db, 0)
    assert mail_access_refusal(db, guest) == GUEST_MAIL_REFUSAL
    # The same level on an ordinary account is no bar.
    assert mail_access_refusal(db, alice) is None


def test_turning_guest_login_off_gives_the_account_its_mail_back(db, guest):
    set_guest_user(db, None)
    assert mail_access_refusal(db, guest) is None


def test_the_mail_level_closes_mail_below_it(db, alice, sysop):
    set_mail_min_level(db, 20)
    assert mail_access_refusal(db, alice) == "Mail is open from access level 20; yours is 10."
    set_mail_min_level(db, SYSOP_LEVEL)
    assert mail_access_refusal(db, sysop) is None


def test_the_mail_level_refuses_a_level_out_of_range(db):
    with pytest.raises(ValueError):
        set_mail_min_level(db, 256)
    with pytest.raises(ValueError):
        set_mail_min_level(db, -1)


# -- who may be sent mail -------------------------------------------------------


def test_local_mail_to_the_guest_account_is_refused(db, alice, guest):
    with pytest.raises(MailRecipientRefused, match="shared guest account"):
        send_mail(db, alice, guest, "Hello", "anyone there?")
    assert list_inbox(db, guest) == []
    assert mail_recipient_refusal(db, alice) is None


def test_mail_to_an_account_below_the_mail_level_still_arrives(db, sysop, alice):
    # It waits for the day the SysOp raises the account's level.
    set_mail_min_level(db, 20)
    send_mail(db, sysop, alice, "Welcome", "you will be validated soon")
    assert len(list_inbox(db, alice)) == 1


def test_link_mail_to_the_guest_account_bounces_no_mailbox(db, guest):
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")
    message = _incoming_message(node_identity, remote, recipient="visitor")

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    row = db.connection.execute("SELECT ack_event_json FROM link_mail_acknowledgements").fetchone()
    envelope = json.loads(row["ack_event_json"])["envelope"]
    assert envelope["object_type"] == "link_message_bounced"
    assert envelope["payload"]["reason"] == "no_mailbox"
    # The sender is told in words, not the code.
    assert "guest account" in bounce_reason_text("no_mailbox")


def test_a_rejected_guest_post_sends_no_mail_and_logs_no_failure(db, sysop, guest, caplog):
    board = create_board(db, "general", creator=sysop, moderated=True)
    held = create_post(db, board, guest, "Hello", "what I wrote")

    with caplog.at_level(logging.WARNING):
        delete_post(db, held, deleted_by=sysop, reason="off topic")

    assert list_inbox(db, guest) == []
    assert "not delivered" not in caplog.text


# -- every way in -----------------------------------------------------------------


def test_the_main_menu_offers_no_mail_to_the_guest(db, lane, guest):
    session = _menu(db, lane, guest, ["l"])

    text = _visible_text(session)
    assert "-mail" not in text
    assert "mail caught up" not in text


def test_the_main_menu_offers_no_mail_below_the_mail_level(db, lane, alice):
    set_mail_min_level(db, 20)
    session = _menu(db, lane, alice, ["l"])

    text = _visible_text(session)
    assert "-mail" not in text
    assert "mail caught up" not in text


def test_the_main_menu_still_offers_mail_at_the_mail_level(db, lane, alice):
    set_mail_min_level(db, 10)
    session = _menu(db, lane, alice, ["l"])

    assert "-mail" in _visible_text(session)


def test_e_at_the_main_menu_tells_the_guest_why_and_opens_nothing(db, lane, alice, guest):
    send_mail(db, alice, alice, "private", "for alice only")
    session = _menu(db, lane, guest, ["e", "l"])

    text = _visible_text(session)
    assert "Mail needs an account of your own" in " ".join(text.split())
    assert "Inbox" not in text
    assert "private" not in text


def test_the_mailbox_itself_refuses_the_guest(db, lane, guest):
    session = FakeSession(keys=[], lines=[])
    asyncio.run(browse_mail(session, lane, guest))

    # Refused before anything is drawn: the reason waits for the next screen.
    assert session.written == []
    assert any("Mail needs an account of your own" in line for line in take_notices(session))


def test_the_to_prompt_refuses_the_guest_account_and_asks_again(db, lane, alice, guest):
    session = FakeSession(keys=["c", "s", "b"], lines=["visitor", "sysop", "Hello", "Hi there", "/done"])
    create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    asyncio.run(browse_mail(session, lane, alice))

    text = " ".join(_visible_text(session).split())
    assert "visitor is this board's shared guest account, which has no mailbox." in text
    assert "Message sent." in text
    assert list_inbox(db, guest) == []
    [sent] = list_sent(db, alice)
    assert sent.subject == "Hello"


def test_help_tells_a_guest_why_there_is_no_mail(db, sysop, guest):
    session = _MenuSession(["?", " ", "l", "y"])
    asyncio.run(_main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), guest))

    text = " ".join(_menu_visible_text(session).split())
    assert "This board is run by sysop." in text
    assert "Send them E-mail" not in text
    assert "Mail needs an account of your own" in text


# -- the SysOp's setting --------------------------------------------------------


def test_the_mail_level_is_a_limits_setting(db, lane, sysop):
    # s: Settings, s: Limits & retention, m: mail level, 20, s: save.
    session = AdminSession(["s", "s", "m", "20", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    assert get_mail_min_level(db) == 20
    assert "Mail level" in _visible(_admin_text(session))
    audit = [a for a in list_recent_actions(db, limit=10) if a.action == "set_limits_and_retention"]
    assert len(audit) == 1 and "mail_level=20" in audit[0].detail
