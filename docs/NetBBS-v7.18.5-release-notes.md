# NetBBS v7.18.5

A patch release for v7.18.4: a typed field is now edited where it is drawn,
the MRC bridge and the FTN gateway carry the same name on the Node menu as
under Settings, and the browser terminal has an icon. **Nothing migrates:** the node database stays at schema 123, and
every protocol, door API, save and world version is unchanged. Upgrading is a
wheel swap and a restart; rolling back to v7.18.4 is the reverse.

## A typed field is edited where it is drawn (PRs #1197, #1198)

On a screen with numbered fields, choosing a field that takes typed input (by
its number, or by moving the cursor to it and pressing Enter) is meant to put
the cursor in that field's value, with a short hint on the `Choice:` row. Many
fields did, but a number of them still drew a prompt of their own below
`Choice:` instead, and some asked a yes/no question before the typing. With
redraw-in-place on, all of these now edit in place:

- **Door compatibility** (SysOp console, a door's compatibility editor): none
  of its typed fields could edit in place, because the editor never asked for
  the SysOp's redraw setting. All of them can now. **Import JSON** takes the
  path in its field too, and a failed import is shown on the next draw instead
  of on a screen of its own that waited for a key.
- **Settings:** the timestamp **Format** and the three node **Colors**
  (accent, header, clock). Each opens on its current value: a colour reads
  `R,G,B` or `default`, and entering `default` for a colour that already is
  the default now changes nothing without a message.
- **Policy trust → Domains:** the **Domain ID**.
- **Node → Shutdown, Drain and Lock & drain:** the **Delay**.
- **Settings → Managed DNS:** the **Subdomain name**, which opens on the name
  it has.
- **FTN network** passwords (session, packet, AreaFix): typed unseen at the
  field, rather than after the hint on the prompt row.
- **Users → Create:** the **Password** is typed unseen straight into its
  field and once more to confirm, and the **Public key** is pasted into its
  field; an empty answer clears either. Neither asks "Set a password?" or "Add
  a public key?" first any more. A mismatched password or a key that does not
  parse keeps what the draft had and says why.
- **The user editor** (Users → an account): **Level**, **Display name** and
  **Birthdate**.
- **The review after writing a post or mail:** **To** (mail) and **Subject**.
  A subject that is too long is said on the `Choice:` row and the field
  reopens in place.

If the terminal is resized while a field is open, the edit is cancelled, the
value is kept, and the next draw says so.

**Where the old prompt stays:** with redraw-in-place off, the prompt still
opens below `Choice:`. A form taller than the terminal shows just the rows
around the field while it is edited. The user editor and the review keep their
old prompt when the screen does not fit; the review also keeps it when the
value wraps or a row is wider than the terminal. Fields that open a
picker or a list of their own (a trust anchor's node, the MRC room blocklist)
are not typed fields; a bio or signature opens its editor; and the level
ladder's `[N]ame` and `[G]o to level` act on a row of the list and keep their
prompt.

## The same name for the MRC bridge and the FTN gateway (PR #1196)

The MRC bridge was **Inter-BBS chat (MRC)** under Settings, where it is set
up, and **Chat bridge (MRC)** under Node, where its live status is, so nothing
said the two were the same thing. The FTN gateway was **Echomail & netmail
(FTN)** against **FTN mail**. The Node menu now uses the Settings names and
keys, and the status screens their titles:

| | Settings | Node, before | Node, now |
|---|---|---|---|
| MRC | `[I]nter-BBS chat (MRC)` | `[C]hat bridge (MRC)` | `[I]nter-BBS chat (MRC)` |
| FTN | `[E]chomail & netmail (FTN)` | `[F]TN mail` | `[E]chomail & netmail (FTN)` |

The Node menu's `[C]` and `[F]` no longer open anything.

**Menu hints end on a whole word.** Where a menu shows each entry's short
description beside it and the column is too narrow for all of it, the
description used to be cut mid-word ("Banners and m"). It now ends on the last
whole word that fits, and is left out if not even the first word fits; the
full text is in the screen's help.

## The browser terminal has an icon (#1199, PR #1200)

The browser terminal every node serves linked no icon, and the paths browsers
ask for on their own were 404s, so the tab showed a blank icon. Found on
Reliable Link. It now shows the same icon as www.netbbs.org: a small terminal
with a `>_` prompt.

- The page links it, and the web listener serves it where browsers and phones
  look without reading the page: `/favicon.ico`, `/favicon.svg`,
  `/apple-touch-icon.png` and `/apple-touch-icon-precomposed.png`, each with
  its image type.
- **Behind a reverse proxy** that passes everything to the web listener, as
  Reliable Link's Apache does, nothing needs changing: the icon comes through
  with the page. A proxy that forwards only `/`, `/ws` and `/static/` has to
  pass these four paths too.
- The icon is the project's, not your board's own; there is no setting to
  replace it yet.

## Upgrade and rollback

Stop NetBBS, replace the wheel and start it. No migration runs, and no setting
or config key is added. Rolling back to v7.18.4 is the reverse.

## Verification boundaries

- **The release gate:** the full suite on this release's tree, 14,784 passed,
  139 skipped, 0 failed; the timing-sensitive tests 5/5.
- **Tested where it is shown:** the delay, colour, password and public-key
  fields, the user editor's Level and Display name, and the review's To and
  Subject edit at their own row and column; the password reads unseen twice at
  the same place; the user editor and the review open their old prompt with
  redraw-in-place off; a resize during an in-place review edit says so on the
  next draw; the Node menu's `[I]` opens the MRC status screen; a too-long menu
  hint ends on a whole word. The timestamp format, domain ID, subdomain name,
  FTN passwords, the door editor's fields and the birthdate use the same
  in-place path but have no test of their own.
- **Not exercised on a live node yet:** these screens have been checked through
  the test suite's terminal emulation, not on ReLink.
- **The icon:** the four paths return 200 with their image types and the
  exact files, the page links them, and the browser terminal's copies are the
  same bytes as the website's; all three are in the wheel.
- **Website:** two captures on the MRC page still show the old Node title,
  `Chat bridge (MRC)`; they are regenerated on the next website update.
