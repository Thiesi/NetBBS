"""The one test for "is this text a number `int()` will take".

`str.isdigit()` is true for "²", "³", "①" and other characters `int()`
rejects with `ValueError`; "²" is AltGr+2 on a German keyboard, so a
caller types it by accident. Guarding `int()` with `isdigit()` alone ended
sessions at a numeric prompt (#859, #902, #928). `isdecimal()` does not
crash but still takes fullwidth and Arabic-Indic digits, which no prompt
here means to accept. Test with this instead.
"""

from __future__ import annotations


def is_ascii_number(text: str) -> bool:
    """True when `text` is one or more ASCII digits `0-9` and nothing else."""
    return bool(text) and text.isascii() and text.isdigit()
