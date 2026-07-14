"""
Identity: an Ed25519 signing keypair plus the fingerprint derived from it.

Design doc references: §5 (Identity), §7 (message signing), §11 (transport
authentication).
"""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import nacl.encoding
import nacl.hash
import nacl.pwhash
import nacl.secret
import nacl.signing
import nacl.utils
from nacl.exceptions import CryptoError

from netbbs.timeutil import utc_now_iso

# Version tag for the on-disk identity file format. Bump this if the
# format changes, so old identity files can be migrated rather than
# silently misread.
_FILE_FORMAT_VERSION = 1

# Raw Ed25519 public key length in bytes -- fixed by the algorithm, not
# configurable. nacl.signing.VerifyKey has no public SIZE constant to
# reference instead.
_ED25519_PUBLIC_KEY_BYTES = 32

# Fingerprint length in raw bytes before encoding. 20 bytes (160 bits) of
# BLAKE2b output gives a very comfortable safety margin against collision
# for a namespace of node/user identities, while staying short enough to
# be usable in an address a human might actually type
# (e.g. alice@a1b2c3d4e5f6g7h8j9k0).
_FINGERPRINT_BYTES = 20

# RFC 4648 base32, lowercased, with padding stripped — chosen over base64
# because it avoids visually-confusable characters (no 0/O or 1/l/I
# ambiguity issues the way raw base64's mixed case + symbols can have) and
# because it's case-insensitive, which matters for something users will
# type at a BBS prompt.
def _encode_fingerprint(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def fingerprint_from_verify_key(verify_key: nacl.signing.VerifyKey) -> str:
    """
    Derive the short, human-typable fingerprint for a given public key.

    Standalone (not just `Identity.fingerprint`) because naming/looking up
    *other* identities — the common case once accounts are stored with
    just a public key, or once NetBBS Link exists — only ever requires their
    public key, never a full local `Identity` with private key material.
    """
    raw_pubkey = bytes(verify_key)
    digest = nacl.hash.blake2b(
        raw_pubkey,
        digest_size=_FINGERPRINT_BYTES,
        encoder=nacl.encoding.RawEncoder,
    )
    return _encode_fingerprint(digest)


# Argon2id cost parameters for identity file encryption. SENSITIVE tier is
# appropriate for something as long-lived and high-value as a node's or
# user's master identity keypair. Deliberately module-level (not inlined
# in save()) so the test suite can monkeypatch these down to a much
# cheaper tier — see tests/conftest.py — without touching call sites or
# risking production code accidentally picking up a weakened default.
_SAVE_OPSLIMIT = nacl.pwhash.argon2id.OPSLIMIT_SENSITIVE
_SAVE_MEMLIMIT = nacl.pwhash.argon2id.MEMLIMIT_SENSITIVE


class IdentityKind(str, Enum):
    """Whether an Identity represents a node or an individual user.

    Kept as an explicit tag (rather than inferring from context) because
    the two are stored and looked up separately even though the
    underlying key machinery is identical — see design doc §5.
    """

    NODE = "node"
    USER = "user"


class IdentityError(Exception):
    """Raised for any identity load/save/decrypt failure.

    Deliberately a single broad exception type at this layer — callers
    (e.g. the login flow, or node startup) generally just need to know
    "this identity could not be loaded", not the precise cryptographic
    reason, to avoid leaking information useful to an attacker probing
    for valid identity files vs. wrong passphrases.
    """


@dataclass(frozen=True)
class Identity:
    """
    A single Ed25519 signing identity — either a node or a user.

    The fingerprint (derived from the public key) *is* the identity's
    address on NetBBS Link; see design doc §5. This class deliberately does
    not know about node-vs-user addressing format — that's
    `netbbs.identity.addressing`.
    """

    kind: IdentityKind
    label: str  # human-readable name (username, or node's configured name)
    signing_key: nacl.signing.SigningKey  # private half — never serialize directly
    created_at: str  # ISO 8601 UTC timestamp, set at generation time

    # -- derived properties -------------------------------------------------

    @property
    def verify_key(self) -> nacl.signing.VerifyKey:
        """The public half of the keypair."""
        return self.signing_key.verify_key

    @property
    def fingerprint(self) -> str:
        """
        Short, human-typable identifier derived from the public key.

        This is what appears in addresses like `user@node-fingerprint`
        (design doc §5) — never the raw public key itself, which is long
        and includes characters awkward to read aloud or type at a BBS
        prompt.
        """
        return fingerprint_from_verify_key(self.verify_key)

    # -- construction ---------------------------------------------------

    @classmethod
    def generate(cls, kind: IdentityKind, label: str) -> "Identity":
        """Generate a brand-new identity with a fresh random keypair."""
        signing_key = nacl.signing.SigningKey.generate()
        return cls(kind=kind, label=label, signing_key=signing_key, created_at=utc_now_iso())

    # -- signing / verification ------------------------------------------

    def sign(self, message: bytes) -> bytes:
        """Sign `message`, returning the detached signature bytes."""
        return self.signing_key.sign(message).signature

    def verify(self, message: bytes, signature: bytes) -> bool:
        """Verify a signature made by this identity's own key."""
        return verify_signature(self.verify_key, message, signature)

    # -- persistence ------------------------------------------------------

    def save(self, path: Path, passphrase: bytes | None = None) -> None:
        """
        Write this identity to disk.

        If `passphrase` is given, the private key is encrypted at rest
        (Argon2id key derivation + XSalsa20-Poly1305 via nacl.secret,
        i.e. libsodium's standard secretbox construction). If omitted,
        the private key is stored in the clear, restricted to
        owner-only file permissions (0600) as the only protection — this
        is intended for early development/testing only. Headless
        node-startup key handling (so a NetBSD rc.d-managed daemon can
        unlock its own identity without an interactive prompt) is a real
        open problem, not solved here — flagged for a decision before
        this leaves development use.
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        raw_private = bytes(self.signing_key)  # 32-byte seed

        if passphrase is not None:
            salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
            key = nacl.pwhash.argon2id.kdf(
                nacl.secret.SecretBox.KEY_SIZE,
                passphrase,
                salt,
                opslimit=_SAVE_OPSLIMIT,
                memlimit=_SAVE_MEMLIMIT,
            )
            box = nacl.secret.SecretBox(key)
            encrypted = box.encrypt(raw_private)

            # Verify the encryption round-trip before writing anything to
            # disk. Deliberately re-using `box`/`key` already derived
            # above rather than re-running Identity.load() against a
            # written file: that would re-run the (intentionally slow)
            # Argon2id KDF a second time, roughly doubling every save's
            # cost, for a check that decrypting-with-the-same-key doesn't
            # actually need. This still genuinely exercises the
            # encrypt/decrypt round trip — it just doesn't re-derive a key
            # we already have correctly in hand.
            if box.decrypt(encrypted) != raw_private:
                raise IdentityError(
                    "internal error: encrypted private key failed to "
                    "round-trip before writing to disk — refusing to save"
                )

            private_field = {
                "encrypted": True,
                "salt": base64.b64encode(salt).decode("ascii"),
                "ciphertext": base64.b64encode(encrypted).decode("ascii"),
                # Recorded per-file rather than assumed from whatever the
                # module constants currently are, so a file saved under
                # one cost tier stays correctly loadable even if the
                # running code's default tier later changes. Argon2id
                # requires the exact same opslimit/memlimit used at
                # encryption time to re-derive the same key — these
                # aren't just informational.
                "opslimit": _SAVE_OPSLIMIT,
                "memlimit": _SAVE_MEMLIMIT,
            }
        else:
            raw_b64 = base64.b64encode(raw_private).decode("ascii")

            # Same principle for the unencrypted path: confirm the base64
            # encoding we're about to write actually decodes back to the
            # exact bytes we started with, rather than trusting the
            # encode call blindly.
            if base64.b64decode(raw_b64) != raw_private:
                raise IdentityError(
                    "internal error: private key failed to round-trip "
                    "through base64 encoding — refusing to save"
                )

            private_field = {
                "encrypted": False,
                "raw": raw_b64,
            }

        payload = {
            "format_version": _FILE_FORMAT_VERSION,
            "kind": self.kind.value,
            "label": self.label,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
            "private_key": private_field,
        }

        # Write to a temp file and rename, so a crash mid-write can never
        # leave a half-written (and unparseable, or worse, silently
        # truncated-but-valid-looking) identity file behind.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only
        tmp_path.replace(path)

    @classmethod
    def load(cls, path: Path, passphrase: bytes | None = None) -> "Identity":
        """Load an identity previously written by `save()`."""
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentityError(f"could not read identity file: {path}") from exc

        if payload.get("format_version") != _FILE_FORMAT_VERSION:
            raise IdentityError(
                f"unsupported identity file format version in {path} "
                f"(got {payload.get('format_version')!r}, "
                f"expected {_FILE_FORMAT_VERSION!r})"
            )

        private_field = payload["private_key"]
        if private_field["encrypted"]:
            if passphrase is None:
                raise IdentityError(f"identity file {path} is encrypted; passphrase required")
            salt = base64.b64decode(private_field["salt"])
            # Use the cost parameters recorded in the file itself, not
            # whatever the current _SAVE_OPSLIMIT/_SAVE_MEMLIMIT happen to
            # be — Argon2id needs the exact parameters used at encryption
            # time to re-derive the same key, and those could differ from
            # today's defaults (a production tier change, or a test run
            # with monkeypatched values) without this file being touched.
            key = nacl.pwhash.argon2id.kdf(
                nacl.secret.SecretBox.KEY_SIZE,
                passphrase,
                salt,
                opslimit=private_field["opslimit"],
                memlimit=private_field["memlimit"],
            )
            box = nacl.secret.SecretBox(key)
            ciphertext = base64.b64decode(private_field["ciphertext"])
            try:
                raw_private = box.decrypt(ciphertext)
            except CryptoError as exc:
                raise IdentityError(
                    f"could not decrypt identity file {path} — wrong passphrase?"
                ) from exc
        else:
            raw_private = base64.b64decode(private_field["raw"])

        signing_key = nacl.signing.SigningKey(raw_private)

        identity = cls(
            kind=IdentityKind(payload["kind"]),
            label=payload["label"],
            signing_key=signing_key,
            created_at=payload["created_at"],
        )

        # Defensive check: the fingerprint recorded at save time should
        # always match what we recompute now. A mismatch means either
        # file corruption or (far less likely, but worth catching) a
        # format bug — either way, better to fail loudly here than let a
        # node silently start operating under the wrong identity.
        if identity.fingerprint != payload["fingerprint"]:
            raise IdentityError(
                f"fingerprint mismatch loading {path}: "
                f"stored={payload['fingerprint']!r} computed={identity.fingerprint!r}"
            )

        return identity


def load_identity(path: Path, passphrase: bytes | None = None) -> Identity:
    """Module-level convenience wrapper around `Identity.load`."""
    return Identity.load(path, passphrase=passphrase)


def parse_verify_key(text: str) -> nacl.signing.VerifyKey:
    """
    Parse a public key pasted by a human (design doc -- SysOp foundation
    round, the admin account-creation flow) into a `VerifyKey`.

    Accepts either this project's own base64 raw-key form (what
    `users.public_key` stores) or a standard OpenSSH public-key line
    ("ssh-ed25519 AAAA... comment", e.g. the contents of
    ~/.ssh/id_ed25519.pub) — the two forms a SysOp is realistically going
    to have on hand. Raises `IdentityError` for anything malformed,
    matching every other failure mode in this module.
    """
    text = text.strip()
    if text.startswith("ssh-ed25519 "):
        fields = text.split()
        if len(fields) < 2:
            raise IdentityError("malformed ssh-ed25519 line: missing key data")
        try:
            blob = base64.b64decode(fields[1], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise IdentityError("malformed ssh-ed25519 line: key data is not valid base64") from exc
        raw = _decode_openssh_ed25519_pubkey(blob)
    else:
        try:
            raw = base64.b64decode(text, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise IdentityError("public key is not valid base64") from exc

    if len(raw) != _ED25519_PUBLIC_KEY_BYTES:
        raise IdentityError(
            f"an Ed25519 public key is exactly {_ED25519_PUBLIC_KEY_BYTES} bytes "
            f"(got {len(raw)})"
        )
    return nacl.signing.VerifyKey(raw)


def _decode_openssh_ed25519_pubkey(blob: bytes) -> bytes:
    """
    Minimal OpenSSH wire-format decoder: two length-prefixed strings, the
    algorithm name and the raw key. No certificates, no other key types —
    this project only ever stores/verifies Ed25519 keys, so anything else
    is rejected rather than silently accepted and mishandled later.
    """
    offset = 0

    def read_string() -> bytes:
        nonlocal offset
        if offset + 4 > len(blob):
            raise IdentityError("malformed ssh-ed25519 key: truncated field")
        length = int.from_bytes(blob[offset : offset + 4], "big")
        offset += 4
        if offset + length > len(blob):
            raise IdentityError("malformed ssh-ed25519 key: truncated field")
        value = blob[offset : offset + length]
        offset += length
        return value

    algorithm = read_string()
    if algorithm != b"ssh-ed25519":
        raise IdentityError(
            f"unsupported key algorithm {algorithm!r}; only ssh-ed25519 is supported"
        )
    key = read_string()
    if len(key) != _ED25519_PUBLIC_KEY_BYTES:
        raise IdentityError(
            f"malformed ssh-ed25519 key: expected {_ED25519_PUBLIC_KEY_BYTES} raw key bytes"
        )
    return key


def verify_signature(verify_key: nacl.signing.VerifyKey, message: bytes, signature: bytes) -> bool:
    """
    Verify a detached signature against a known public key.

    Standalone function (not just `Identity.verify`) because verifying
    *other* nodes'/users' signatures — the common case in §7's DAG
    processing and §11's transport auth — only ever requires their public
    key, never a full local `Identity` with private key material.
    """
    try:
        verify_key.verify(message, signature)
        return True
    except CryptoError:
        return False
