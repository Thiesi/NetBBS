# NetBBS v7.4.0

A dogfood release. Eleven reports came in from running a real node, and
this is the first batch of answers: the SysOp's resource lists stop being
a flat grey sentence, editing a field stops meaning retyping it, and a
picker can create the thing it was going to list.

No schema change, no migration, no persisted-format change. Upgrading is
replacing the wheel.

## The SysOp's resource lists (#528)

Listing boards, file areas, chat channels or Communities used to produce
one grey string per row. Now it is a table: a header row, aligned
columns, the levels in their own colour, and — the reported gap — the
**gates that were never shown at all**. A file area with a minimum level
set *and* an age gate *and* a name requirement looked identical to one
with only the level.

Columns are opt-in per screen, because roughly thirty other pickers pass
genuine prose as their description and a table would be the wrong shape
for them. Below the width where the name column stays readable the row
falls back to that prose form, decided per render against the live
terminal width: a truncated table is worse than the sentence it replaced.

Gates now lead the fallback string too, so when truncation does happen it
takes the levels rather than the gates.

## Editing a value without retyping it (#529)

A field prompt opens on the current value. Enter saves what is shown,
Escape leaves the draft untouched, and an emptied line clears the field.

That last one is a convention change worth reading twice: every edit
screen used to treat an empty submit as "keep the current value", because
the prompt opened empty and Enter was the natural way to skip a field.
With the value already in the buffer, an empty result can only mean the
caller deleted it on purpose — so clearing gets the empty string, and
"leave it alone" moves onto Escape, which is its own key rather than an
overload of the empty one. The prompt says which key does what.

Alt-key combinations are tokenised as single events on the web transport,
so Alt+S no longer reaches the screen behind the prompt as a [S]ave.

## Creating from inside the list (#530)

A picker now offers `[C]reate`, dispatches to a caller-supplied callback
and returns what was made. An empty list stays interactive rather than
bailing out — which is exactly where this was most painful: you could not
make the first board from the screen whose whole job was listing boards.

## The file-area prompt (#527)

Pressing Enter in a file area reprinted `Choice or command:` once per
keystroke. The prompt belonged to the loop rather than to the render,
which is the opposite of the convention every other screen follows. Every
flow was audited; this was the only one out of step.

## Previous Callers (#535)

The name field had no fixed width, so every row below a long name was
shifted out of alignment. Names are now padded to a column, the
truecolour gradient covers the name rather than the padding, and a hidden
name reads as `(hidden)`.

## Age gates have to be ages (#540)

`_prompt_min_age` accepted any integer. A SysOp who meant 18 and typed
188 locked the resource against the entire node — silently, with nothing
on screen connecting "nobody can get in here" to a mistyped number. A
negative value was worse than useless: truthy, so treated as a real gate,
then passed by everyone with a birthdate while still failing closed for
anyone without one — a gate that filtered only the people who had not set
a birthday.

Bounded to 0-120, with the range in the prompt. Zero still means "no
gate", which is a supported way to override an inherited one, and `none`
still clears.

## Also

- `_page_size` accepted frozen width and height and then ignored them,
  so a page could be sized against a terminal the rows were not laid out
  for (#544).
- Voidrunner redraws in place, and its labels no longer hide in the blue.

## Not in this release

**Guest login and the pre-login notice (#531) are held back.** The
feature works, but it has taken nine rounds of review and twenty-seven
findings, several of them ways into the node that only existed because
passwordless access existed. It lands in 7.4.1 once a review round comes
back clean. Nothing in this release depends on it.
