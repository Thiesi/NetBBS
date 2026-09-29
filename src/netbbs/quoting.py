"""
Replying with a quote (issue #675): the subject a reply starts with and
the quoted text its body starts with. Shared by a board post's `[R]eply`
and mail's Reply, so the two read the same. Mail's Forward (issue #822)
takes its subject rule and its forwarded-message header from here too.

Both callers hand `quote_body` the body a reader with color off sees
(`netbbs.rendering.post_body.plain_post_body`) -- mail too, since it
shows color (issue #809); either way it is sanitized again here, since it can come from another
node. Both return text for an editor, never anything rendered.
"""

from __future__ import annotations

from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.width import cut_to_width

# The signature delimiter (`netbbs.signature`): a quote stops at it, since
# a reply answers what was said, not the sign-off under it.
_SIGNATURE_DELIMITER = "\n-- \n"

# A quote is the start of what is answered, not all of it. These keep a
# reply to a long post writable in the line editor, whose caps count the
# quote too (`netbbs.net.composition.edit_line_body`).
MAX_QUOTED_LINES = 40
MAX_QUOTED_BYTES = 8_000
_ELIDED = ">"
_ELISION_NOTE = "> [...]"


def reply_subject(subject: str, *, max_bytes: int) -> str:
    """`subject` with "Re: " in front, unless it already starts with one,
    cut to `max_bytes` of UTF-8 so the prefix cannot push a subject at the
    limit over it."""
    return _prefixed_subject(subject, "Re:", ("re:",), max_bytes=max_bytes)


def forward_subject(subject: str, *, max_bytes: int) -> str:
    """`subject` with "Fwd: " in front, unless it already starts with one
    (or with the "Fw:" some mail programs write), cut to `max_bytes` as
    `reply_subject` is (issue #822)."""
    return _prefixed_subject(subject, "Fwd:", ("fwd:", "fw:"), max_bytes=max_bytes)


def _prefixed_subject(subject: str, prefix: str, already: tuple[str, ...], *, max_bytes: int) -> str:
    stripped = subject.strip()
    text = stripped if stripped.lower().startswith(already) else f"{prefix} {stripped}"
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def quote_body(body: str, *, author: str) -> str:
    """`body` quoted for a reply: "<author> wrote:", then every line of the
    body before its signature with "> " in front (a line already quoted
    becomes "> > ..."), then an empty line to write on.

    Bounded by `MAX_QUOTED_LINES` and `MAX_QUOTED_BYTES`; a cut quote ends
    with "> [...]". Empty when there is nothing to quote.

    Both `body` and `author` may come from another node, so both are
    sanitized here, whatever the caller did first (claude review on
    #786): the quote goes into an editor and from there into what the
    replier sends, and a control sequence in it would reach both."""
    text = sanitize_text(body.replace("\r\n", "\n").replace("\r", "\n"), allow_newlines=True)
    author = sanitize_text(author)
    text = _drop_forwarders_signature(text)
    if _SIGNATURE_DELIMITER in text:
        text = text.rsplit(_SIGNATURE_DELIMITER, 1)[0]
    lines = text.split("\n")
    # Found by index, never by popping the front: a carried body of many
    # thousand blank lines made that quadratic (Codex review on #786).
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return ""
    last = next(i for i in range(len(lines) - 1, -1, -1) if lines[i].strip())
    lines = lines[first:min(last + 1, first + MAX_QUOTED_LINES + 1)]

    quoted: list[str] = []
    size = 0
    for index, line in enumerate(lines):
        row = f"> {line}".rstrip() if line.strip() else _ELIDED
        size += len(row.encode("utf-8")) + 1
        if index >= MAX_QUOTED_LINES or size > MAX_QUOTED_BYTES:
            quoted.append(_ELISION_NOTE)
            break
        quoted.append(row)
    header = f"{cut_to_width(author, 60)} wrote:"
    return "\n".join([header, *quoted, ""])


FORWARD_RULE = "---------- Forwarded message ----------"


def forward_body(body: str, *, sender: str, recipient: str, date: str, subject: str) -> str:
    """`body` as a forward carries it (issue #822): an empty line for the
    forwarder's note, then a header naming whom it was from and to, when
    and under what subject, then a blank line and the body itself, whole.
    `sign_forward` tidies the note line away when nothing was written on it.

    Verbatim, not quoted: a forward passes a letter on for someone else to
    read, so it is not marked as text being answered, and nothing is cut --
    `quote_body` stops at `MAX_QUOTED_LINES` and at the signature, which a
    forward must keep. What the forwarder may send is bounded by the mail
    body limit instead, checked before the letter is reviewed.

    `body` keeps its color pipe codes, so the forward reads as the original
    did; it and every header value may come from another node and are
    sanitized here. Text for an editor, never anything rendered."""
    text = sanitize_text(body.replace("\r\n", "\n").replace("\r", "\n"), allow_newlines=True).strip("\n")
    header = [
        FORWARD_RULE,
        f"From: {sanitize_text(sender)}",
        f"To: {sanitize_text(recipient)}",
        f"Date: {sanitize_text(date)}",
        f"Subject: {sanitize_text(subject)}",
    ]
    return "\n".join(["", *header, "", text])


def _drop_forwarders_signature(text: str) -> str:
    """A forward's note without the signature `sign_forward` put under it,
    so `quote_body` quotes a reply to a forward as it quotes any letter:
    up to one signature, the forwarded letter's writer's, found last. Left
    in, the forwarder's signature would be the one found when the letter
    passed on is unsigned, and the quote would stop at the note."""
    at = text.find(FORWARD_RULE)
    if at < 0:
        return text
    note = text[:at]
    if note.startswith(_SIGNATURE_DELIMITER[1:]):
        note = ""
    elif _SIGNATURE_DELIMITER in note:
        note = note.split(_SIGNATURE_DELIMITER, 1)[0]
    else:
        return text
    note = note.strip("\n")
    return f"{note}\n\n{text[at:]}" if note else text[at:]


def sign_forward(body: str, signature: str | None) -> str:
    """A written forward, ready to send (issue #822): the forwarder's note,
    signed with `signature` if they have one, a blank line, then the
    forwarded letter from its `FORWARD_RULE` on. The signature closes the
    note -- appended at the end it would read as the forwarded letter's
    writer's. Blank lines around the note go, so a forward with none
    starts at its rule.

    Idempotent, as `netbbs.signature.append_signature` is: a resumed
    forward that already carries the signature does not get it twice. A
    body whose rule the forwarder deleted is signed at its end."""
    sig = (signature or "").strip("\n")
    block = f"-- \n{sig}" if sig.strip() else ""
    at = body.find(FORWARD_RULE)
    if at < 0:
        text = body.strip("\n")
        if block and not text.endswith(block):
            text = f"{text}\n{block}"
        return text
    note, rest = body[:at].strip("\n"), body[at:]
    if block and not note.endswith(block):
        note = f"{note}\n{block}" if note else block
    return f"{note}\n\n{rest}" if note else rest
