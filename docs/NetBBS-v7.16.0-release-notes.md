# NetBBS v7.16.0

This release covers everything merged since v7.15.3. Most of it came out of
the maintainer's hands-on test of the SyncTERM and SysOp-art work on
2026-10-06 and the re-checks that followed. It has five parts.

- **Verified ages.** A minimum age can now require a *verified* age, not
  just a birthdate the caller typed in (#1082). A SysOp passes age and name
  gates, can verify identity without granting it to themselves first, can
  revoke a verification on purpose, and can correct a caller's display name
  and birthdate.
- **Gates you can see.** Entering a gated board, file area or chat channel
  names its gates under the title, with the ones you don't meet marked
  (#1105). Linked boards, areas and channels are marked by the colour of
  their name (#1104).
- **A calmer SysOp console.** The six resource screens open straight on
  their own fields, with no `[E]dit` step (#1081). Results start with a
  `✓`, `!` or `✗` mark and keep NetBBS's usual colours, every pause reads
  `[Enter] Continue`, and results stay on screen after the redraw, on the
  caller's screens too (#1083, #1109, #1124).
- **Art and terminals.** The bundled presets use only CP437's characters,
  iCE art stops blinking in SyncTERM, Backspace stays Backspace in SyncTERM
  with F1 for help, the web terminal joins box and block characters between
  rows, and a SysOp can delete a banner's file (#1083, #1119).
- **Doors open with an animated splash** (#1080), and lists search with
  `[/] Find`, as the main menu does (#1083).

**It migrates: the node database goes from schema 116 to 117.** Other
versions stay as they were: `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 4, Voidrunner careers
are save schema 2 with tactical and outclassed ruleset 3, and War Dialer worlds
are world schema 11. `netbbs.toml` has no new keys. The Link wire gains one
optional field that older nodes ignore: a board, channel or file-area genesis
may carry `default_age_requirement`; see *Upgrade and rollback*. Rolling back
needs a restore.

**Keys that moved:**
- Lists search with **`[/] Find`**; `[S]earch` is gone, with no hidden
  alias, so `S` is free on those screens (#1093).
- On SyncTERM, **F1** opens help; Backspace no longer does (#1120). Ctrl-H
  still opens help everywhere else.
- The six resource screens choose fields with the **cursor only**; the field
  letters are gone, and so is `[E]dit` (#1081).
- On an account screen, **`[I]dentity verification`** grants or removes the
  right to verify, **`[V]erification: revoke`** withdraws a verified age or
  name, and **`Display [n]ame`** and **`Birthdat[e]`** correct the caller's
  own fields (#1110, #1115).

## Verified ages and verification

### A minimum age can require a verified age (#1082, PR #1094)

Up to v7.15.3 a minimum age accepted a verified age if the account had one,
and otherwise the birthdate the caller typed into their profile. A SysOp
could not run an area that needed more than a typed birthdate.

- **Setting it:** type `18v` in a board's, file area's or channel's Min age
  field. It reads "18, verified only", and the SysOp lists show
  "18+ verified". A Community default of `18v` reaches every board, area and
  channel in it that sets no age of its own. MRC open rooms take the same
  setting.
- **Existing gates keep their meaning.** A plain `18` still accepts a typed
  birthdate when there is no verified age.
- **What callers see:**
  - verified and old enough: they get in;
  - old enough only by their typed birthdate: the entry is listed, marked
    "needs verification", and entering it says who to ask;
  - too young, or no birthdate at all: the entry is not listed, as before.
- **Where it applies:** every age check goes through the same rule: lists,
  entering and posting, New scan, Find, the node map, file references,
  browser transfer links, MRC open rooms, the access map and the level-change
  preview. The plain file-area list now shows "needs verification" too, as
  the board list already did.
- **Link:** a Linked board, channel or file area passes its age requirement
  along beside the name requirement, as a recommendation the receiving node
  may keep.

### SysOps pass name and age gates (PR #1096)

A local level-255 account passes a minimum age, a verified-age requirement and
a name requirement without a birthdate or any verification, and is never shown
"needs verification". Staff, level 254 and authors from other nodes still meet
every gate.

### SysOps can verify; finding where to verify (#1103, PR #1106)

- **A level-255 SysOp can verify identity by default** and sees `[V]erify` on
  the main menu. The account screen reads "Can verify identity: yes, as
  SysOp".
- **Staff need the separate grant.** The Co-SysOp preset does not include
  it. After applying the preset, the Staff screen offers
  **`[V]erify identity`**, the same audited switch as
  `[I]dentity verification` on the account (#1115, PR #1117).
- **Refusals say what to do.** A verified-age refusal names where to set a
  birthdate (Your profile › Name & details) and who to ask (the Staff list on
  the main menu). Verified-name refusals in channels and MRC rooms name the
  Staff list too, and the editors' help for `18v` and verified names says who
  verifies.

### Revoking a verification (#1115, PR #1118)

- **On purpose only.** The account screen's **`[V]erification: revoke`**
  withdraws a verified age or name after a yes/no question, and the
  `[V]erify` screen has **`[R]evoke`**. Both are open to SysOps and to
  accounts that may verify, and both record the revocation in the account's
  history. Clearing a caller's birthdate or display name never revokes
  anything.
- **Link:** a verification already shared with other nodes is revoked to them
  at the next sync pass.
- **Callers see both values apart.** Profile › Name & details shows "Verified
  by this node: born …" separately from the caller's own fields. Callers can
  now clear their own display name, location and birthdate by typing `-`.

### A SysOp can correct a caller's display name and birthdate (#1110, PR #1112)

The account screen has **`Display [n]ame`** and **`Birthdat[e]`**. Enter
saves, a blank line clears, Esc keeps the value.

- **Who:** a SysOp, on any account. Staff with "Manage accounts", on accounts
  within the same reach as a password reset: below level 255, holding no staff
  permission, and never their own. The fields only show to someone who may
  edit them.
- **Rules:** the caller's own Profile rules, including the look-alike checks
  for display names. A birthdate may not be in the future or before
  1900-01-01; **that earliest date now applies to callers too.**
- **History:** a display-name change is recorded with the old and new name;
  a birthdate change only as set, changed or cleared, never with the date.
- **Verified values are untouched** and still win; the field says
  "; age verified" or "; real name verified" when one exists.

## Gates and Linked resources you can see

### The gates on a resource, named on entry (#1105, PR #1107; #1115, PR #1116)

Entering a gated board, file area or chat channel shows one line under its
title, for example:

```
Requires: age 18+ verified · verified name · level 20+ to post
```

- **Only gates that restrict someone,** with the values that apply after the
  Community cascade. A write level no higher than the read level is left out.
  File areas say "browse" and "upload". An ungated resource shows nothing.
- **Gates you don't meet are drawn in red.** ASCII callers get "(not met)"
  after each. The separate "Read only: posting needs level N." line is gone
  wherever a marked gate explains it; reasons that aren't gates, such as a
  closed board, keep their own line.
- **One row:** the line fits the screen. When it must be cut, unmet gates
  come first.

### Linked resources are marked by colour (#1104, PR #1108)

Boards, file areas and chat channels carried over NetBBS Link show their names
in pale turquoise, the colour already used for other nodes in chat, in the
callers' lists and in the SysOp console's three lists. ASCII callers, and
rows inside SysOp list art, get ` ~` after the name instead. Ctrl-H on any
list with a Linked entry explains the colour or the `~`. The board list no
longer starts a Linked board's description with "[LINK]".

## The SysOp console

### Resources open on their own fields (#1081, PRs #1086, #1090, #1098, #1100, #1101, #1102)

Community, category, message board, file area, chat channel and door screens
used to open on an overview and needed `[E]dit` to change anything. They now
open on the fields:

- a one- or two-line summary at the top, longer Link details in a section at
  the end;
- the cursor starts on the first field: ↑↓ choose, Enter or Space change, ←→
  step a choice;
- the resource's actions (Up, Down, Remove, Pending, History, Link and the
  rest) stay on their keys below;
- once a field is changed, only `[S]ave` and `[B]ack` show, and Back asks
  before discarding;
- a rename updates the heading, and a result message shows once.

Creating a resource uses the same screen with no actions. Accounts and the
settings screens are unchanged.

### Results, pauses and confirmations (#1083, #1109, #1119, #1124)

- **Status marks** (PRs #1095, #1106, #1113): a result starts with a green
  `✓`, a yellow `!` or a red `✗`; CP437 shows `√` and `x`, ASCII shows `*` and
  `x`. The rest of the line uses the normal colours: keys in the menu green,
  paths and values in the value colour. "Nothing changed" lines stay muted.
  Report screens such as `[C]heck` emphasise their numbers.
- **`[Enter] Continue`** (PR #1088): every "Press any key" pause now reads
  `[Enter] Continue`, or `[Enter] Back` / `[Enter] Stop` where that says
  more. Any key still continues, and in the web terminal the bracket can be
  clicked.
- **Mistyped confirmations** (PR #1121): typing the wrong name at a
  type-to-confirm prompt says
  `! Cancelled: 'Pen Repar' is not 'Pen Repair'. Nothing was deleted.` A blank
  answer still says "Cancelled.". The
  managed-DNS service screen no longer loses its results.
- **Results survive the redraw on callers' screens too** (#1124, PRs #1125,
  #1126): Profile and Name & details, Verify, Who's online, Link messages,
  direct-chat invitations, every draft editor, door launch refusals, Find,
  chat-entry refusals and the main menu. Verify and Who's online no longer
  hold a result behind a pause. The pause after a door exits stays, so its
  last screen can be read.

### Accounts (#1109, #1115, #1119)

- **Back returns to the list** (PR #1114) the account was picked from, on the
  same row, with sort and filter kept. After deleting an account, Back lands
  on the next one. The Directory works the same way.
- **The account screen's fields walk straight down** (PR #1123): level,
  status, blocked, display name, birthdate, public key, password, staff,
  verify identity and auto promotion, with "Member since", admin actions and
  moderator grants below under "Record".
- **Staff, password and SSH-key screens redraw in place** (PR #1117) instead
  of scrolling, with their results above the prompt.

### Banner and masthead files can be deleted (#1119, PR #1122)

Every banner and masthead screen offers **`[R]emove file`** while a file is
saved. It asks first, deletes the file, switches the piece off and records it
in the history. Mode and speed settings stay.

## Art and terminals

- **Presets use only CP437's characters** (#1083, PR #1089). 55 bundled
  presets used characters many terminal fonts lack, which showed as boxes in
  PuTTY and as `?` in SyncTERM. They are redrawn with CP437 glyphs at the same
  widths, and the character table gained stand-ins for those symbols in a
  SysOp's own art. **Presets already applied are copies** and stay as they
  were; apply a preset again to get the new version.
- **iCE art stops blinking in SyncTERM** (PR #1087). CP437 sessions get
  CTerm's bright-background modes 33 and 35 before any art with a bright
  background.
- **Field values are bare** (PR #1084). `{level}`, `{online}`, `{mail}` and
  `{count}` fill in a number only ("20", not "level 20"; "0" for caught-up
  mail), and the art supplies the words. The bundled slot presets now carry
  their own words. A value is never drawn wider than its field.
- **Previews play at the art's speed** (PR #1091), every time, skippable, and
  the two slot-art preview screens are numbered "1 of 2" and "2 of 2".
- **An answer starts below the art** (PR #1092) when the prompt is drawn
  inside it, so a question such as "Log off?" no longer overwrites a notice.
- **The web terminal joins box and block characters** (PR #1085). Rows are
  one font height tall, and a WebGL renderer draws those characters itself;
  without WebGL the browser falls back to its normal renderer.
- **Backspace stays Backspace on SyncTERM** (PR #1120). SyncTERM sends 0x08
  for Backspace, which NetBBS read as Ctrl-H help. On SyncTERM and ansi-bbs
  terminals 0x08 is now Backspace everywhere, F1 opens help on every screen,
  and the hints say "F1"; elsewhere they say "Ctrl-H" as before.

## Doors open with an animated splash (PR #1080)

Voidrunner, War Dialer and Retro Trivia each open with an animated title of
about 2.5 seconds before their usual first screen: a starfield and ship for
Voidrunner, a modem dial and CRT burn-in for War Dialer, and a game-show stage
for Retro Trivia. Any key ends it and is used up. It plays only when the door's
motion is on, the terminal is live and no input is waiting, and it fits every
size from 40×12 up. **To turn it off for a door,** set `DOOR_SPLASH=0` in its
profile environment; the door guide describes it.

## Lists search with `[/] Find` (#1083, PR #1093)

Every list uses `[/] Find` with a `Find:` prompt, as the main menu has since
v7.14.0. `[S]earch` is gone.

## Smaller changes

- **A flaky Retro Trivia test** waits on the event, not a timer (#1097,
  PR #1099).
- **The website** gained tour pages for message boards, file areas, doors and
  MRC (PR #1079). They are not part of the package.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. One migration runs
on the node database, taking it from schema 116 to 117:

- **117: `age_requirement` on boards, channels and file areas, and
  `default_age_requirement` on Communities** (#1082, PR #1094). All empty, so
  every existing minimum age keeps its meaning.

`netbbs.toml` has no new keys. The systemd and NetBSD rc.d examples did not
change.

On the first start after the upgrade:

- **SysOps pass every name and age gate** and can verify identity, without a
  birthdate, a verification or the "Can verify identity" switch.
- **Lists search with `/`.** Callers used to `S` find it free; tell regulars.
- **Art you drew with `{level}`, `{online}`, `{mail}` or `{count}` shows
  numbers only.** **MANUAL —** if your art relied on the words NetBBS used to
  add ("level 20", "3 online", "caught up"), draw the words into the art next
  to the field.
- **Bundled presets you applied earlier are unchanged.** **MANUAL —** apply
  the preset again if it shows boxes in PuTTY or `?` in SyncTERM.
- **Callers can no longer save a birthdate before 1900-01-01.** A birthdate
  saved earlier is kept.
- **A verified-age requirement on a Linked resource is only a recommendation
  to other nodes, and nodes on v7.15.3 or earlier ignore it.** They read the
  genesis with the field left out, so they apply a plain minimum age, and a
  typed birthdate gets their callers in. **MANUAL —** don't rely on `18v` on a
  Linked board, area or channel until every node carrying it runs this
  release.

**Rolling back needs a restore.** A 7.15.3 wheel refuses a schema-117
database ("database schema version 117 is newer than this NetBBS build
supports (116)"). **MANUAL — to roll back:** stop NetBBS, install the 7.15.3
wheel, then restore the backup taken before the upgrade. Anything since the
upgrade is lost with it: verified-age requirements, display names and
birthdates a SysOp or caller changed, verifications given or revoked, and
banner or masthead files you removed. Two things outlast the restore:

- **Revocations already sent stay with the nodes that received them.**
- **Genesis events carrying `default_age_requirement` stay with their
  carriers**, which keep the recommendation; it is an optional field, so
  7.15.3 sends nothing that contradicts it.

## Verification boundaries

- **The maintainer's last re-check did not run.** The maintainer checked the
  SyncTERM, art, list, verified-age and resource-screen work by hand on the
  test VM on 2026-10-06 and 2026-10-07, and every finding was fixed. The final
  round was deferred to after this release: the four #1119 fixes (Backspace
  and F1 on SyncTERM, deleting banner files, mistyped confirmations, the
  account screen's field order), the caller-side results of #1124, the red
  "not met" marks on the Requires line, and the "Verified by this node" line
  in Profile have automated tests only.
- **Door splashes** (#1080) are tested from 40×12 to 132×50 in each colour
  depth and photographed through the website's terminal emulator; War
  Dialer's splash cannot play on Windows, so its gallery walk is skipped
  there. They have not been watched in SyncTERM.
- **The WebGL renderer** (#1085) was checked in headless Edge only, in three
  fonts. No other browser was tried, and the fallback to the normal renderer
  after a lost GPU context is tested only through the script.
- **`default_age_requirement` against v7.15.3** was not run between real
  nodes; that older nodes ignore it comes from reading v7.15.3's code, which
  reads genesis fields with `payload.get`.
- **Revocations over Link** (#1118) are tested in-process; no real node has
  received one from this release.
- **No test ran on NetBSD or Linux** for this release; the gate was the full
  suite on Windows on the exact release tree (see the release page).
