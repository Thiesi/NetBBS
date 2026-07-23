"""
Tests for netbbs.attestation (design doc §18): the
core age/name attestation mechanism -- profile fields, age computation,
attestation records, gating checks, and anti-forgery display formatting.

UI wiring (the [V]erify screen, profile-edit additions, and boards/
channels/file-areas admin screens + enforcement) is a separate,
follow-up piece of this same backlog item -- not covered here, and not
claimed as done. This file covers the core mechanism only.
"""

from __future__ import annotations

from datetime import date

import pytest

from netbbs.attestation import (
    AttestationError,
    ProfileFieldError,
    attest_age,
    attest_name,
    compute_age,
    format_name_for_resource,
    get_attestation,
    get_birthdate,
    get_display_name,
    get_location,
    has_any_verification,
    is_birthdate_visible,
    is_display_name_visible,
    is_location_visible,
    is_verified_badge_visible,
    meets_age,
    meets_name_requirement,
    set_birthdate,
    set_birthdate_visible,
    set_display_name,
    set_display_name_visible,
    set_location,
    set_location_visible,
    set_verified_badge_visible,
)
from netbbs.auth.users import SYSOP_LEVEL, create_user, set_can_verify_identity
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw")


# -- profile fields -----------------------------------------------------


def test_display_name_round_trips(db, alice):
    assert get_display_name(db, alice) is None
    set_display_name(db, alice, "Alice (they/them)")
    assert get_display_name(db, alice) == "Alice (they/them)"


def test_display_name_rejects_reserved_marker(db, alice):
    with pytest.raises(ProfileFieldError, match="cannot contain"):
        set_display_name(db, alice, "Alice =Verified=")


def test_display_name_allows_parentheses(db, alice):
    # Parentheses are allowed -- color is the actual anti-forgery
    # guarantee, not restricting parentheses.
    set_display_name(db, alice, "Alex (they/them)")
    assert get_display_name(db, alice) == "Alex (they/them)"


def test_display_name_rejects_oversized_input(db, alice):
    with pytest.raises(ProfileFieldError, match="cannot exceed"):
        set_display_name(db, alice, "x" * 100)


def test_display_name_visibility_defaults_hidden(db, alice):
    assert is_display_name_visible(db, alice) is False
    set_display_name_visible(db, alice, True)
    assert is_display_name_visible(db, alice) is True


def test_location_round_trips_and_visibility(db, alice):
    assert get_location(db, alice) is None
    set_location(db, alice, "Somewhere, Earth")
    assert get_location(db, alice) == "Somewhere, Earth"
    assert is_location_visible(db, alice) is False
    set_location_visible(db, alice, True)
    assert is_location_visible(db, alice) is True


def test_birthdate_round_trips_and_visibility(db, alice):
    assert get_birthdate(db, alice) is None
    set_birthdate(db, alice, date(1990, 6, 15))
    assert get_birthdate(db, alice) == date(1990, 6, 15)
    assert is_birthdate_visible(db, alice) is False
    set_birthdate_visible(db, alice, True)
    assert is_birthdate_visible(db, alice) is True


def test_birthdate_rejects_future_date(db, alice):
    with pytest.raises(ProfileFieldError, match="future"):
        set_birthdate(db, alice, date(2999, 1, 1))


def test_verified_badge_visibility_defaults_hidden(db, alice):
    assert is_verified_badge_visible(db, alice) is False
    set_verified_badge_visible(db, alice, True)
    assert is_verified_badge_visible(db, alice) is True


# -- age computation (real date math, not year subtraction) -----------------


def test_compute_age_before_birthday_this_year():
    # Born June 15; "today" is June 14 -- birthday hasn't happened yet.
    assert compute_age(date(1990, 6, 15), today=date(2026, 6, 14)) == 35


def test_compute_age_on_birthday():
    assert compute_age(date(1990, 6, 15), today=date(2026, 6, 15)) == 36


def test_compute_age_after_birthday_this_year():
    assert compute_age(date(1990, 6, 15), today=date(2026, 6, 16)) == 36


def test_compute_age_naive_year_subtraction_would_have_been_wrong():
    # current_year - birth_year overestimates age by one for anyone
    # whose birthday hasn't happened yet.
    naive = 2026 - 1990  # 36 -- wrong on 2026-06-14, one year too many
    assert compute_age(date(1990, 6, 15), today=date(2026, 6, 14)) == naive - 1


# -- meets_age: resource-permissive default, fail-closed on missing data ----


def test_meets_age_passes_with_no_gate(db, alice):
    assert meets_age(db, alice, None) is True
    assert meets_age(db, alice, 0) is True


def test_meets_age_fails_closed_with_no_birthdate_at_all(db, alice):
    assert meets_age(db, alice, 18) is False


def test_meets_age_uses_self_reported_birthdate(db, alice):
    set_birthdate(db, alice, date(1990, 1, 1))
    assert meets_age(db, alice, 18) is True
    assert meets_age(db, alice, 200) is False


def test_meets_age_prefers_attested_over_self_reported(db, alice, sysop):
    # Self-report claims an adult; attestation (the trustworthy source)
    # says otherwise -- attested must win.
    set_birthdate(db, alice, date(1990, 1, 1))
    attest_age(db, alice, date(2020, 1, 1), verifier=sysop)
    assert meets_age(db, alice, 18) is False


# -- name-gating: no self-report fallback ------------------------------------


def test_meets_name_requirement_passes_with_no_gate(db, alice):
    assert meets_name_requirement(db, alice, None) is True


def test_meets_name_requirement_fails_without_attestation_even_with_display_name(db, alice):
    set_display_name(db, alice, "Totally Real Name")
    assert meets_name_requirement(db, alice, "verified") is False
    assert meets_name_requirement(db, alice, "verified_and_displayed") is False


def test_meets_name_requirement_passes_once_attested(db, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    assert meets_name_requirement(db, alice, "verified") is True
    assert meets_name_requirement(db, alice, "verified_and_displayed") is True


# -- attestation authorization --------------------------------------------


def test_attest_age_requires_verifier_permission(db, alice):
    bob = create_user(db, "bob", password="hunter2pw")
    with pytest.raises(AttestationError, match="not authorized"):
        attest_age(db, alice, date(1990, 1, 1), verifier=bob)


def test_sysop_can_always_attest_without_the_flag(db, alice, sysop):
    assert sysop.can_verify_identity is False
    attestation = attest_age(db, alice, date(1990, 1, 1), verifier=sysop)
    assert attestation.attested_value == "1990-01-01"


def test_granted_verifier_can_attest(db, alice, sysop):
    bob = create_user(db, "bob", password="hunter2pw")
    bob = set_can_verify_identity(db, bob, True, changed_by=sysop)
    attestation = attest_name(db, alice, "Alice Smith", verifier=bob)
    assert attestation.verifier_user_id == bob.id


def test_attest_age_rejects_future_birthdate(db, alice, sysop):
    with pytest.raises(AttestationError, match="future"):
        attest_age(db, alice, date(2999, 1, 1), verifier=sysop)


def test_attest_name_rejects_blank(db, alice, sysop):
    with pytest.raises(AttestationError, match="blank"):
        attest_name(db, alice, "   ", verifier=sysop)


def test_new_attestation_replaces_the_old_one(db, alice, sysop):
    attest_age(db, alice, date(1990, 1, 1), verifier=sysop)
    attest_age(db, alice, date(1991, 1, 1), verifier=sysop)
    attestation = get_attestation(db, alice, "age")
    assert attestation.attested_value == "1991-01-01"


def test_attestations_are_deliberately_unsigned_for_now(db, alice, sysop):
    # See netbbs.attestation's module docstring: no live-session signing
    # protocol exists yet for any feature, so this is a documented,
    # temporary scope cut, not an oversight.
    attestation = attest_name(db, alice, "Alice Smith", verifier=sysop)
    assert attestation.verifier_fingerprint is None
    assert attestation.signature is None
    assert attestation.link_visible is False


def test_has_any_verification(db, alice, sysop):
    assert has_any_verification(db, alice) is False
    attest_age(db, alice, date(1990, 1, 1), verifier=sysop)
    assert has_any_verification(db, alice) is True


# -- anti-forgery display formatting -----------------------------------------


def test_format_name_for_resource_with_no_requirement_is_plain(db, alice):
    assert format_name_for_resource(db, alice, name_requirement=None) == "alice"


def test_format_name_for_resource_uses_display_name_when_set(db, alice):
    set_display_name(db, alice, "Al")
    assert format_name_for_resource(db, alice, name_requirement=None) == "Al"


def test_format_name_for_resource_verified_only_does_not_show_real_name(db, alice, sysop):
    # "verified" (SysOp can identify) vs "verified_and_displayed" (shown
    # in this resource's rendering) are different display scopes, even
    # though both require the same underlying attestation.
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    assert format_name_for_resource(db, alice, name_requirement="verified") == "alice"


def test_format_name_for_resource_verified_and_displayed_shows_colored_real_name(db, alice, sysop):
    attest_name(db, alice, "Alice Smith", verifier=sysop)
    rendered = format_name_for_resource(db, alice, name_requirement="verified_and_displayed")
    assert rendered.startswith("alice ")
    assert "(=Alice Smith=)" in rendered
    assert "\x1b[" in rendered  # an SGR color code was actually applied


def test_format_name_for_resource_falls_back_when_gate_set_but_not_yet_attested(db, alice):
    # A board can require verified_and_displayed names before anyone on
    # it has actually been verified -- must degrade to the plain name,
    # not crash or show a placeholder.
    assert format_name_for_resource(db, alice, name_requirement="verified_and_displayed") == "alice"


def test_format_name_cannot_be_spoofed_by_an_unrestricted_display_name(db, alice, sysop):
    # The actual anti-forgery property: even though "=" is banned from
    # display_name, prove the *rendered* colored unit is something only
    # a real attestation can produce -- a plain display_name (no
    # attestation at all) never contains the ANSI-colored "(=...=)"
    # pattern, regardless of what plain parentheses text it has.
    set_display_name(db, alice, "alice (not really verified)")
    rendered = format_name_for_resource(db, alice, name_requirement="verified_and_displayed")
    assert "\x1b[" not in rendered
    assert rendered == "alice (not really verified)"
