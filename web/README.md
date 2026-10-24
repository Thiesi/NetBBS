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
  netbbs-doors.html      -> https://www.netbbs.org/doors.html    game overview
  netbbs-mrc.html        -> https://www.netbbs.org/mrc.html      main pages
  netbbs-voidrunner.html -> https://www.netbbs.org/voidrunner.html
  netbbs-war-dialer.html -> https://www.netbbs.org/war-dialer.html
  netbbs-retro-trivia.html -> https://www.netbbs.org/retro-trivia.html
  netbbs-blacksite.html  -> https://www.netbbs.org/blacksite.html
  netbbs-lunar-lander.html -> https://www.netbbs.org/lunar-lander.html
  game-scenes.js        -> https://www.netbbs.org/game-scenes.js
  blacksite-hero.png     -> https://www.netbbs.org/blacksite-hero.png
  lunar-lander-hero.jpg  -> https://www.netbbs.org/lunar-lander-hero.jpg
  favicon.ico            -> https://www.netbbs.org/favicon.ico   the icon: a
  favicon.svg            -> https://www.netbbs.org/favicon.svg   terminal with a
  apple-touch-icon.png   -> https://www.netbbs.org/apple-touch-icon.png  prompt
  shots/
    raw-*.txt            raw ANSI as the door wrote it
    shot-*.html          the same capture converted for embedding
    cast-*.json          a game's animated scene, frame by frame, as it wrote it
    scene-*.json         the same scene as player data, and scene-*.html its poster
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
python scripts/website_check_pages.py --local web/netbbs-*.html
python scripts/website_check_wording.py web/netbbs-*.html
```

The examples above use shell glob expansion. In PowerShell, pass the paths
explicitly instead:

```powershell
$sitePages = (Get-ChildItem web/netbbs-*.html).FullName
python scripts/website_check_pages.py --local $sitePages
python scripts/website_check_wording.py $sitePages
```

These local checks do not request outbound URLs. Check documentation links
against the checkout too; new handbook links become live after the corresponding
repository changes are published. Inspect rendered pages at desktop and narrow
widths when changing layout or substantial copy.

The site is deployed from `main`, never from a branch or a working tree, so
what is live is what `main` says. Where it is hosted, how to reach that host,
and the exact copy-and-install steps are operational details. They live in the
maintainer's private operations notes, not in this public repository.

The individual game pages also need `game-scenes.js` and the two hero images
at the site root, under the exact names in the map above. Verify their hashes
alongside the HTML. The `shots/` files are source captures embedded into HTML;
they do not need to be served for the game pages to work.

A deploy backs up each page it replaces, outside the web-served directory. The
icon files go the same way as the pages, each to its own name at the site's
top level; they change only when the picture does.

Verify the **full SHA-256 chain** every time — local, each copy made during
the deploy, and a `curl` of the live URL must all be the same hash. Then:

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

The landing page's main menu is Harbor Lights' own art, drawn by
`scripts/website_harbor_lights_menu.py` (`python scripts/website_harbor_lights_menu.py
out.ans` writes it) and installed as slot art: NetBBS draws keeper's values into its
tokens and keeper's `[T]opics`, which the art doesn't draw, into its `{menu}` row. Its
full blocks are drawn as cells painted in their colour, which a terminal shows the
same way and the page's tall lines don't split into stripes.

The chat capture runs Baud for real: `BotRuntime` starts the bundled bot as
its own process, and the caller's lines are typed into the channel, so every
`[BOT]` line on the landing page is an answer Baud actually gave. The
channel's clock is pinned to one evening and each line gets its own minute, so a
recapture shows the same times whenever it runs; the bot host's clock, which
stamps a private `(to you)` answer, is pinned with it. The top ten for `!top`
comes from a made-up third-party door, dropped through the door outbound hook
before Baud starts: the bundled games do not report game events yet. No
reminder is shown: Baud's confirmation names the time it falls due from Baud's
own clock, which runs in its own process and cannot be pinned.

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

## Individual game pages

The three bundled games have dedicated pages with their gameplay galleries
and scene players. Blacksite and Lunar Lander have development previews:
describe planned play, label the status, and never manufacture a screenshot.
Their images are copied without modification from
`Thiesi/Blacksite` (`docs/assets/blacksite-banner.png`) and
`Thiesi/LunarLander` (`assets/lunar-lander-social.jpg`). Both repositories are
BSD-2-Clause licensed. The concept-art pages declare
`<meta name="netbbs-capture-kind" content="concept-art">`; the page checker
requires labelled artwork and no terminal captures for that declaration.

Capture the real launch splashes through the games' own `play_splash` functions:

```sh
PYTHONPATH=src python scripts/website_capture_game_splash.py voidrunner web/shots/raw-voidrunner-splash.txt
PYTHONPATH=src python scripts/website_capture_game_splash.py war_dialer web/shots/raw-war_dialer-splash.txt
PYTHONPATH=src python scripts/website_capture_game_splash.py retro_trivia web/shots/raw-retro_trivia-splash.txt
PYTHONPATH=src python scripts/website_capture_retro_trivia.py question web/shots/raw-retro_trivia-question.txt
```

Convert each with `website_ansi_to_html.py --width 80 --height 24` and embed
the fragment in its page. The splash helper records the held final frame,
before teardown clears it, using the same terminal-recorder approach as the
scene helper. Retro Trivia's question capture launches the subprocess and
chooses a five-question round: questions are random, so recapturing may show a
different question. Keep its raw output with the converted fragment.

Run the local page and wording checkers over **all** `web/netbbs-*.html` files
when changing a game page; the live checkers include all five game URLs.
`game-scenes.js` is the game pages' shared player; the older pages retain their
own scripts for unrelated interactions.

`doors.html` is the game overview: one real launch splash per bundled game,
one labelled hero image per development project, a short description and a
link to each dedicated page. Keep gameplay galleries and scene players on the
individual pages. The landing page keeps at most one still per game in its
general screenshot gallery. Every public page links to the overview.

## Scenes: animated captures

The bundled games play short full-screen scenes during play, and their individual
game pages replay them. A scene is recorded as a **cast**: every
frame's raw ANSI, with the wait the game asked for after it.
`website_capture_door_scene.py` plays the scene through the game's own `play_*`
function at 80x24 in truecolor. Only the terminal around it is replaced: stdout
records, and the frame wait reports that no key came. `--list` names every scene.
Voidrunner's Viewport pictures are recorded the same way through
`show_viewport_picture`. They idle until a key for up to 21 seconds, so a cast
keeps the opening and the first nine seconds (`VIEWPORT_FRAMES`).

```sh
PYTHONPATH=src python scripts/website_capture_door_scene.py voidrunner jump \
  web/shots/cast-voidrunner-jump.json
python scripts/website_scene_to_html.py web/shots/cast-voidrunner-jump.json \
  web/shots/scene-voidrunner-jump.json --poster web/shots/scene-voidrunner-jump.html
```

`website_scene_to_html.py` paints each frame with the same terminal emulator
as the stills and keeps only the cells each frame changes. That data goes into
the page as `<script type="application/json" id="scene-<door>-<scene>">`. The
page's player script draws the same one-`<span>`-per-cell grid and applies
the frames at the game's timing. It cycles through the scenes listed in its
chips while it is on screen, and pauses when scrolled away. With reduced motion
it never starts by itself. The poster, the scene's last frame, is the
`<pre>`'s static content. A page shows it until the player starts, and it is
all a page shows with scripts off.

- **A scene needs `line-height:1.2`, not the stills' 1.55.** Its pictures are
  half blocks, and the taller rows split every pixel into stripes.
- **Re-record after vendoring a new game release.** A cast is that release's
  bytes, so `VENDORED.json` and the casts change together.

## House vocabulary

"Link" is always **NetBBS Link**, "boards" **message boards**, "channels"
**chat channels**, "areas" **file areas**. Two senses are exempt and re-applying
the rule blindly breaks them: *board* meaning a whole BBS ("a room full of other
boards"), and *channel* meaning a network connection ("encrypted channel").
`website_check_wording.py` reports what is still unqualified.
