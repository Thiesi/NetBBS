"""Tests for netbbs.identity — keypair generation, signing, persistence."""

from __future__ import annotations

import os

import pytest

from netbbs.identity import format_address, parse_address
from netbbs.identity.addressing import AddressError
from netbbs.identity.keys import Identity, IdentityError, IdentityKind, verify_signature


# -- generation & basic properties --------------------------------------


def test_generate_produces_distinct_identities():
    a = Identity.generate(IdentityKind.NODE, "roanoke")
    b = Identity.generate(IdentityKind.NODE, "roanoke")
    assert a.fingerprint != b.fingerprint


def test_fingerprint_is_stable_for_same_key():
    identity = Identity.generate(IdentityKind.USER, "thiesi")
    assert identity.fingerprint == identity.fingerprint  # deterministic, not resampled
    assert len(identity.fingerprint) > 0
    assert identity.fingerprint == identity.fingerprint.lower()


def test_node_and_user_kind_are_distinct():
    node = Identity.generate(IdentityKind.NODE, "roanoke")
    user = Identity.generate(IdentityKind.USER, "thiesi")
    assert node.kind == IdentityKind.NODE
    assert user.kind == IdentityKind.USER


# -- signing & verification ----------------------------------------------


def test_sign_and_verify_roundtrip():
    identity = Identity.generate(IdentityKind.USER, "thiesi")
    message = b"post:hello the link"
    signature = identity.sign(message)
    assert identity.verify(message, signature)


def test_verify_signature_standalone_function():
    identity = Identity.generate(IdentityKind.USER, "thiesi")
    message = b"post:hello the link"
    signature = identity.sign(message)
    assert verify_signature(identity.verify_key, message, signature)


def test_verify_rejects_tampered_message():
    identity = Identity.generate(IdentityKind.USER, "thiesi")
    signature = identity.sign(b"original message")
    assert not identity.verify(b"tampered message", signature)


def test_verify_rejects_wrong_key():
    a = Identity.generate(IdentityKind.USER, "alice")
    b = Identity.generate(IdentityKind.USER, "bob")
    signature = a.sign(b"hello")
    assert not verify_signature(b.verify_key, b"hello", signature)


# -- persistence: unencrypted --------------------------------------------


def test_save_and_load_roundtrip_unencrypted(tmp_path):
    original = Identity.generate(IdentityKind.NODE, "roanoke")
    path = tmp_path / "node.identity.json"
    original.save(path)

    loaded = Identity.load(path)
    assert loaded.fingerprint == original.fingerprint
    assert loaded.kind == original.kind
    assert loaded.label == original.label

    message = b"proof of continuity"
    assert loaded.verify(message, original.sign(message))


@pytest.mark.skipif(
    os.name != "posix",
    reason="os.chmod cannot express owner-only permission bits on Windows "
    "(no POSIX permission model); this test is only meaningful on the "
    "project's actual POSIX targets (NetBSD/Linux).",
)
def test_save_sets_owner_only_permissions(tmp_path):
    identity = Identity.generate(IdentityKind.NODE, "roanoke")
    path = tmp_path / "node.identity.json"
    identity.save(path)

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


# -- persistence: encrypted -----------------------------------------------


def test_save_and_load_roundtrip_encrypted(tmp_path):
    original = Identity.generate(IdentityKind.USER, "thiesi")
    path = tmp_path / "user.identity.json"
    passphrase = b"correct horse battery staple"
    original.save(path, passphrase=passphrase)

    loaded = Identity.load(path, passphrase=passphrase)
    assert loaded.fingerprint == original.fingerprint


def test_save_encrypted_detects_roundtrip_corruption(tmp_path, monkeypatch):
    """
    Regression test for save()'s round-trip verification: if the
    encrypt/decrypt round trip doesn't return the original bytes, save()
    must refuse to write anything at all rather than silently persisting
    a bad file. Simulates a hypothetical encoding bug by forcing decrypt()
    to return the wrong bytes.
    """
    import nacl.secret

    identity = Identity.generate(IdentityKind.USER, "thiesi")
    path = tmp_path / "user.identity.json"

    def broken_decrypt(self, ciphertext, *args, **kwargs):
        return b"not the original plaintext bytes"

    monkeypatch.setattr(nacl.secret.SecretBox, "decrypt", broken_decrypt)

    with pytest.raises(IdentityError):
        identity.save(path, passphrase=b"whatever")

    assert not path.exists()


def test_save_unencrypted_detects_roundtrip_corruption(tmp_path, monkeypatch):
    """Same regression test, for the unencrypted (plain base64) path."""
    import base64 as base64_module

    identity = Identity.generate(IdentityKind.NODE, "roanoke")
    path = tmp_path / "node.identity.json"

    def broken_b64decode(data, *args, **kwargs):
        return b"not the original bytes"

    monkeypatch.setattr(base64_module, "b64decode", broken_b64decode)

    with pytest.raises(IdentityError):
        identity.save(path, passphrase=None)

    assert not path.exists()


def test_load_encrypted_without_passphrase_fails(tmp_path):
    identity = Identity.generate(IdentityKind.USER, "thiesi")
    path = tmp_path / "user.identity.json"
    identity.save(path, passphrase=b"secret")

    with pytest.raises(IdentityError):
        Identity.load(path)


def test_load_encrypted_with_wrong_passphrase_fails(tmp_path):
    identity = Identity.generate(IdentityKind.USER, "thiesi")
    path = tmp_path / "user.identity.json"
    identity.save(path, passphrase=b"correct passphrase")

    with pytest.raises(IdentityError):
        Identity.load(path, passphrase=b"wrong passphrase")


def test_load_missing_file_fails(tmp_path):
    with pytest.raises(IdentityError):
        Identity.load(tmp_path / "does-not-exist.json")


def test_load_corrupted_file_fails(tmp_path):
    path = tmp_path / "corrupted.json"
    path.write_text("not valid json{{{")
    with pytest.raises(IdentityError):
        Identity.load(path)


# -- addressing -------------------------------------------------------------


def test_format_and_parse_address_roundtrip():
    identity = Identity.generate(IdentityKind.NODE, "roanoke")
    address_str = format_address("thiesi", identity.fingerprint)
    parsed = parse_address(address_str)
    assert parsed.user == "thiesi"
    assert parsed.node_fingerprint == identity.fingerprint
    assert str(parsed) == address_str


def test_parse_address_rejects_missing_at_sign():
    with pytest.raises(AddressError):
        parse_address("thiesi-no-at-sign")


def test_parse_address_uses_last_at_sign_as_delimiter():
    """
    Regression test: parse_address must split on the *last* @, not the
    first, since fingerprints are guaranteed @-free but a malformed or
    (post-relaxation) legitimate username might not be. An address with
    two @ signs should attribute the failure to the username part, not
    misread part of it as the fingerprint.
    """
    with pytest.raises(AddressError) as exc_info:
        parse_address("alpha@beta@abcdefgh")
    # Should fail on the (invalid) username "alpha@beta", not silently
    # misinterpret "beta@abcdefgh" as a fingerprint.
    assert "alpha@beta" in str(exc_info.value)


def test_format_address_rejects_invalid_user_part():
    with pytest.raises(AddressError):
        format_address("Thiesi With Spaces!", "abcdefgh")


def test_format_address_rejects_invalid_fingerprint():
    with pytest.raises(AddressError):
        format_address("thiesi", "not a valid fingerprint!!")
