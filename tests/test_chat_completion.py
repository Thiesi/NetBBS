"""
Tests for the BBS-specific Tab completer built in netbbs.net.chat_flow
(design doc round 49/Track 5g): command-name completion (permission-
aware for moderation commands), and username completion for /msg,
/private (online accounts only) vs /whois, /finger (every registered
account). The generic word-replacement mechanics themselves are covered
separately in tests/test_char_input_completion.py; this file only
exercises what `_build_completer`'s closure returns for a given line of
text. `/query` was removed in round 54.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def carol(db):
    return create_user(db, "carol", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "lobby", creator=alice)


# -- command-name completion --------------------------------------------


def test_bare_slash_lists_every_visible_command(db, presence, alice, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    candidates = completer("/")
    assert "/quit" in candidates
    assert "/help" in candidates
    assert "/msg" in candidates


def test_command_prefix_matches_only_names_starting_with_it(db, presence, alice, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    candidates = completer("/m")
    assert set(candidates) <= {"/msg", "/me", "/mute", "/members"}
    assert "/msg" in candidates
    assert "/me" in candidates
    assert "/members" in candidates  # always visible -- not gated on MANAGE_MEMBERS


def test_command_completion_is_case_insensitive(db, presence, alice, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert "/quit" in completer("/QU")


def test_non_moderator_does_not_see_moderation_commands_suggested(db, presence, alice, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    candidates = completer("/m")
    assert "/mute" not in candidates


def test_moderator_does_see_moderation_commands_suggested(db, presence, alice, channel):
    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    completer = chat_flow._build_completer(db, presence, channel, alice)
    candidates = completer("/m")
    assert "/mute" in candidates


def test_moderation_commands_stay_hidden_from_completion_after_a_moderate_grant_elsewhere(
    db, presence, alice, bob, channel
):
    # A grant on a *different* channel must not leak visibility here --
    # confirms the check is scoped to ctx.channel.id, not global.
    other = create_channel(db, "offtopic", creator=alice)
    grant_permissions(
        db, bob, object_type="channel", object_id=other.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    completer = chat_flow._build_completer(db, presence, channel, bob)
    assert "/mute" not in completer("/m")


def test_unrelated_command_prefix_matches_nothing(db, presence, alice, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/zzz") == []


# -- membership-admin commands (design doc round 33 points 8/11, Track 5h) --


def test_non_manager_does_not_see_membership_admin_commands_suggested(db, presence, alice, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    candidates = completer("/")
    assert "/uninvite" not in candidates
    assert "/grantaccess" not in candidates
    assert "/revokeaccess" not in candidates
    # /invite is a distinct predicate -- checked separately below.
    assert "/invite" not in candidates


def test_manager_does_see_membership_admin_commands_suggested(db, presence, alice, channel):
    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=alice,
    )
    completer = chat_flow._build_completer(db, presence, channel, alice)
    candidates = completer("/")
    assert "/invite" in candidates
    assert "/uninvite" in candidates
    assert "/grantaccess" in candidates
    assert "/revokeaccess" in candidates


def test_invite_is_suggested_to_a_plain_member_when_the_channel_opts_in(db, presence, alice, bob, channel):
    from netbbs.chat.membership import add_member

    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=alice,
    )
    open_channel = create_channel(
        db, "welcoming", creator=alice, members_only=True, allow_member_invites=True
    )
    grant_permissions(
        db, alice, object_type="channel", object_id=open_channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=alice,
    )
    add_member(db, open_channel, bob, granted_by=alice)

    completer = chat_flow._build_completer(db, presence, open_channel, bob)
    candidates = completer("/")
    assert "/invite" in candidates
    # bob has no MANAGE_MEMBERS -- the other three admin commands stay hidden.
    assert "/uninvite" not in candidates


# -- /msg, /private: online usernames only ------------------------------


def test_msg_completes_against_online_usernames(db, presence, alice, bob, channel):
    presence.enter("bob")
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/msg bo") == ["bob"]


def test_msg_does_not_suggest_an_offline_user(db, presence, alice, bob, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/msg bo") == []


def test_private_completes_the_same_way_as_msg(db, presence, alice, bob, channel):
    presence.enter("bob")
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/private bo") == ["bob"]


def test_query_is_no_longer_a_recognized_command_prefix(db, presence, alice, bob, channel):
    # design doc round 54: /query removed -- no longer completes at all.
    presence.enter("bob")
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/query bo") == []
    assert "/query" not in completer("/")


def test_msg_completion_is_case_insensitive(db, presence, alice, bob, channel):
    presence.enter("bob")
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/msg BO") == ["bob"]


def test_msg_completion_stops_once_a_full_argument_is_typed(db, presence, alice, bob, channel):
    presence.enter("bob")
    completer = chat_flow._build_completer(db, presence, channel, alice)
    # A trailing space after the username means we've moved on to the
    # message body -- free text, no candidates.
    assert completer("/msg bob ") == []


# -- /whois, /finger: any registered user, online or not -----------------


def test_whois_completes_against_offline_users_too(db, presence, alice, bob, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/whois bo") == ["bob"]


def test_finger_completes_against_offline_users_too(db, presence, alice, bob, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/finger bo") == ["bob"]


def test_whois_completion_matches_multiple_candidates(db, presence, alice, bob, carol, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert set(completer("/whois ")) == {"alice", "bob", "carol"}


# -- /invite: registered users who aren't already members (Track 5h) -----


def test_invite_completes_against_offline_users_too(db, presence, alice, bob, channel):
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/invite bo") == ["bob"]


def test_invite_excludes_users_already_a_member(db, presence, alice, bob, channel):
    from netbbs.chat.membership import add_member
    from netbbs.moderation import ChannelPermission, grant_permissions

    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=alice,
    )
    add_member(db, channel, bob, granted_by=alice)
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("/invite bo") == []


# -- plain chat text: no candidates --------------------------------------


def test_plain_chat_text_has_no_candidates(db, presence, alice, bob, channel):
    presence.enter("bob")
    completer = chat_flow._build_completer(db, presence, channel, alice)
    assert completer("hello bo") == []
