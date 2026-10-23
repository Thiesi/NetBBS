# NetBBS v7.35.0

The full-screen editor no longer seems to swallow spaces, and it keeps up
with typing even on long texts. Verified real names now show on a post as
they were when it was written, not as they are today. A SysOp verifies an
age or name on a screen that edits the values where they stand. List rules
are as wide as the lists they frame.

**This release migrates:** the node database goes from schema 148 to 149.
Rolling back to v7.34.0 needs the backup taken before the upgrade (see
below).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1.

It is a minor release because it migrates and changes how a screen works.

## The editor: spaces show at once, and typing keeps up (PR #1451)

- **Spaces:** a typed space showed nothing until the next letter, which then
  appeared together with it. A second space in a row seemed lost. The editor
  drew the text being typed as if it were finished text, which drops spaces at
  the end of a row and squeezes runs of them. It now draws exactly what was
  typed. The SysOp's break-in chat line had the same fault and is fixed too.
- **Speed:** each keystroke rewrapped the whole text four times and went out
  as five separate writes. On a long post that cost a third of a second or
  more per key, measured on the server. Each key now costs a few
  milliseconds and goes out as one write, and the cursor no longer jumps to
  the status line and back between a letter and its echo. The ANSI art editor
  also sends one write per key.

## Verified names are kept as they were written (PR #1455, issue #1453)

On a board, file area or chat channel that displays verified real names, the
`(=name=)` beside an author used to be looked up again whenever the record was
shown. Verifying someone made their old posts look as if they had been written
under the verified name.

Now a post, an upload or a chat line stores the name shown with it when it is
written:
- **What doesn't change it:** a later verification, a replacement, a
  revocation, a rename or a new display name all leave old records as they
  were.
- **Edits:** an edit keeps its post's name.
- **Link:** a post carried from another node keeps the name accepted when it
  arrived.
- **What's still decided when the record is shown:** whether a resource shows
  the name at all. One that stops displaying names shows plain labels again.
- **Taking a name down:** a revoked verification no longer removes the name
  from posts already written. To take a name down, delete the post.
- **FTN:** a real-name echo (v7.34.0) sends the stored name. The board and the
  network copy always agree, including for a post a moderator approves after
  the author's verification changed.

**The upgrade repairs older records.** On resources that display names, a
record keeps its verified name only if the author's current verification
already existed when it was written. A post written before its author was
verified shows the plain name again.

## The Verify screen edits values in place (PR #1449)

The main menu's <kbd>V</kbd> **Verify** screen for an account is now a form:
- **The header:** what the person says about themselves, and who verified
  what, when.
- **The fields:** `01` **Verified birthdate** (with the age beside it) and
  `02` **Verified real name** are edited where they stand, opened on the value
  on record. Esc keeps a value. A malformed or future date is refused in the
  field.
- **[S]ave:** nothing is stored until <kbd>S</kbd> **Save**. It says what it
  records and what it replaces, including that a replaced verification's Link
  sharing is switched off, then asks once. Age and name are stored together.
- **Revoking:** <kbd>R</kbd> **Revoke** is offered while nothing is unsaved.
  Emptying a field doesn't revoke.
- **Keys:** `[A]ge` and `[N]ame` are gone; use `01` and `02`.

## List rules match their lists (PR #1450)

The rules around the message board, mailbox and file area lists, and after
chat history, were capped at 78 columns. The rows use the terminal's width
less one column, so the rules were one column short at 80 columns and far
short on wider terminals. They now match the rows at every width. The file
list's column heading also sat one column left of its cells; it is aligned
now.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The database
  migrates from schema 148 to 149: five columns, filled from existing records
  as described above.
- **Rollback to v7.34.0 — MANUAL:** v7.34.0 refuses a database newer than
  schema 148. Stop NetBBS, install the v7.34.0 wheel, then restore the backup
  taken before the upgrade with `python -m netbbs.backup restore`. In that
  order: a restore run by v7.35.0 would migrate the backup straight back to
  149. Anything since the upgrade is lost with it.

## Verification boundaries

- The full test suite ran on Windows against the release tree: 12,851 passed, 136 skipped, none failed (17:47); the timing-sensitive test passed too.
- The editor's speed was measured on the server only, in CPU time and writes
  per key, not over a live Telnet or SSH connection.
- **Known, not fixed here:** the file area list has no narrow-terminal layout,
  so its rows wrap below about 63 columns (#1452).
