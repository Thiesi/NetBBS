# NetBBS v7.18.3

A patch release for v7.18.2 with four changes: a SysOp can now edit a member's
whole Profile from the user editor; key bars that wrap line their hotkeys up in
columns; the user editor fits those columns at 80x24; and a rejected key no
longer eats a character of the prompt. **Nothing migrates:** the node database
stays at schema 123, and every protocol, door API, save and world version is
unchanged. Upgrading is a wheel swap and a restart; rolling back to v7.18.2 is
the reverse.

## Edit a member's Profile from the user editor (PR #1189)

The user editor (SysOp console > Users > an account) has a new key,
**`[E]dit profile`**. It opens the member's own Profile screen, acting on their
account: the same fields, sections and help they see. That covers their bio,
signature, name and details (location and who may see them), the people they
block, and their display, mail and chat settings. Until now the user editor
covered the account itself (level, status, name, birthdate, password, keys,
staff rights) but none of the settings a member sets in their own Profile.

- **Who:** a SysOp, on any account; a staff member with **Manage accounts**,
  on an account below level 255 that holds no staff permissions, the rule the
  display name and birthdate already follow. It is not offered on your own
  account (your Profile is on the main menu) or on the guest account, whose
  settings belong to each guest's own call.
- **Every change is checked and recorded.** Each write checks the editor's
  permission again in the database; a permission taken away while the screen
  is open refuses the next change with "Not changed: …". Each change goes into
  the account's admin history (**`[H]istory`**) as `edit_profile`, in the same
  database transaction as the change itself. Settings are recorded with their
  new value. Private text is recorded only as changed, never quoted: the bio,
  the signature, the location and who they block. A cleared sort preference is
  recorded with which one it was. The display name, birthdate, password and
  SSH keys keep the records they already had (`set_display_name`,
  `set_birthdate` and the password and key entries).
- **The screen draws with your settings, not theirs.** Your display settings
  (menu descriptions, redraw-in-place, line style, breadcrumbs) and your own
  session's character set apply. A change to the
  member's character set or banner animations is stored for them and never
  touches your own session. The "Transport report" line, which describes your
  connection, is left off.
- **What stays theirs or the SysOp's alone.** Whether a verified age or name is
  shared over NetBBS Link stays the member's own choice; staff cannot switch
  it on (a SysOp can still revoke a verification with **`[V]`**). SSH keys stay
  SysOp-only, as on the user editor. The username is shown, not editable.
- **Bio and signature** open in your preferred editor. Your unsaved draft is
  kept apart from the member's own, so neither of you recovers the other's
  text.

The SysOp Handbook's user-editor section had old names: the keys "Display
[n]ame" and "Birthdat[e]", and the main menu's "Staff list". It now says
**`[N]ame`**, **`[W]hen born`** and **Operators**, as the screens do.

## Key bars that wrap line their hotkeys up (PRs #1188, #1190)

Many screens show their keys on a packed bar: with menu descriptions off, on
detail panels, and wherever a described menu would not fit. When the bar was
too wide for one row it wrapped, and each row's keys started wherever the
row before ended, which made them hard to scan. A bar that needs more than one
row is now laid out in aligned columns, in reading order, each column as wide
as its widest entry:

```
[L]evel     [U]se promotion rules    [T]oggle enable/disabled
[N]ame      [W]hen born              [E]dit profile
[S]taff     [I]dentity verification  [K]ey
[P]assword  [R]estrict login         [H]istory
[D]elete    [B]ack
```

Columns cost rows: that bar takes 5 rows where packing took 3. So a bar is
aligned only while it takes at most a quarter of the terminal's height (6 rows
at 24 lines, 3 at 12); past that it stays packed, as before. A bar on one row
is unchanged. This applies to every NetBBS screen with such a bar, for callers
and SysOps alike; the bundled doors draw their own and are unchanged.

Screens that must keep their content on screen take the packed bar when the
aligned one would not fit:

- a detail panel or the review screen after writing a post or mail, when its
  page would otherwise be cut below its minimum;
- a SysOp console screen with a panel above its keys, when the aligned bar
  would not fit under that panel.

**The user editor fits at 80x24.** Its read-only facts (member since, admin
actions, moderator grants) now follow the account's fields directly, with no
`RECORD` heading or blank row of their own. That frees the two rows the aligned
bar needs: with redraw-in-place on, the screen is exactly 24 rows. With
redraw-in-place off, with a result line shown above the prompt, or for a
pending signup that left a signup answer (now shown below the record), the bar
stays packed.

## A rejected key no longer eats a character of the prompt (PR #1187)

On some screens, pressing a key the screen does not use moved the cursor back
one character before the bell rang, erasing part of the prompt each time. The
screens read keys without echoing them, but rejected them as if the key had
been printed. Affected were the SysOp user editor, every Create/Edit screen
(message boards, chat channels, file areas and the rest, and Profile), and the
review screen after writing a post or mail. An unknown key there now only rings
the bell.

## Upgrade and rollback

Stop NetBBS, replace the wheel and start it. No migration runs, and no setting
or config key is added. Rolling back to v7.18.2 is the reverse; any
`edit_profile` entries already in an account's admin history stay there and
are still listed.

## Verification boundaries

- **The release gate:** the full suite on this release's tree, 14,762 passed,
  139 skipped, 0 failed; the timing-sensitive tests 5/5.
- **Tested where it is shown:** the user editor's key gating (SysOp, Manage
  accounts, plain staff, your own account, the guest account), a staff edit
  landing on the member and in their history, a failed history entry undoing
  its change, the character set staying off the SysOp's session, the aligned
  and packed bars at the widths and heights above, the user editor at 80x24
  with redraw-in-place on and off, and the bell-only rejection on all three
  screens.
- **Not exercised on a live node yet:** the new user-editor layout and
  `[E]dit profile` have been checked through the test suite's terminal, not on
  ReLink.
