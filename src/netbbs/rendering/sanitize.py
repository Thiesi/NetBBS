"""
Sanitizing untrusted text at the terminal-rendering boundary (design
doc, issue #8).

`sanitize_text()` is the one documented sanitizer every terminal-
visible user- or federated-controlled string (usernames, board/channel/
file-area names and descriptions, post subjects/bodies, chat messages,
uploader labels, filenames, ...) must pass through immediately before
it's interpolated into anything written to a `Session` — never applied
to a whole already-composed line, which would also strip NetBBS's own
trusted ANSI styling (`netbbs.rendering.ansi.colored()` and friends).
Callers are responsible for sanitizing only the untrusted piece, at the
point of interpolation — this module has no notion of "trusted markup"
itself, and doesn't need one: an ESC byte this function removes from
untrusted text can never re-introduce itself by being wrapped in
`colored()` afterward, since `colored()` only *adds* its own SGR codes
around whatever text it's given, never removes anything.

Sanitizes on output, not on storage — the exact split the issue asks
for ("preserve original stored content where appropriate; sanitize on
output"). Nothing here mutates what `create_post`/`create_user`/etc.
write to the database; a moderator or a future Link re-transmission
still sees the original bytes. Only the copy actually reaching a
terminal is ever altered.
"""

from __future__ import annotations

import unicodedata

# The Unicode bidi embedding/override/isolate controls -- the specific,
# well-documented set with real visual text-reordering potential (the
# "Trojan Source" class of attack: make displayed text visually differ
# from its logical byte order). Deliberately not the broader "Format"
# (Cf) category: that also includes zero-width joiners/non-joiners and
# similar characters with legitimate uses in real multilingual/emoji
# text and no visual-reordering capability -- stripping those would
# corrupt legitimate content for no benefit specific to this issue's
# terminal-injection/UI-spoofing threat model. Confirmed with Thiesi as
# the intended scope rather than assumed.
#
# Built from explicit numeric code points via chr(), not as literal
# characters (or even \uXXXX string escapes, which some editors/
# pipelines silently render back into the literal glyph) -- these code
# points are, by design, invisible or nearly so wherever they render,
# which is exactly the property that makes them dangerous in *user*
# content. Having them literally present (and effectively unreviewable
# in a diff) in the sanitizer's own source would be an ironic,
# avoidable risk.
_BIDI_CONTROLS = frozenset(
    chr(code_point)
    for code_point in (
        0x202A,  # LRE - Left-to-Right Embedding
        0x202B,  # RLE - Right-to-Left Embedding
        0x202C,  # PDF - Pop Directional Formatting
        0x202D,  # LRO - Left-to-Right Override
        0x202E,  # RLO - Right-to-Left Override
        0x2066,  # LRI - Left-to-Right Isolate
        0x2067,  # RLI - Right-to-Left Isolate
        0x2068,  # FSI - First Strong Isolate
        0x2069,  # PDI - Pop Directional Isolate
    )
)


def sanitize_text(text: str, *, allow_newlines: bool = False) -> str:
    """
    Strip every character capable of injecting a terminal control
    sequence, spoofing on-screen layout, or visually reordering text,
    from untrusted `text`.

    Removes (silently -- confirmed with Thiesi rather than replacing
    with a visible marker, which would add complexity and an
    unpredictable output length for no clear benefit over just not
    having the dangerous content there at all):

    - Every Unicode "Control" (Cc) character: C0 controls (U+0000-
      U+001F, which includes ESC U+001B -- removing it alone is enough
      to prevent untrusted text from ever forming a CSI/OSC/DCS/APC
      sequence, since all of those require an ESC byte to introduce
      them), DEL (U+007F), and C1 controls (U+0080-U+009F, the 8-bit
      single-byte equivalents some terminals accept as alternate
      encodings of the same sequence-introducers -- stripping only the
      7-bit ESC form would leave this path open).
    - The 9 bidi embedding/override/isolate controls (see
      `_BIDI_CONTROLS`) -- real visual-reordering/spoofing potential,
      not just "unusual".

    Two explicit, narrow exceptions, matching the issue's own
    "except explicitly permitted newline/tab semantics":

    - Tab (U+0009) is always kept -- benign, and reflow/textwrap
      already treat it as ordinary whitespace.
    - Newline (U+000A) is kept only if `allow_newlines=True` -- for
      genuinely multi-line content like a post body, where a real
      embedded newline is legitimate structure (a paragraph break),
      not spoofing. Single-line fields (usernames, board/channel
      names, subjects, chat messages, filenames, ...) should leave
      this at the default `False`: an embedded newline in content
      that's displayed as one line is exactly the kind of thing that
      could fake extra output lines or spoof a prompt.
    - Carriage return (U+000D) is **always** stripped, even with
      `allow_newlines=True` -- unlike `\\n`, a lone `\\r` isn't
      normalized by `Session.write()`'s CRLF handling (which only
      rewrites `\\n`/`\\r\\n`, not a bare `\\r`) and would reach the
      wire as a raw cursor-to-column-0 move, letting untrusted content
      overwrite the start of whatever line it's on.
    """
    kept = []
    for char in text:
        if char == "\t":
            kept.append(char)
            continue
        if char == "\n":
            if allow_newlines:
                kept.append(char)
            continue
        if char == "\r":
            continue
        if unicodedata.category(char) == "Cc":
            continue
        if char in _BIDI_CONTROLS:
            continue
        kept.append(char)
    return "".join(kept)
