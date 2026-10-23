"""A SysOp verifies their own identity (maintainer decision 2026-10-23,
issue #1443): a real-name echo needs the SysOp's verified name. Staff who
may verify identity still vouch for others only. A self-verification reads
as one in the history and is never shared over Link, where it would look
like a third party's."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from netbbs.attestation import (
    AttestationError,
    attest_age,
    attest_name,
    get_attestation,
    set_attestation_link_visible,
)
from netbbs.auth.users import create_user, set_can_verify_identity
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.net import profile_flow
from netbbs.storage.database import Database
from tests.test_verify_identity_screen import FakeSession, _visible


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "Carrier", password="hunter2", user_level=255)


@pytest.fixture
def helper(db, sysop):
    user = create_user(db, "helper", password="hunter2", user_level=254)
    return set_can_verify_identity(db, user, True, changed_by=sysop)


def _picker_candidates(db, verifier, monkeypatch) -> list[str]:
    seen: dict[str, list] = {}

    async def fake_pick_item(session, items, **kwargs):
        seen["items"] = list(items)
        return None

    monkeypatch.setattr(profile_flow, "pick_item", fake_pick_item)
    asyncio.run(profile_flow._verify_identity_menu(FakeSession([]), db, verifier))
    return [user.username for user in seen["items"]]


def test_a_sysop_finds_their_own_account_in_the_verify_picker(db, sysop, helper, monkeypatch):
    assert "Carrier" in _picker_candidates(db, sysop, monkeypatch)


def test_staff_who_may_verify_do_not_find_their_own_account(db, sysop, helper, monkeypatch):
    names = _picker_candidates(db, helper, monkeypatch)
    assert "helper" not in names and "Carrier" in names


def test_a_sysop_verifies_their_own_name_on_the_verify_screen(db, sysop):
    session = FakeSession(["0", "2", "Jane Carrier", "s", "y", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, sysop))
    text = " ".join(_visible(session).split())
    assert "Verifying yourself" in text
    assert "Record for yourself: real name Jane Carrier." in text
    assert "A verification you make of yourself is not shared over Link." in text
    assert "Recorded the verified real name (self)." in text
    assert get_attestation(db, sysop, "name").attested_value == "Jane Carrier"


def test_a_sysop_verifies_their_own_name_and_age(db, sysop):
    name = attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    age = attest_age(db, sysop, date(1980, 1, 2), verifier=sysop)
    assert name.self_attested and age.self_attested


def test_staff_who_may_verify_cannot_verify_themselves(db, sysop, helper):
    with pytest.raises(AttestationError, match="only a SysOp can verify their own identity"):
        attest_name(db, helper, "Hel Per", verifier=helper)
    with pytest.raises(AttestationError, match="only a SysOp"):
        attest_age(db, helper, date(1980, 1, 2), verifier=helper)
    assert get_attestation(db, helper, "name") is None

    # ... and still verify others, and are verified by the SysOp as before.
    other = create_user(db, "other", password="hunter2")
    assert not attest_name(db, other, "Ot Her", verifier=helper).self_attested
    assert not attest_name(db, helper, "Hel Per", verifier=sysop).self_attested


def test_the_history_says_a_self_verification_is_one(db, sysop, helper):
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    attest_name(db, helper, "Hel Per", verifier=sysop)

    (own,) = [a for a in list_actions_for_target_user(db, sysop.id) if a.action == "attest_name"]
    (theirs,) = [a for a in list_actions_for_target_user(db, helper.id) if a.action == "attest_name"]
    assert own.detail == "attested name for 'Carrier' (self)"
    assert theirs.detail == "attested name for 'helper'"


def test_a_self_verification_is_never_shared_over_link(db, sysop, helper):
    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    with pytest.raises(AttestationError, match="not shared over Link"):
        set_attestation_link_visible(db, sysop, "name", True)
    assert not get_attestation(db, sysop, "name").link_visible
    # Switching it off is always allowed.
    assert not set_attestation_link_visible(db, sysop, "name", False).link_visible

    # A verification by someone else can be shared, as before.
    attest_name(db, helper, "Hel Per", verifier=sysop)
    assert set_attestation_link_visible(db, helper, "name", True).link_visible


def test_a_self_verification_is_not_built_into_a_link_object(db, sysop):
    import nacl.signing

    from netbbs.link.remote_attestation import build_link_visible_remote_attestation

    attest_name(db, sysop, "Jane Carrier", verifier=sysop)
    # Forced on behind the domain's back: the builder still refuses.
    db.connection.execute("UPDATE user_attestations SET link_visible = 1")
    db.connection.commit()
    with pytest.raises(ValueError, match="self-verification"):
        build_link_visible_remote_attestation(
            db, sysop, nacl.signing.SigningKey.generate(), home_node_fingerprint="f" * 64, attribute="name",
            issued_at="2026-10-23T00:00:00Z", expires_at="2026-11-23T00:00:00Z",
        )
