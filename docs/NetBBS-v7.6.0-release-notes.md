# NetBBS v7.6.0

The MRC bridge grows a room directory, chat help becomes readable, and
the field screens finally line their values up.

This one is mostly about rooms and columns. A SysOp bridging their node
onto MRC could exchange messages but had no way to see what rooms were
out there; now the picker asks the hub. Chat `/help` stopped being a wall
of text that ran off the top of the screen. And the draft editors that
#529 rebuilt got the other half of that work: values aligned into a
column, and typed fields edited where they stand.

**This release migrates.** The node database goes from schema 65 to 66
for one additive column. Rolling back therefore needs a restore, not just
the previous wheel — see *Upgrade and rollback*. Voidrunner careers stay
at save schema 2 and War Dialer worlds at world schema 10.

## An MRC room directory (#574)

7.5.0 could already *ask*: `/rooms` sent the hub a `LIST` and printed
the reply back to whoever asked. What it could not do was use the
answer. The room picker now parses that reply into rows it offers you —
room names, how many people are in them, and their topics, including
rooms whose topic is empty — instead of leaving the picker to list only
what it had happened to observe from openings and chatter.

- The lobby is offered even when the SysOp has preconfigured other
  rooms, and the directory refreshes after joining or reconnecting
  rather than going stale.
- Background discovery runs on the shared refresh limit, so it never
  spends a caller's interactive message allowance; an explicit `/rooms`
  still costs a normal command. Asking twice while a reply is pending
  reuses that reply rather than extending its expiry — per caller: two
  different callers still send a request each.
- Changing hub settings clears both discovery caches and reloads what
  the node retained locally, so a retained room shows the topic it has
  now — including a topic that was cleared — rather than an older
  snapshot.

**Who is in the room.** The status bar counted local users only. Local
participants and local away counts are now shown separately from the MRC
roster, with unknown and stale states of their own. Nickname ownership is
scoped to the room whose roster is being updated, so the same nickname in
two rooms no longer confuses one for the other, and leaving a room stops
counting a cached bare local nickname as a remote user.

**Colors survive the wire.** A sender's foreground color used to
disappear when the wire prefix was stripped. It is now preserved in live
chat and in scrollback, with quieter network provenance, and it honours
the existing color preference. This is the one thing in the release that
migrates: `channel_messages` gains an `mrc_nick_color` column, additive,
constrained so that only rows from MRC can carry one. Stored message text
and identities are untouched.

**A refusal you can still read.** Opening a room the node will not open —
a level gate, a blocklist, a capacity limit — put an error on screen that
the next redraw wiped. The refusal now survives redraw and Ctrl-L, so you
can read why while looking at the picker that refused you.

Tab completion covers MRC commands, room names and recipients, and
excludes unmapped rooms when opening is disabled.

## Chat help you can read (#576)

`/help` in chat produced a dense stream that ran past the top of the
terminal. It is now paginated inside the live chat viewport, reserving
the pinned chat rows rather than drawing over them, with syntax and
description in separate columns and command names, parameters,
descriptions, headings and rules each colored distinctly. Long syntax
wraps at a separator that means something.

Permission-aware help is unchanged, and worth stating precisely: bare
`/help` lists what you can use, the same predicate Tab completion
applies. `/help <command>` answers for any command by name, deliberately
— visibility gating is a suggestion filter, not an authorization check,
so asking about a command explicitly is not treated as a passive listing
a non-moderator should be nudged away from.

## Fields in a column (#528, #529)

The remaining editor slices. Label widths are now shared across a
screen's sections, so a screen reads as a table rather than as a ragged
list, and a seeded edit is positioned by the wrapped screen row it is
actually on — which is what lets a typed field be edited in place instead
of being re-asked. Scrolling prompts and validation feedback survive it.

**In-place editing follows the caller's own redraw preference.** Both
paths that create an account for a person — signing up, and a SysOp
creating one — set it on, so anyone who joined since that landed gets the
new behaviour. Accounts predating it have it unset, which resolves to
off: they keep the scrolling path and get the value pre-filled at the
prompt instead. It is `[R]` on *Your profile*, and the alignment work
above applies either way.

If you have written tests against these screens, note that `label: value`
is now `label:` followed by the padding that aligns the column. Assertions
that spell the separator as a single space stopped matching (#575).

## Every frame closes (#570)

The bundled banner and masthead art had two defects, both visible and
neither reported by anything:

- **Fourteen presets drew a box whose rows were not the same width** — by
  up to nine columns. The frame could not close, and the vertical borders
  wandered down the screen. Every framed row in every preset is now one
  width. `welcome/cathedral_of_signals` and
  `new_account_after/solar_flare_crimson_amber` needed redrawing rather
  than padding: their interiors were built around two or three different
  centres.
- **Three presets used U+276E/U+276F**, the Dingbats ornament v7.5.0
  removed from the chat prompt for drawing as a hollow box in the fonts a
  Windows terminal reaches for. They are also East Asian Ambiguous, so
  the terminal, not the art, decided whether they took one column or two,
  and the frame moved with the choice.

Two tests now measure both, so neither can come back quietly.

**If your node already applied one of these presets, re-apply it.** This
is the one thing in the release that needs a SysOp's hand. Applying a
preset copies its bytes to the node's own banner or masthead file
(`path.write_bytes(data)`), and that copy is what callers see. Replacing
the wheel replaces the package resource and leaves the copy alone, so a
node that applied `cathedral_of_signals` last week still shows the
version whose frame did not close. Re-select the preset in the gallery
and apply it again; nothing else is needed, and a node that never applied
one of the fourteen has nothing to do.

## The website

www.netbbs.org was rebuilt alongside this release and is already live.
Worth recording here because of what it turned up: the gallery's two door
captures came from an artifact import with no script behind them, and the
War Dialer one was drawn with **heavy** box-drawing — the frame issue
#517 replaced with light, because a run of `━` shows gaps at the cell
seams. The site had been showing a frame the door stopped drawing.

Every capture on both pages is now regenerated by a script in the repo,
the door ones included (`scripts/website_capture_door_screen.py`, which
drives a door through the presentation gallery's own fixtures and walks).
The landing page is a caller's session; the overview is a SysOp's.

## Also

- The Users sub-console is in the published gallery for the first time.
  That is where v5.4.0's console work shows — the framed panel, the one
  ratio with a real denominator behind it, and the registration awaiting
  review. The console's landing screen is deliberately plain, which is
  why none of it had ever appeared.
- Chat status is no longer repainted once per line of a hub command
  reply. Periodic MRC count updates continue every five seconds.
- Redundant status-bar database reads are gone from the same path.

## Upgrade and rollback

Replace the wheel and restart. The node database migrates **65 → 66** on
first start.

**Rolling back to 7.5.0 requires a restore, and a restore costs more
than the migration did.** This is the manual part, and the important
sentence is the second one:

`_apply_migrations` refuses a database whose schema is newer than the
build understands, so a 7.5.0 wheel will not open a database that 7.6.0
has opened. There is no down-migration. Going back means restoring the
backup you took before upgrading — **which rewinds the whole database to
that moment**. Every post, message, account, permission change and
configuration change made while 7.6.0 was running is discarded, not just
the column the migration added. The longer the node ran, the more that
is.

So: take the backup immediately before upgrading, and decide early. If
7.6.0 is going to be rolled back, it is far cheaper in the first hour
than on the third day.

Nothing else changes shape: no Link protocol change, no save-format
change in either bundled game, no new `node_config` key.

One manual step besides the backup: **re-apply any banner or masthead
preset your node had already applied**, for the reason given under *Every
frame closes* — the wheel carries the fixed art, but your node is showing
its own copy of the old.

## Verification boundaries

What this release does **not** establish:

- **The MRC directory has not been exercised against a live hub on this
  branch.** #574's validation is 69 focused room, presence and chat tests
  plus locally inspected 80-column captures; ReLink's live SSH connection
  was not used for it. The directory is explicitly an advisory bounded
  snapshot of the hub format we have observed, not a specified one.
- **MRC provides no structured remote away flag.** Away state for remote
  users is not shown because the protocol does not carry it.
- **Three MRC review findings are deferred by decision, not fixed**: a
  status refresh that can wait for the current minute boundary when an
  already-occupied channel is mapped; the roster cache under live
  remapping; and combining concurrent `LIST` requests from different
  callers.
- **Guest login still has not been run on a live node** — unchanged from
  7.5.0, and still the part of recent releases most worth exercising
  deliberately.
- **The DoorParty and BBSLink templates remain unverified** against live
  provider accounts (#566, #565).
- **The POSIX-only door tests have still never executed** (#509).
- The picker's page counter can still say "page 2/2" while `[N]ext` has
  another page, after a mid-browse resize (#558). Cosmetic.

Gate for this release: full suite **9,396 passed / 53 skipped**,
`PYTEST_EXIT=0`, on this tree. Getting there took three passes and found
three things the release did not put there: an upgrade-compatibility
assertion that forbade adding a column (#579), a guest-login test whose
premise evaporated when two accounts landed on one clock tick (#580, and
the guard it exposed, #581), and eighteen field assertions still spelling
a separator as one space (#577). All are fixed and in this release. The
first gate pass also lost one Voidrunner test to a ten-second subprocess
timeout under load; it did not recur, and the Voidrunner suite passes
2,391 on its own.
