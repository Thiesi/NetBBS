"""
ROT-13 for the message reader (issue #1473, design doc §6.8).

A post or a letter written in ROT-13 -- a spoiler, a punchline, the old
Usenet and FidoNet habit -- is shown decoded while the reader's
`[D]ecode ROT-13` toggle is on. It is display-only: the stored body is
never changed, and nothing about the toggle is stored.

Only ASCII letters turn. Everything a body can carry that is not text is
left exactly as it was, so the decoded body renders the same way: an
escape sequence (SGR color a body keeps) and a ``|NN`` pipe code (whose
two-letter forms would otherwise become different codes).
"""

from __future__ import annotations

import codecs
import re

# An escape sequence (CSI with its parameters and final byte, or ESC and
# one character) or a pipe token: the parts of a body that are not text.
_NOT_TEXT = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b.|\|[0-9A-Za-z]{2}", re.DOTALL)


def rot13(text: str) -> str:
    """`text` with every ASCII letter turned 13 places, escape sequences
    and pipe codes kept as they are. Applying it twice gives `text`."""
    parts: list[str] = []
    last = 0
    for match in _NOT_TEXT.finditer(text):
        parts.append(codecs.encode(text[last:match.start()], "rot13"))
        parts.append(match.group(0))
        last = match.end()
    parts.append(codecs.encode(text[last:], "rot13"))
    return "".join(parts)
