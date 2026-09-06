"""Audit the published website pages. Run after every deploy.

    python scripts/website_check_pages.py
    python scripts/website_check_pages.py --local deploy/index.html deploy/overview.html

Checks, in order of how often they have actually caught something:

* **Terminal captures.** Rows and columns per capture, whether a capture that
  needs the taller frame has it, and whether any real markup leaked into one.
  Also `CR CR LF`, which HTML renders as a blank line between every terminal
  row -- the symptom of a capture written in text mode on Windows and then
  embedded in a page stored with CRLF.
* **Classes with no CSS rule** -- catches a modifier that was applied in the
  markup but never defined, and dead classes left behind by an earlier build.
* **Tag balance** and **every in-page anchor resolving**.
* **Encoding**: the charset meta must be present, because Apache serves these
  with no charset of its own; mojibake means it was lost.
* **Outbound links** still resolving.

Exits non-zero if anything failed, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

PAGES = {
    "index.html": "https://www.netbbs.org/",
    "overview.html": "https://www.netbbs.org/overview.html",
}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

# A capture of 17-24 rows is given the taller frame so it shows whole; the
# long transcripts scroll inside the default box, as they always have.
TALL_MIN, TALL_MAX = 17, 24

# UTF-8 read as latin-1: an em dash becomes "â€”", an apostrophe
# "â€™", a non-breaking space "Â ". The lead byte is always
# U+00C2/C3 (two-byte sequences) or U+00E2/E3 (three-byte), followed by a
# continuation byte that lands in Latin-1 Supplement or, for 0x80-0x9F,
# in the Windows-1252 punctuation block.
MOJIBAKE = re.compile("[ÂÃâã]"
                      "[-¿–—‘-”€™šžŒœ]")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[tuple[str, int]] = []
        self.problems: list[str] = []
        self.ids: set[str] = set()
        self.anchors: list[str] = []
        self.classes: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if a.get("href", "").startswith("#"):
            self.anchors.append(a["href"][1:])
        for cls in (a.get("class") or "").split():
            self.classes.add(cls)
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for unclosed, line in self.stack[i + 1:]:
                    self.problems.append(f"<{unclosed}> opened line {line} never closed")
                del self.stack[i:]
                return
        self.problems.append(f"stray </{tag}> at line {self.getpos()[0]}")


def captures(text: str):
    """(title, class tokens, body) for each gallery capture, in document order.

    Anchored on the `term-body` block and walking backwards for its enclosing
    div and title, so reordering or adding class tokens cannot make a capture
    invisible to the audit -- which would let every terminal check silently
    pass on nothing.
    """
    for body_match in re.finditer(r'<pre class="term-body">(.*?)</pre>', text, re.S):
        before = text[:body_match.start()]
        # The *enclosing* shot, not the `term-bar` div that sits between it
        # and the body: match on the class token, never on position.
        classes: set[str] = set()
        for div_match in re.finditer(r'<div[^>]*\sclass="([^"]*)"', before):
            tokens = set(div_match.group(1).split())
            if "shot" in tokens:
                classes = tokens
        if not classes:
            continue        # the hero terminal, which is not a gallery shot
        title = "(untitled)"
        for title_match in re.finditer(r'<span class="term-title">(.*?)</span>', before, re.S):
            title = title_match.group(1)
        yield title, classes, body_match.group(1)


def check(label: str, raw: bytes) -> bool:
    text = raw.decode("utf-8")
    ok = True
    print(f"===== {label}  ({len(raw)} bytes) =====")

    charset = re.search(r'<meta charset="?([\w-]+)', text[:400])
    mojibake = MOJIBAKE.findall(text)
    print(f"  charset meta        : {charset.group(1) if charset else 'MISSING'}")
    print(f"  mojibake sequences  : {len(mojibake)}")
    ok &= bool(charset) and not mojibake

    page = Page()
    page.feed(text)
    for unclosed, line in page.stack:
        if unclosed not in ("html", "body"):
            page.problems.append(f"<{unclosed}> opened line {line} never closed")
    print(f"  tag problems        : {len(page.problems)}")
    for p in page.problems[:10]:
        print(f"      ! {p}")
    ok &= not page.problems

    missing = sorted({a for a in page.anchors if a and a not in page.ids})
    print(f"  in-page anchors     : {len(page.anchors)} | unresolved: {missing or 'none'}")
    ok &= not missing

    css = "\n".join(re.findall(r"<style>(.*?)</style>", text, re.S))
    styled = set(re.findall(r"\.([A-Za-z][\w-]*)", css))
    unstyled = sorted(c for c in page.classes if c not in styled)
    print(f"  classes with no rule: {unstyled or 'none'}")
    ok &= not unstyled

    shots = list(captures(text))
    print(f"  terminal captures   : {len(shots)}")
    if not shots:
        print("      ! no captures found -- every terminal check below was skipped")
        ok = False
    for title, classes, body in shots:
        lines = body.split("\n")
        rows = len(lines)
        cols = max(len(re.findall(r"<span\b", line)) for line in lines)
        # Real markup inside a capture: strip the per-character spans, and
        # anything tag-shaped that survives was never escaped.
        stray = len(re.findall(r"<[a-zA-Z/]", re.sub(r"</?span[^>]*>", "", body)))
        doubled = body.count("\r\r\n")
        tall_wanted = TALL_MIN <= rows <= TALL_MAX
        tall_have = "shot-tall" in classes
        flags = []
        if stray:
            flags.append(f"{stray} stray markup")
        if doubled:
            flags.append(f"{doubled} CR CR LF (doubled linefeeds)")
        if tall_wanted != tall_have:
            flags.append("shot-tall " + ("missing" if tall_wanted else "not needed"))
        status = "; ".join(flags) or "ok"
        ok &= not flags
        print(f"      {title:<36} {rows:>2}r x {cols:>3}c  {status}")

    print()
    return ok


def self_test() -> None:
    """Prove the detectors still detect.

    The first version of this file shipped a mojibake class containing
    U+00A2 where U+00E2 was meant -- a lookalike. It matched nothing, so
    every run reported a clean page and the audit was worthless. A check
    that can pass by doing nothing needs a canary.
    """
    corrupt = "an em dash â€” and an apostrophe â€™s"
    if not MOJIBAKE.search(corrupt):
        raise AssertionError("MOJIBAKE no longer matches known-bad text")
    if MOJIBAKE.search("an em dash — and an apostrophe’s"):
        raise AssertionError("MOJIBAKE matches correctly-encoded text")
    sample = ('<div class="term shot shot-tall"><div class="term-bar">'
              '<span class="term-title">t</span></div>'
              '<pre class="term-body">x</pre></div>')
    found = list(captures(sample))
    if [(t, sorted(c)) for t, c, _ in found] != [("t", ["shot", "shot-tall", "term"])]:
        raise AssertionError(f"capture discovery broke: {found}")


def main() -> int:
    self_test()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", nargs="*", type=Path,
                        help="check these files instead of the live URLs")
    parser.add_argument("--skip-links", action="store_true")
    args = parser.parse_args()

    ok = True
    if args.local:
        for path in args.local:
            ok &= check(str(path), path.read_bytes())
        return 0 if ok else 1

    texts = []
    for label, url in PAGES.items():
        raw = urllib.request.urlopen(url, timeout=30).read()
        texts.append(raw.decode("utf-8"))
        ok &= check(label, raw)

    if not args.skip_links:
        print("===== outbound links =====")
        # Unescape first: a query string is written `&amp;` in valid HTML, and
        # requesting it raw asks for a different URL with an `amp;` parameter.
        for url in sorted({html.unescape(u) for t in texts for u in
                           re.findall(r'href="(https?://[^"]+)"', t)}):
            try:
                req = urllib.request.Request(
                    url, method="HEAD", headers={"User-Agent": "netbbs-site-check"})
                code: object = urllib.request.urlopen(req, timeout=25).status
            except Exception as exc:                       # noqa: BLE001
                code, ok = f"ERROR {exc}", False
            print(f"  {code}  {url}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
