# NetBBS v7.5.0

The second batch of dogfood answers, and the feature 7.4.0 held back.

Most of this release is about what a caller actually sees: text that was
technically present and practically unreadable, a cursor you could not
find, and a warning about something that was never wrong. Alongside it,
guest login finally lands, and a backup that had been quietly leaving
game data out stops doing that.

No schema change, no migration, no persisted-format change. The node
database stays at schema 65, Voidrunner careers at save schema 2, War
Dialer worlds at world schema 10. Upgrading is replacing the wheel.

## Guest login (#531)

A node designates one **existing account** as its guest identity. Typing
that name at the login prompt starts a session as that user, with no
password. That is the whole feature.

It is an **authentication** shortcut and never an authorization model.
The guest is an ordinary account, so levels, per-object permissions, age
and name gates, moderation, auditing and Link trust all apply to it
exactly as to anybody else — and no code anywhere branches on whether a
caller is a guest. A SysOp says what a guest may do the same way they say
it for everyone: level-gate the release area at or below the guest
account's level, and leave everything else above it.

7.4.0 held this back after nine review rounds and twenty-seven findings,
several of them ways into the node that existed *only* because
passwordless access existed — an SSH key addable from a guest session, a
promotion landing during the login path's last await and coming back as a
SysOp session, a designation keyed on values that turned out not to be
unique. It ships now because a review round finally came back clean and
the full suite is green on the tree that merged.

## The greys

Dogfood, verbatim: *"whites/greys are too muted — descriptions are barely
readable."*

`MUTED_COLOR` moves 244 → 248. 244 is the middle of the xterm greyscale
ramp, which against the black background a terminal actually has sits
nearer the background than the foreground. "Muted" should mean recessive
but legible — you read a join notice without leaning in, and still see at
a glance that it is not somebody talking. 651 call sites take the change
without being touched, because every one of them names the constant.

Two consequences came with it. Seven horizontal rules had been spelling
"the dimmest value this palette has" as `MUTED_COLOR`, and a rule drawn
at 248 is a bar across the screen — including the masthead rule, which is
on every screen in the product. They now use a constant named for what
they are. And chat message bodies turn out never to have had a color at
all: they inherited whatever the caller's terminal defaults to, which is
the one shade the palette never got to choose. Raising the greys alone
would have narrowed the gap between a system notice and a person talking
rather than widening it.

## The file listing

*"The cursor is small, and the color change highlighting the selected
row barely noticeable, not least because it uses the same color as some
elements of the line do."*

Exactly so: the highlight was the accent color the filename already
carried, leaving bold as the only real signal. A highlighted row is now
one reverse-video bar — and the same in the shared picker, because two
different-looking cursors in one product is its own bug.

The five columns stop being three shades of one grey. The size takes the
brightest step, since it is the figure you compare down the column; the
date and the uploader get hues of their own. The uploader had **no color
at all**, which is how a five-field row came to read as one run of text.
Descriptions move up to the shade the sizes and uploaders used to have.

## Chat timestamps are on by default

**Worth reading twice: this changes behaviour for existing accounts.**
The default is the answer for an account that never expressed a
preference, so timestamps appear for everyone who has not deliberately
switched them off. An account holding an explicit `off` keeps it, and
`/timestamps` still toggles.

Knowing when a line was said is most of what makes scrollback readable,
and a caller entering a quiet channel cannot otherwise tell whether the
last line is a minute or a week old.

One consequence was a latent crash. With timestamps off, nothing ever
formatted a *carried* message's timestamp; with them on, every entry into
a channel formats every message in its scrollback — remote data, on a
path that had never run. A malformed or extreme value could take the
channel down for good. Such values are now refused at the Link protocol
boundary before the event is accepted, and a row stored before that
boundary existed costs its own stamp and nothing else. Rendering must
never be able to shut a caller out of a channel.

## One answer for an empty submit (#557)

7.4.0 changed what an empty submit means — a field opens on its current
value, Enter saves it, Escape leaves it alone, an emptied line clears it
— and the text fields did that while the gate and level fields on the
*same screen* kept the old "blank = keep".

Nothing was broken; `none` cleared a gate and the prompt said so. What
matters is the direction the mismatch failed in. A SysOp learns the
convention on whichever field they meet first, and applied to an age gate
"clear the line to clear the value" silently left the gate in place — and
the screen afterwards looked exactly like one where it had worked,
because the value was never on the line to begin with.

Every value-field prompt in the SysOp console now uses the one
convention, not just the two the report named. `none` and `-` are still
accepted; the prompts simply stop advertising them.

## The backup that said "no save directory found" (#555)

A node started by `examples/netbbs.rc` runs with `HOME` set to the state
directory, so Voidrunner writes careers there. A SysOp running the
documented backup command from their own shell has their own `HOME` — and
the CLI resolved the save directory in *its* process. It looked somewhere
the node never writes, found nothing, **exited 0**, and printed a line
that reads as a fact about the node.

That archive is the rollback point taken before an upgrade.

The node now records the directory it would hand a door, once its
listeners are bound, and the CLI reads that. Three provenances exist and
are reported differently because they carry different confidence: an
operator-supplied `--voidrunner-save-dir` wins; a node-recorded path is
authoritative, so finding it empty is a fact about the node; an
unrecorded lookup falls back to the calling process's home and is
reported as a guess — **whether or not that directory happens to exist**.
A guess that finds files is the dangerous case, because an operator who
once ran a node from their shell has exactly that directory holding
exactly the wrong careers.

**MANUAL:** every existing node is in the unrecorded state until it next
starts. A backup taken before that restart prints `Voidrunner: NOT
CAPTURED` and says what to do. Start the node once, or pass
`--voidrunner-save-dir`.

## Also

- A **local** chat channel no longer announces that it is connecting to,
  and then failing to reach, a real-time origin it never had.
- The chat prompt glyph moves from U+276F to U+203A. Not PuTTY's fault:
  U+276F lives in Dingbats, which the legacy fonts a Windows terminal
  reaches for do not cover. U+203A is the same shape a weight lighter and
  is already what every masthead breadcrumb uses.
- Enter no longer *places* a cursor in the file listing. It read well as
  an argument and wrong in the hand — it was the only place in NetBBS
  where that key creates a cursor.
- `pick_item` draws inside the terminal at every width (#538), the line
  editor can edit a value wider than one row (#546), the chat channel
  picker says which channels will refuse you rather than hiding them
  (#541), and the forked user picker is retired back onto the shared one
  (#537).
- Twelve tests that failed on NetBSD while passing on Windows are fixed,
  all test-side (#536).
- The project website is republished, and its landing page fits a phone
  (#559). The bullet lists there had never been able to wrap as prose at
  *any* width.
- Three facts a documentation rewrite had dropped are back in the door
  guide. Their absence had left `main` failing one test for eight weeks.

## Upgrade and rollback

Replace the wheel and restart. Nothing migrates.

Rolling back to 7.4.0 is the same operation in reverse, with no restore
required — but three things change shape and are worth knowing:

- Two new `node_config` keys (`guest_login_user_id`,
  `voidrunner_save_dir`). An older build ignores keys it does not know,
  so a rollback simply stops honouring them; the guest designation
  becomes inert rather than dangerous.
- Backup manifests written by 7.5.0 carry a `voidrunner_source` block and
  a per-component `source_provenance`. Older restores ignore unknown
  manifest keys, so a 7.5.0 archive restores under 7.4.0.
- Accounts that have never set a timestamp preference see timestamps
  disappear again on rollback, since the default returns to off.

## Verification boundaries

What this release does **not** establish:

- **Guest login has not been run on a live node.** It is merged on a
  clean review round and a green full suite, not on dogfood. It is the
  part of this release most worth exercising deliberately before a node
  designates a guest account.
- **The website's terminal captures still show the previous palette.**
  The color work changed what NetBBS looks like; the embedded captures
  predate it and are being redone.
- **The DoorParty and BBSLink templates remain unverified** against live
  provider accounts (#566, #565).
- **The POSIX-only door tests have still never executed** (#509). #536
  fixed test-side assumptions that failed on NetBSD; it did not run the
  suite that has never run.
- The picker's page counter can still say "page 2/2" while `[N]ext` has
  another page, after a mid-browse resize (#558). Cosmetic: paging itself
  lands correctly.

Gate for this release: full suite **9,308 passed / 53 skipped**,
`PYTEST_EXIT=0`, on a tree whose hash matches `main` exactly.
