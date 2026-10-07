"""
Character sets of FTN message text: the `^ACHRS` kludge (FTS-5003).

A message names its set in `^ACHRS: <identifier> <level>`. The identifier
decides; the level is informational, and some software writes the wrong
one ("UTF-8 2"), so it is ignored when reading. A message without the
kludge is read in the network's default set, CP437 unless the SysOp
chose another (design doc §6.8).

Outbound text is written in CP437 when every character of the message --
names, subject and text together, since one kludge covers all of them --
fits, and in UTF-8 otherwise (`choose_outbound_charset`).

Header fields have fixed byte limits (To and From 35, Subject 71), so a
field is cut on a character boundary of its encoding (`truncate_encoded`),
never in the middle of a UTF-8 sequence -- the bug Synchronet issue #1276
records SBBSecho shipping.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

DEFAULT_CHARSET = "cp437"


@dataclass(frozen=True)
class Charset:
    codec: str  # a Python codec name
    kludge: str  # the `^ACHRS` value written for it


CP437 = Charset("cp437", "CP437 2")
UTF8 = Charset("utf-8", "UTF-8 4")

# FTS-5003's registered identifiers, and the legacy ones still in the wild,
# mapped to Python codecs. `IBMPC` is CP437 unless a `CODEPAGE` kludge says
# otherwise; `+7_FIDO` is the Russian Fido name for CP866.
_IDENTIFIERS = {
    "ASCII": "ascii",
    "CP437": "cp437",
    "CP850": "cp850",
    "CP852": "cp852",
    "CP857": "cp857",
    "CP858": "cp858",
    "CP860": "cp860",
    "CP861": "cp861",
    "CP863": "cp863",
    "CP865": "cp865",
    "CP866": "cp866",
    "CP1250": "cp1250",
    "CP1251": "cp1251",
    "CP1252": "cp1252",
    "LATIN-1": "latin-1",
    "LATIN-2": "iso8859-2",
    "LATIN-5": "iso8859-9",
    "LATIN-9": "iso8859-15",
    "ISO-8859-1": "latin-1",
    "KOI8-R": "koi8-r",
    "KOI8-U": "koi8-u",
    "MAC": "mac-roman",
    "UTF-8": "utf-8",
    "IBMPC": "cp437",
    "+7_FIDO": "cp866",
}

# Byte limits including the terminating NUL (FTS-0001), so one less of text.
MAX_NAME_BYTES = 35
MAX_SUBJECT_BYTES = 71


def codec_for_kludge(value: str | None, *, codepage: str | None = None, default: str = DEFAULT_CHARSET) -> str:
    """The Python codec a `^ACHRS` value names, or `default`.

    `codepage` is the value of a `^ACODEPAGE` kludge, which refines the
    legacy `IBMPC` identifier.
    """
    if not value:
        return default
    identifier = value.split()[0].upper()
    if identifier == "IBMPC" and codepage and codepage.strip().isdigit():
        candidate = f"cp{int(codepage.strip())}"
        if _is_codec(candidate):
            return candidate
    return _IDENTIFIERS.get(identifier, default)


def decode(data: bytes, codec: str) -> str:
    """Decode text bytes; anything the codec can't read becomes U+FFFD."""
    return data.decode(codec, errors="replace")


def choose_outbound_charset(*texts: str) -> Charset:
    """CP437 when every one of `texts` fits it, otherwise UTF-8."""
    try:
        for text in texts:
            text.encode("cp437")
    except UnicodeEncodeError:
        return UTF8
    return CP437


def truncate_encoded(text: str, codec: str, max_bytes: int) -> bytes:
    """`text` encoded in `codec`, cut to at most `max_bytes` on a character
    boundary. Characters the codec can't hold become `?`."""
    encoder = codecs.getincrementalencoder(codec)(errors="replace")
    out = bytearray()
    for character in text:
        piece = encoder.encode(character)
        if len(out) + len(piece) > max_bytes:
            break
        out += piece
    return bytes(out)


def _is_codec(name: str) -> bool:
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    return True
