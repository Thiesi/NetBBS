# www.netbbs.org

Developer reference for the public project site. The pages and their
embedded captures are the source of truth. Edit them here and review the diff;
deployment is a separate operation using the procedure below.

Every page is first-contact material. Explain what callers and SysOps can do,
link to the three [handbooks](../docs/README.md), and keep protocol or implementation
detail in the developer references. Do not copy phase-by-phase development
history into the landing page. Claims about compatibility and readiness must
match the handbooks and current issues. The pre-v7.2 Claude Artifacts are
historical copies, not an editing or publishing route.

```
web/
  netbbs-index.html      -> https://www.netbbs.org/
  netbbs-overview.html   -> https://www.netbbs.org/overview.html
  netbbs-boards.html     -> https://www.netbbs.org/boards.html   tour pages: one
  netbbs-files.html      -> https://www.netbbs.org/files.html    subsystem each,
  netbbs-doors.html      -> https://www.netbbs.org/doors.html    linked from both
  netbbs-mrc.html        -> https://www.netbbs.org/mrc.html      main pages
  favicon.ico            -> https://www.netbbs.org/favicon.ico   the icon: a
  favicon.svg            -> https://www.netbbs.org/favicon.svg   terminal with a
  apple-touch-icon.png   -> https://www.netbbs.org/apple-touch-icon.png  prompt
  shots/
    raw-*.txt            raw ANSI as the door wrote it
    shot-*.html          the same capture converted for embedding
```

The three icon files are generated, never edited: the picture is the 16x16
grid in `scripts/website_favicon.py`, and `python scripts/website_favicon.py`
redraws all three from it. Every page links them in its `<head>`. The site
root's `favicon.ico` also serves pages that do not link it, such as the node
pages under `/nodes/`, because a browser asks for it there by itself.

## Rules that are easy to get wrong

- **LF only.** Every page is stored LF. A capture converted to LF and embedded
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

Before deployment, validate the repository copies:

```sh
python scripts/website_check_pages.py --local web/netbbs-index.html web/netbbs-overview.html
python scripts/website_check_wording.py web/netbbs-index.html web/netbbs-overview.html
```

These local checks do not request outbound URLs. Check documentation links
against the checkout too; new handbook links become live after the corresponding
repository changes are published. Inspect rendered pages at desktop and narrow
widths when changing layout or substantial copy.

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

The icon files go the same way, each to its own name at the docroot's top
level. They change only when the picture does.

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
`Database`/`DatabaseLane` — no network, nothing installed. A door cannot be
driven that way: it is a subprocess with its own terminal, so
`website_capture_door_screen.py` reuses `door_gallery.py`'s fixtures and walks
and writes one screen instead of a page of panels. That is not only convenience
— it means a screen the website shows is a screen the presentation review
already covers, under the same label, so the two cannot drift apart silently.

**Every embedded capture is reproducible.** `website_capture_screens.py --list`
names the ten NetBBS screens; `website_capture_chat_mrc.py`,
`website_capture_door_menu.py` and `website_capture_door_profile.py` cover three
more; `website_capture_door_screen.py <door> --list` names the walks the two
bundled games offer. The tour pages add `website_capture_boards_showcase.py`,
`website_capture_files_showcase.py`, `website_capture_doors_showcase.py` and
`website_capture_mrc_showcase.py`, each with its own `--list`; their captures
are `shots/raw-<page>-<name>.txt` (door screens are named after their game). The raw ANSI for each lives in `shots/raw-<name>.txt` and
its converted form in `shots/shot-<name>.html`, so a capture can be regenerated,
diffed, and re-embedded rather than rebuilt by hand.

That was not always true, and the cost of it not being true is worth
remembering: the original gallery came from two Claude Artifacts with no way to
redraw it, so it aged silently. By v7.5.0 the files shot was showing a prose
layout the product had replaced several releases earlier, and every grey on
both pages was a `MUTED_COLOR` the palette no longer used. **A capture nobody
can regenerate is a screenshot of a product you no longer ship.**

Regenerate all fifteen, convert each at the width its capture was drawn
for, then re-embed:

```sh
for s in $(PYTHONPATH=src python scripts/website_capture_screens.py --list); do
  PYTHONPATH=src python scripts/website_capture_screens.py "$s" web/shots/raw-$s.txt
done
PYTHONPATH=src python scripts/website_ansi_to_html.py web/shots/raw-files.txt \
  web/shots/shot-files.html --width 88 --height 60
```

The two bundled games are captured the same way, by the walk's own label.
`--setup` plays keys first, in a launch whose screens are thrown away: it is
how a world gets wear the cached fixture does not have, by playing the door
rather than by writing its tables.

```sh
PYTHONPATH=src python scripts/website_capture_door_screen.py voidrunner \
  "Command Deck" web/shots/raw-voidrunner.txt --fixture played \
  --expect "Command Deck"
PYTHONPATH=src python scripts/website_capture_door_screen.py war_dialer \
  Switchboard web/shots/raw-war_dialer.txt --setup 'X{Bay}N*A'
```

A walk that is given `--fixture` or `--setup` starts somewhere its own keys
did not, so it can end somewhere else: `Command Deck --fixture combat`
stops on the combat screen and would publish under the deck's caption.
The capture is checked against `door_gallery.SHOWS`, and where that has no
marker -- it covers War Dialer and not Voidrunner -- `--expect` is required
and the capture is refused without it.

**Convert at the capture's own terminal width**, not at the width of its
widest row. `website_ansi_to_html.py` emulates a terminal, and a capture
repaints with absolute cursor moves, so the wrong canvas width puts rows in
the wrong places — the chat screen came out 21 rows of 164 columns. Every
screen is drawn at 80 columns except boards and files, which are captured at
88. Height only has to be generous; trailing blank rows are trimmed.

**A door capture comes from a played career.** A fresh one draws every gauge
full or empty, which is the least informative state a gauge can be in. The
Voidrunner shot names the `played` fixture for that reason, and its hull
reads `36/60 Scuffed` rather than full.

`website_check_pages.py` reports each capture's rows × columns and whether it
needs `shot-tall` (17–24 rows do; fewer do not). Recapturing changes those
numbers — the Colors screen gained rows and needed the class, the directory
screen lost them and no longer did — so read that report rather than assuming
the classes still fit.

## House vocabulary

"Link" is always **NetBBS Link**, "boards" **message boards**, "channels"
**chat channels**, "areas" **file areas**. Two senses are exempt and re-applying
the rule blindly breaks them: *board* meaning a whole BBS ("a room full of other
boards"), and *channel* meaning a network connection ("encrypted channel").
`website_check_wording.py` reports what is still unqualified.
