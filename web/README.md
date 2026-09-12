# www.netbbs.org

The two pages of the public site, and the captures they embed. **These files are
the source of truth.** Edit them here, review the change as a diff like any
other, then deploy.

Until v7.2.0 the source lived in two published Claude Artifacts instead. That
worked, but updating one required reading the whole 650 KB file back through the
model before the tool would accept a republish — roughly 400 KB of per-character
`<span>` markup per page, every time a sentence changed. The artifacts are no
longer authoritative; treat them as historical copies.

```
web/
  netbbs-index.html      -> https://www.netbbs.org/
  netbbs-overview.html   -> https://www.netbbs.org/overview.html
  shots/
    raw-*.txt            raw ANSI as the door wrote it
    shot-*.html          the same capture converted for embedding
```

## Rules that are easy to get wrong

- **LF only.** Both pages are stored LF. A capture converted to LF and embedded
  in a CRLF page turns every row break into `\r\r\n`, which HTML renders as a
  blank line between every terminal row. Keep one line ending everywhere.
- **Never `write_text` a capture on Windows.** It turns the doors' CR LF into
  CR CR LF, same symptom, different layer. Read and write captures as bytes.
- **One `<span>` per character**, each `width:1ch` — no web font gives
  box-drawing glyphs a uniform advance, so a styled run bends a long border.
  `scripts/website_ansi_to_html.py` does this; do not hand-write a capture.
- **A capture comes from a save with wear on it.** A fresh career or world draws
  every gauge full or empty, which is the least informative state a gauge can
  be in. Play it forward first.
- **Captures never re-flow**: `white-space:pre` plus `overflow:auto`. A capture
  of 17-24 rows needs `shot-tall` or its last rows hide under the 420px cap.

## Deploying

Host `Roanoke.NetWorkXXIII.de` (NetBSD, pkgsrc Apache), account `thiesi`, SSH
via PuTTY's **Pageant** — use `plink`/`pscp`, not OpenSSH. `sudo` is
passwordless; `chown`/`chmod` are not on its `PATH`, so call `/sbin/chown` and
`/bin/chmod` by absolute path. The docroot is not writable by `thiesi`, so
upload to `/tmp` and `sudo mv` into place.

```sh
D=/usr/pkg/share/httpd/htdocs/www.NetBBS.org
plink -batch -agent thiesi@Roanoke.NetWorkXXIII.de \
  "cp $D/index.html /tmp/index.html.<version>.bak"      # keep it out of the docroot
pscp -batch -agent web/netbbs-index.html thiesi@Roanoke.NetWorkXXIII.de:/tmp/index.html.new
plink -batch -agent thiesi@Roanoke.NetWorkXXIII.de \
  "sudo mv /tmp/index.html.new $D/index.html && \
   sudo /sbin/chown root:wheel $D/index.html && sudo /bin/chmod 644 $D/index.html"
```

Verify the **full SHA-256 chain** every time — local, the `/tmp` copy, the
docroot copy, and a `curl` of the live URL must all be the same hash. Then:

```sh
PYTHONPATH=src python scripts/website_check_pages.py     # exits non-zero on a problem
PYTHONPATH=src python scripts/website_check_wording.py --live
```

`website_check_pages.py` audits tag balance, in-page anchors, orphan classes,
charset and mojibake, per-capture size, and every outbound link. It runs a
`self_test()` first, because both of its detectors once passed by checking
nothing.

## Capturing new screens

`scripts/website_capture_*.py` drive real code paths against a real
`Database`/`DatabaseLane` — no network, nothing installed. For the bundled door
games, drive the door as a subprocess through `scripts/door_gallery.py` and
convert with `website_ansi_to_html.py`.

## House vocabulary

"Link" is always **NetBBS Link**, "boards" **message boards**, "channels"
**chat channels**, "areas" **file areas**. Two senses are exempt and re-applying
the rule blindly breaks them: *board* meaning a whole BBS ("a room full of other
boards"), and *channel* meaning a network connection ("encrypted channel").
`website_check_wording.py` reports what is still unqualified.
