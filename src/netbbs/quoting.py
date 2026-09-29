"""
Replying with a quote (issue #675): the subject a reply starts with and
the quoted text its body starts with. Shared by a board post's `[R]eply`
and mail's Reply, so the two read the same.

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
    stripped = subject.strip()
    text = stripped if stripped.lower().startswith("re:") else f"Re: {stripped}"
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
