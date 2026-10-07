# NetBBS v7.17.1

A patch release for v7.17.0, with two fixes for problems the maintainer met on
a live node. Entering a birthdate on Profile overflowed an 80-column screen,
and some MRC senders' names appeared twice. **Nothing migrates:** the node
database stays at schema 120, and every protocol, door API, save and world
version is unchanged. Upgrading is a wheel swap and a restart; rolling back to
v7.17.0 is the reverse.

## Name & details: values are edited in a line of their own (PR #1151)

On **Your profile › Name & details**, the birthdate prompt put
`Birthdate [(not set)] -- new value as YYYY-MM-DD (blank to keep, - to clear):`
in front of the cursor. That is 78 columns, so on an 80-column screen the date
wrapped after two characters and scrolled the screen. Display name and location
had the same shape, with 14 columns left, and fewer once a value was shown in
the brackets.

All three now open with the current value already in the line, on a line of
its own, the way the SysOp edits the same values:

- **Enter** saves what is in the line;
- **an empty line** clears the value;
- **Esc** leaves it as it was.

A date that isn't one reads "Not a valid date (expected YYYY-MM-DD) --
unchanged."

**Callers who learned `-` to clear a value:** typing `-` no longer clears it.
Empty the line instead. A `-` typed as a display name or location is now saved
as the text `-`, and as a birthdate it is refused as not a date. The User
Handbook says so.

## MRC: a decorated sender handle is no longer shown twice (#1152, PR #1153)

Some MRC lines showed the sender's name twice:

```
<Michael Nln@Castle_of_the_Gods_V> +Michael_Nln+[CASTLE BBS] hello all
<johnny5@The_Delta_Quadrant> ^Johnny5<grAvY> hey
```

The MRC spec makes the first word of a message the sender's handle, and some
clients or users decorate it. NetBBS only recognised the reference clients'
plain shapes (`<nick>`, a bare `nick`, `* nick`), so a decorated handle stayed
in the message.

When no plain shape matches, NetBBS now also removes a decorated first word
that names the sender:

- **The name:** either spelling (underscore or space), any case, with only
  punctuation touching it. `Alicehello` is not Alice's handle.
- **Where the word ends:** at the first space outside brackets, since a tag
  like `[CASTLE BBS]` holds one.
- **Kept whole:** a message that is nothing but the handle, one with an
  unbalanced bracket, and one whose first word names someone else.
- **Colour:** the sender's name colour is read from the decorated handle too.

This applies to room lines, broadcasts and private messages from MRC. Lines
already in scrollback keep the text they were stored with.

## Website

www.netbbs.org announces the FTN gateway: "NetBBS can bark now." (PR #1150).
It is not part of the package.

## Upgrade and rollback

Stop NetBBS, install the wheel and start it. No migration runs. Nothing about
the upgrade is MANUAL, but **tell callers who clear profile values with `-`**
that they now empty the line instead.

To roll back, install the v7.17.0 wheel and restart. No restore is needed.

## Verification boundaries

- **The MRC fix** was tested against the two handles seen live and against
  constructed variants. It has not yet run against a live MRC room.
- **The Name & details screen** was tested with scripted sessions and drawn at
  80x23 through the website's terminal emulator. It has not been checked in a
  real terminal client.
- **The release gate:** the full suite on Windows, 14,385 passed and 139
  skipped with `pytest -n auto`, plus the 5 `timing_sensitive` tests run
  serially, all passing, on a tree identical to main after PRs #1151 and
  #1153. The release commit adds only the version bump and these notes.
