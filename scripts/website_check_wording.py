"""List every unqualified product noun in the website's prose.

    python scripts/website_check_wording.py deploy/index.html deploy/overview.html
    python scripts/website_check_wording.py --live

House vocabulary: "Link" is always **NetBBS Link**, "boards" always **message
boards**, "channels" always **chat channels**, "areas" always **file areas**.

This reports rather than rewrites, because two senses are exempt and a blind
substitution changes what the sentence means:

* **board = a whole BBS** -- "other boards already talk on", "a room full of
  other boards", "a genuinely full-featured board", "Running the board day to
  day", "Inbound lines carry the sending board".
* **channel = a network connection** -- SSH's "encrypted channel", NetBBS
  Link's "Noise-encrypted channel", and the lowercase MRC "hub link".

Terminal captures, CSS, JavaScript and attribute values are never searched:
a capture is real screen output and must not be reworded, and a class or href
is not prose.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

PAGES = {
    "index.html": "https://www.netbbs.org/",
    "overview.html": "https://www.netbbs.org/overview.html",
}
PROTECTED = re.compile(r"<pre\b.*?</pre>|<style>.*?</style>|<script>.*?</script>", re.S)
TAG = re.compile(r"<[^>]+>")
# "Link" is a proper noun, so only a capitalised occurrence is the product --
# a hyperlink, a serial link and the MRC hub link are ordinary English. The
# other three are common nouns whose case follows their position in a
# sentence, so `Boards`/`Channels`/`Areas` at the head of a heading or clause
# are matched too; those are exactly the ones a case-sensitive scan misses.
TERMS = re.compile(r"\bLink\b|(?i:\b(?:boards?|channels?|areas?)\b)")
# Already-qualified phrasings, and the two exempt senses.
#
# Each entry must span the noun it qualifies *including its plural*, because
# an occurrence counts as qualified only when the term match sits inside one
# of them. Words are joined so that a hyphen, a run of spaces, or a line
# break between them all match: the page source wraps prose freely, so a
# phrase split across a line and its indent is still the qualified phrase.
_PHRASES = [
    # the house forms
    "NetBBS Link", "message boards?", "chat channels?", "file areas?",
    "bulletin boards?",
    # board = a whole BBS, not a message board
    "other boards?", "your board", "the sending board",
    "the board day to day", "full featured board", "boards already",
    "owned boards", "A board has always",
    # channel = a network connection, not a chat channel
    "encrypted channel", "hub link",
]
QUALIFIED = re.compile(
    "|".join(r"[\s-]+".join(phrase.split()) for phrase in _PHRASES), re.I)


def prose(text: str):
    """Yield each run of page prose, tags and captures excluded."""
    blocked = sorted([(m.start(), m.end()) for m in PROTECTED.finditer(text)]
                     + [(m.start(), m.end()) for m in TAG.finditer(text)])
    merged: list[list[int]] = []
    for a, b in blocked:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    cursor = 0
    for a, b in merged:
        if a > cursor:
            yield text[cursor:a]
        cursor = b
    if cursor < len(text):
        yield text[cursor:]


def check(label: str, text: str) -> int:
    print("=" * 78)
    print(label)
    print("=" * 78)
    flagged = 0
    for chunk in prose(text):
        # Spans of every already-qualified or exempt phrase in this run of
        # prose. An occurrence counts as qualified only when it sits *inside*
        # one of them: a nearby "message boards" must not silence a bare
        # "channels" a few words later.
        safe = [m.span() for m in QUALIFIED.finditer(chunk)]
        for m in TERMS.finditer(chunk):
            start, end = m.span()
            if any(a <= start and end <= b for a, b in safe):
                continue
            lo, hi = max(0, start - 70), min(len(chunk), end + 70)
            context = " ".join(chunk[lo:hi].split())
            print(f"  [{m.group(0):<9}] ...{context}...")
            flagged += 1
    print(f"  -> {flagged} unqualified use(s)")
    print()
    return flagged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pages", nargs="*", type=Path)
    parser.add_argument("--live", action="store_true", help="fetch the published pages")
    args = parser.parse_args()

    total = 0
    if args.live or not args.pages:
        for label, url in PAGES.items():
            total += check(label, urllib.request.urlopen(url, timeout=30).read().decode("utf-8"))
    for path in args.pages:
        total += check(str(path), path.read_bytes().decode("utf-8"))

    print("Review each one: qualify it, or confirm it is one of the exempt senses.")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
