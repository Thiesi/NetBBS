# NetBBS v7.15.3

A patch release for v7.15.2. Most of it closes the ways a guest session could
change the one account every guest shares. It also makes letter hotkeys work
on Android keyboards in the web terminal, fixes a slot-art rule that didn't
match its documentation, and renames the levels screen. **Nothing migrates:**
the node database stays at schema 116, and every protocol, door API, save and
world version is unchanged. Upgrading is a wheel swap and a restart; rolling
back to v7.15.2 is the reverse.

**Two keys moved** in `SysOp ▸ Users`: List users is now `[U]` (was `[L]`),
and the levels screen is now `[L]` (was `[V]`). See "LAST" below.

## Guest sessions can't change the shared guest account (#1073, #1075)

With guest login on, every anonymous caller signs in to the same account. Up
to v7.15.2 a guest session was refused only the password and SSH-key screens
and mail,
so any visitor could change what every other caller and every later guest
saw. A session that signed in without a password is now refused the parts of the
account other callers see, and the settings a node-wide service reads from it. Each entry stays where it is
and says why when pressed: "This session signed in without a password, so it
cannot change … Every guest signs in to this same account."

**Refused to a guest session:**
- the public profile: bio and its visibility, signature, name and details
  (display name, location, **birthdate** and their visibility switches);
- whether it takes direct messages, read receipts, blocked people (from
  Profile or from
  **Bloc[k]** in Who's online), and whether its name shows on Previous
  callers;
- the chat alias (`/nick`) and the MRC settings the bridge keeps per handle:
  private messages, last-seen and nick colour;
- MRC hub registration: `/mrc register`, `identify`, `roompass`,
  `update password`, and raw `/mrc send`, whose free text could carry any
  of them.

**Birthdate mattered most.** A self-entered birthdate counts towards
`min_age` gates when there is no age attestation, so one guest saving a
birthdate opened every age-gated message board, chat channel, file area and
MRC room to every later guest.

**Display settings still change, for that call only.** Character set, colour
depth, redraw style, banners, editor, colours and sort orders apply to the
guest's own call and are never saved; the next guest gets the account's
stored settings. To change the guest's defaults, turn guest login off, sign in
as the guest account with its password, set them in Profile, and turn guest
login back on.

**Per call, not shared:**
- **Drafts.** A guest's unfinished posts and file descriptions are kept for
  that call only and deleted at hang-up. Before, the next guest was offered
  them.
- **Own posts.** A guest may edit or withdraw only the posts it wrote during the
  same call, and change the description only of files it uploaded during
  that call.
  Earlier guests' content is read-only to guests.
- **Bundled doors.** A guest plays Voidrunner, War Dialer and Retro Trivia
  with a throwaway identity for the call. Voidrunner and War Dialer run on
  copies, so a guest sees the real Hall of Fame and crews but changes nothing
  in them, and a callsign a guest types is never published. External doors
  still get the guest account; gate them by level as before.

**No privileges for the guest account.** Staff permissions, identity
verification (`can_verify_identity`) and moderator rights on a message board,
file area or chat channel can no longer be granted to the guest account, from
the console or the shell. An account holding any of them can't be made the
guest account; the Guest access screen names what it holds. Read and post
grants are still allowed: they are how you open an area to guests.

**Check after upgrading:** a Voidrunner career, score or War Dialer crew the
guest account built before this release stays in the real data. Remove it by
hand if you don't want it there.

## Web terminal: letters typed on Android keyboards act at once (#1066, #1067)

On an Android phone keyboard, a letter hotkey in the web terminal did
nothing until Enter was pressed, while `?` worked at once. The keyboard
"composes" each letter as part of a word, and the terminal only sent the word
when it was finished. On Android the terminal now sends each letter as it is
typed, and Backspace inside a word deletes on the node too. Autocorrect,
autocapitalisation and spellcheck are off on the terminal's text field on
every platform. Desktop browsers send exactly what they sent before.

## Main-menu art: a frame character between two items separates them (#1070, #1071)

On main-menu art, a box-drawing or block character between two drawn items,
such as a `│` panel gutter, now separates them, as the documentation already
said. Before, two items with fewer than two spaces before the gutter counted
as one, which `[C]heck` reported as "holds 2 keys". The items were shown or
blanked together, so a SysOp-only item next to a public one stayed visible to
everyone. The same rule applies to clicks in the browser.

## The levels screen is LAST, and says what each row is (#1068, #1072)

- The levels screen is now the **Level Admin SysOp Tool (LAST)**, opened with
  `[L]evels (LAST)` in `SysOp ▸ Users`. **List users moved to `[U]`.**
- From the shell it is `python -m netbbs.admin last`; `levels` still works
  as an alias, so existing scripts keep running.
- A level's own list gains a TYPE column (`board`, `area`, `channel`, `door`,
  `node`) before ACCESS, so each row says what kind of thing it opens. On a
  narrow terminal the one-line form names the type too.

## Verification boundaries

- **Gate:** **13,813 passed, 138 skipped** in the full suite (`pytest -n 10`)
  and 5 of 5 `timing_sensitive` tests, on Windows, on the exact release tree.
- The Android keyboard fix is tested against a model of the bundled xterm.js
  under Node, not on a real phone.
- Nothing here ran on NetBSD or Linux for this release.
