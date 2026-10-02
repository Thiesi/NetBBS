# NetBBS v7.15.0

This release covers everything merged since v7.14.0. It has five parts.

- **Your own art on the menus.** A SysOp can draw the main menu, and the
  Boards, file areas and Chat channels lists, as their own ANSI art, with
  live values filled in, items a caller can't use blanked, and playback at a
  modem's speed that any key skips (#929). SAUCE records from scene tools are
  understood, and SyncTERM gets truecolor.
- **Levels you can read.** A Levels screen shows what each level opens, gate
  by gate; a level change shows what it opens and closes before it is
  applied; every level field says who it lets in; levels can have names; and
  accounts can be promoted automatically under rules the SysOp sets (#1004,
  #992).
- **Link trust that works on its own.** Remote nodes and callers can finally
  graduate from probation without a SysOp; recovery holds release on time; one
  refused event no longer blocks a whole push; Link status shows every
  hour-long wait and lets the SysOp end them. A key compromise now reaches
  nodes that only know the compromised node by introduction, and the re-signed
  content spreads. Nodes detect key equivocation and publish signed,
  verifiable accusations automatically (#589).
- **Linked chat both ways, and attestations that arrive.** Lines from a
  channel's subscribers reach its origin, live and by sync. Verified ages and
  names reach their recipients as sealed snapshots, even from a node nobody can
  dial (#632) — and the old pull is gone, so **nodes on v7.14.0 or earlier stop
  receiving attestations until they upgrade**.
- **Security and smaller fixes.** SSH host keys are created owner-only, web
  callers behind a reverse proxy keep their own address (`[web] trusted_proxies`), rlogin doors can open one game directly, and colour
  FILE_ID.DIZ files read cleanly.

**It migrates: the node database goes from schema 106 to 116.** Other versions
stay as they were: `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 4, Voidrunner careers
are save schema 2 with tactical and outclassed ruleset 3, and War Dialer worlds
are world schema 11. The Link wire gains four things older nodes ignore: an
inventory response may carry `key_chains` (#914), an events response may name
the events it `refused` (#897), and a node's descriptor advertises two new
capabilities, `channel_relay` (#860) and `sealed_attestations` (#632). There is one new route, `POST /link/v1/attestation-bundle` (#632), used only toward nodes that advertise `sealed_attestations`. One route is gone: **`POST /link/v1/attestation-pull/...` now answers 404**, so a
node on v7.14.0 or earlier no longer receives attestations from an upgraded
node (#1046, #1052). One new event type, `board_posting` (#993), is not
ignored by older nodes; see *Upgrade and rollback*. The one new key in
`netbbs.toml`, `[web] trusted_proxies`, is off unless set. Rolling back needs a
restore; see *Upgrade and rollback*.

## SysOp art and terminals

A SysOp's own ANSI art can now be the main menu, or the Boards, file areas
and Chat channels lists, with NetBBS drawing each caller's own items and live
values into it. Art from scene tools such as PabloDraw or Moebius shows as
drawn, and art can play at a modem's speed as an opening animation.

This is steps 3 to 6 of #929. **#929 stays open.** What is not done yet:

- **The maintainer's checks in a real SyncTERM** of this release's art work:
  a scene `.ans` with SAUCE, iCE colours, pictographs, too-wide art falling
  back, and the Card Catalogue sample with its blanked items.
- **No CTerm device-attributes probe.** Detection still uses the terminal
  type only.
- **Hand-drawn items are one row each.** Buttons drawn over several rows are
  deferred.
- The before- and after-signup banners take no field tokens.

**No art setting changes until you act.** Every piece of art keeps its
current mode (above the menu or list), speed (off) and credit line (off).
These settings live in the node database, not in `netbbs.toml`, and there is
no migration.

### Art from scene tools (#984, #987, #989)

These apply to all eight banners and mastheads.

- **SAUCE records are no longer shown to callers** (#984). Scene tools save a
  title, author and group at the end of the file. Before, callers saw them as
  junk under the picture, with a `␚`. The record, its comments and anything
  after the end-of-file byte are now dropped when the file is read.
- **A file with a SAUCE record is read as CP437**, even if its bytes would
  also pass as UTF-8.
- **Smileys, suits, notes and arrows** drawn with CP437's low bytes (☺ ♥ ♫ ►
  ▲ ⌂) reach every caller (#987): as the original bytes on a CP437 terminal,
  as the same symbols on a UTF-8 one, and as plain stand-ins in ASCII. The
  art editor keeps them too. Text that callers write never becomes such a
  byte.
- **iCE colours** (blink used to mean a bright background) show as bright
  backgrounds on every terminal, not as blinking text (#989). A CP437 terminal such as SyncTERM gets them wrapped in the CTerm switch it needs to show them.
- **Art wider than the caller's screen** isn't drawn for them, when its SAUCE
  record says how wide it is. The welcome banner falls back to the default,
  the other pieces to no art. Without a record NetBBS can't tell.
- **Each piece's status in the console shows the SAUCE credit and the width**
  the art was drawn for, and warns when it was made for a font other than the
  IBM PC one. Such art is still shown with CP437's characters.
- **The art editor writes a SAUCE record when it saves**: width, lines, font
  and date, plus the title, author and group of the file it loaded.
- **`[C]redit line`** on the welcome banner's screen puts a line such as
  `art: Nib Logo by InkWell/Quill` under your banner. It is off by default
  and audited.

Known limit: a board art post's rows that are narrower than the screen show
pictographs as stand-ins on a CP437 terminal. Banners and mastheads are
exact.

### The main menu as your own art (#981, #982, #985, #988, #1010, #1015)

**Settings → Mastheads & banners → Mastheads → Main menu** gains **`[M]ode`**.
It switches between *above the menu* (the default, as before) and *the menu
itself*. In the second mode your art is the whole main menu. You mark places
in it by drawing a token in plain text, in the colour its content should
take:

- `{menu 74x7}`: a region 74 columns wide and 7 rows tall for the caller's
  menu items.
- `{prompt}`: where the prompt goes. Without it, the prompt goes below the
  art.
- The fields `{user N}`, `{node N}`, `{level N}`, `{mail N}`, `{time N}`,
  `{date N}` and `{online N}`, cut to N columns. They show, for example,
  `level 20`, `3 unread` or `mail caught up`, and `4 online`. Time and date
  are in the node's timezone.

Brace text that is none of these stays part of the art.

**The art decorates; it never grants.** Each caller sees exactly the items
the generated menu would show them, `[?] Help` included, with the same keys.
`[?]` and browser clicks keep working. A caller gets the
generated menu instead, for that draw, when:

- their items don't fit the `{menu}` region (a SysOp's menu is the longest);
- the art has a problem, such as two slots overlapping, or no `{menu}` and
  no drawn items;
- their terminal is narrower than the art, or too short for it plus the lines
  below it;
- they read plain ASCII.

Nothing is left out to make art fit. CP437 and UTF-8 callers both get the
art. Problems are logged once per version of the file.

**Drawing the items yourself** (#1010, #1015). You can draw items such as
`[B]oards` or `Moder[a]tion` straight into the art:

- **Any bracketed key that is a main-menu key is an item.** No token is
  needed. The item is the text around the key, up to two spaces or a frame
  character on either side, which is also what a browser click picks up.
  **Put at least two spaces between items**: `[B]oards [E]-mail` with one
  space counts as one item, `[B]`.
- **Items a caller can't use are blanked**, painted over in the colour behind
  them, so frames and fills stay whole. A level-20 caller doesn't see a drawn
  `[S]ysOp console` at all. A staff member's `[S]` opens the Staff console, so
  a drawn `[S]ysOp` is blanked for them and their `[S]taff` goes into
  `{menu}`.
- **Items you didn't draw go into `{menu}`.** Games, Communities, Mail, Staff list, Verify, Moderation and Invitations appear only for some callers, so leave a
  `{menu}` region for them. Art without one that misses a caller's item gives
  that caller the generated menu. Art that draws every item needs none.
- **Other bracketed text stays as drawn**, for example `[x] marks the spot`.

**`[C]heck`** lists the art's size, every slot and drawn item, and any
problems. It flags bracketed keys that are no menu key and runs holding two
keys. It says whether your own menu and a level-0 caller's fit on a terminal
your size, or why that caller gets the generated menu, and which drawn items
are blanked and which go into `{menu}` for each.

**`[P]review`** draws the menu as you see it, then as a level-0 caller sees
it.

**Three new gallery samples** use 16 colours and CP437 characters only:
**Quill Ledger** and **Inkwell Blocks** (the menu itself), and **Card
Catalogue (items drawn in)**, which has `[S]ysOp console` and `[V]erify` on a row with `[L]ogoff` and a one-row `{menu}` for the optional items. Applying a
sample sets the mode to match it.

### The Boards, file areas and Chat channels lists as your own art (#1003, #1011, #1016)

The three list mastheads get the same **`[M]ode`**, **`[C]heck`** and
**`[P]review`**. In the second mode the art is the list itself:

- `{list 74x12}` marks the region. Each row shows the number to press, the
  name, and one value: what is new on a board, how many files an area holds,
  or who is in a channel. An entry the caller can't open yet because it asks
  for a verified name says `needs verification` instead. Descriptions stay on
  the generated list.
- `{title N}`, `{page N}` and `{count N}` show, for example, `Available message boards`, `2/5` and `14 total`. `{prompt}`, `{user}`, `{node}`,
  `{level}`, `{time}` and `{date}` work as on the main menu; `{mail}` and
  `{online}` stay blank on a list.
- A page holds as many entries as the region has rows. Numbers, a single
  digit and Enter, the arrow keys, `N`/`P` paging, search and browser clicks
  work as on the generated list.

A caller gets the generated list when the art has a problem, when they read
plain ASCII, when their terminal is too small, or when the region is under 3
rows or leaves names fewer than 12 columns. **`[C]heck`** also says whether a
list with a `needs verification` entry, the widest value, fits.
**`[P]review`** draws the first page of your own list; channel occupancy is
shown blank there, since only a running node knows it.

Each gallery has a sample drawn this way: **Board Ledger**, **Filing
Cabinet** and **Parlour**.

### Live values in the welcome and log-off banners (#991)

The welcome and log-off banners take the field tokens too, but not `{menu}`
or `{prompt}`.

- The welcome banner is shown before sign-in, so it fills `{node}`, `{time}`,
  `{date}` and `{online}`.
- The log-off banner adds `{user}` and `{level}`.
- SSH's sign-in banner fills `{node}`, `{time}` and `{date}`, so a raw
  `{node}` never reaches an SSH caller.
- Any other field is left blank. A banner without tokens is sent exactly as
  before.

### Art at a modem's speed (#1012, #1025)

- **`[S]peed`** on the welcome banner, main-menu masthead and the three list
  masthead screens steps through off (the default), 2400, 9600 and 38400 bps.
  Each piece has its own setting, and each change is audited. The speed shows
  as a `Speed:` row in the piece's status panel (#1037).
- NetBBS sends the art at that speed itself, the way a modem would have, so
  an ANSImation plays as one and still art builds up from the top. It works
  in every terminal, the browser included.
- **Any key draws the rest at once, and that key is used up.** An Enter
  pressed to skip the welcome art doesn't also submit an empty username.
- **A draw is paced for at most 5 seconds**; the rest then goes out at once.
- **Each piece plays once per connection**: the welcome banner when a caller
  connects, the main menu's art on their first main menu, a list's art on
  the first visit to that list. Never on a redraw, a page change, a cursor
  move, after a notice or a break-in, or in a door. On the main menu and the
  lists this covers both modes, the masthead above the menu and the art as
  the menu.
- **Callers reading plain ASCII never get pacing**, and callers can turn it
  off under **Profile → `[Q]uick or animated banners`** (animated by default). The welcome banner plays before sign-in, so it always follows the SysOp's speed; any key still skips it.
- Art that moves the cursor back over rows it already drew keeps its
  trailing spaces, since they may erase an earlier frame. Still art is
  trimmed as before.

### SyncTERM gets truecolor (#995, closes #986)

SyncTERM sends no `COLORTERM`, so it got 256 colours, and the bundled doors
and truecolor presets looked worse than in a modern terminal. A Telnet or SSH
caller whose terminal type is `syncterm` now gets truecolor. `ansi-bbs` and
`ansi` stay at 256 colours, since those names also cover clients with fewer.
An explicit `COLORTERM` still decides, and a caller's own Profile colour
depth still wins after sign-in.

### Banner and masthead screens on narrow terminals (#1037, closes #662)

- **The eight banner and masthead screens in the SysOp console now fit
  terminals narrower than 72 columns.** Their menus used to take two rows per
  entry there and push the title and status off the top. The described menu
  is kept wherever it fits and packed into a bar otherwise.
- **The Board, file-area and chat-channel masthead screens also ran 3 rows
  past 80x24**; they now fit.
- At 80x24 the main-menu masthead now shows the packed bar, because of the new
  `Speed:` row.
- **Known limit:** below 72 columns, the console's landing dashboard and the
  detail screens of a board, a file area, a channel and a user still don't
  fit. Design doc §3.4 records this.

### Chat help keeps one column (#1049, closes #1044)

Every page of chat `/help` now starts its descriptions at the same column.
Later pages used to shift left once the widest commands had been shown. The
full list now takes 5 pages at 80x24 instead of 4.

## Levels and accounts

A SysOp used to set a level on a board, a door or a setting and then guess
what it meant for real accounts. The level overview (#1004, now closed) adds
one view of every level gate on the node, a preview before an account's
level changes, a Levels screen, live counts in every level field, level
names, and a shell command. Accounts can also move up a level on their own,
under rules the SysOp sets (#992).

### Posting and uploading where the level is inherited (#1013)

- **Posting to a board whose read or write level is left to inherit its
  Community's default works again.** So do uploading to, and listing, a file
  area set that way. Each of these failed with an internal error instead of
  checking the level, and a caller only found out after writing the post.
  These checks now use the inherited level, as the board and file-area
  screens already did. Read and write grants still let their holder past it.

### What each level opens (#1004: #1014, #1018)

- **NetBBS now keeps one list of every level gate on the node** (#1014):
  reading and posting on each board, downloading and uploading in each file
  area, joining each chat channel, playing each door, and the node-wide
  gates for the node map, mail, MRC open rooms and the SysOp console. Each
  gate knows where its level comes from (set on the item, a Community's
  default, the node default or a setting) and what else it needs (age 18+,
  verified name, members only). Posting or uploading also needs the read
  level, so a write gate opens at whichever of the two is higher. The
  screens below are built on this list.
- **`Users` → `Le[v]els`** in the SysOp console lists each level that
  matters on the node: every level a gate opens at, every level an enabled,
  approved account holds, and 0. Each row shows how many accounts hold
  exactly that level and what it adds to the levels below it, for example
  `2 read · 2 post · 1 upload · 1 door · Node map`. 255 reads `everything`.
- **Pick a level** to see it gate by gate: the name, what it allows, the
  level, where the level comes from (`set here`, `Community Market`,
  `Settings`) and any other condition. **`[V]iew`** steps through *New at
  this level*, *Everything open*, *Still closed*, and *Open, with other
  gates*.
- **Picking a board, file area, channel or door opens its usual editor**,
  the same one the Content menu opens, so a wrong level can be fixed on the
  spot. Node-wide rows can't be picked.
- **`[G]o to level`** shows any level from 0 to 255, in use or not. Use it
  to check a level before you give it to anyone.

### See a level change before you make it (#1006, #1017)

- **Changing an account's level shows what changes first.** On the
  account's screen, `L` and the new level open a preview titled
  `Level 0 → 10`. It lists what the account **gains**, what it **loses**,
  and what stays **blocked**: things the new level opens but another gate
  still keeps it out of, such as `read  Adult Lounge  board; needs age 18+`.
  `[A]pply` makes the change. `[B]ack` leaves it, and says
  `'alice' stays at level 0.`
- **The preview counts what the account already has.** A board kept open by
  the account's own read or write grant is no loss. Something it was kept
  out of anyway is no loss either. A members-only channel it is not in or
  invited to counts as blocked.
- **A change that opens and closes nothing is applied at once**, and says
  so: `'alice' is now level 10. That opens and closes nothing for them.`
- **A change NetBBS would refuse is refused before the preview**: removing
  the last SysOp, a level out of range, or staff raising someone to 255.
- **Levels must now be 0 to 255.** Setting or creating an account at `-5`
  or `1000` used to be accepted; it is now refused.

### Every level field says who it lets in (#1008, #1019)

- **Each level field in an editor shows how many enabled, approved accounts
  are at that level or above**, and the count follows what you type, before
  you save: `10 · 41 users`. The node map and mail settings read `level 10 and up · 41 users`.
- **A board or file area that inherits its level says from where**:
  `none: 10 from Community Market · 41 users`, or `none: 0 the default · 41 users`.
- **A write level below the read level is counted at the read level**, and
  says so: `5 · 41 users (reading needs 10)`.
- **A Community's default levels show what inherits them**: `10 · 41 users · inherited by 2 boards, 1 file area`.
- This covers boards, file areas, Community defaults, chat channels, doors,
  and the node map, mail and MRC open-room settings. The count goes by level
  only. Age and verified-name gates depend on the account and are not
  counted.

### Level names (#1009, #1020)

- **A level can have a name.** On `Users` → `Le[v]els`, highlight a row and
  press **`Na[m]e`**, then type the name; a blank name clears it. A name is
  at most 12 characters, needs at least one letter, and two levels can't
  share a name, whatever the case. 255 is always `SysOp` and can't be
  renamed.
- **A name is a label only.** Gates and accounts keep their numbers, so
  naming, renaming or clearing a level changes nobody's access.
- **It shows beside the number** as `10 (Member)`: on the Levels screen and
  a level's own screen, in the change preview, on the account screen's Level
  field, and in every editor's level field.
- **You can type the name instead of the number** at the account screen's
  level prompt and at `[G]o to level`. Editors for boards, areas, channels
  and doors still take numbers.
- Names are stored with the node's settings, so backups carry them. Each
  change is recorded in the moderation log.

### The level overview from the shell (#1020)

- **`python -m netbbs.admin levels`** prints the same overview without the
  console. It only reads; it changes nothing and asks for no SysOp account.
  - `python -m netbbs.admin levels` lists each level, its name, its
    accounts and what it opens.
  - `python -m netbbs.admin levels member` shows one level, by number or
    name: what it opens and what stays closed.
  - `python -m netbbs.admin levels --user alice --to 50` shows what moving
    that account would gain, lose and leave blocked, as the console preview
    does.
- Each takes `--json`, and `--db` for a database file other than the
  default. A bad level, an unknown account, or `--to` without `--user`
  exits non-zero with the reason on stderr.

### Automatic promotion (#992, #1023)

- **Accounts can move up a level on their own, under rules you set.** Before,
  a SysOp raised every new account by hand or wrote a cron script. Set the
  rules in **`Users` → `Pr[o]motion rules`**. `[C]reate` asks for the level
  it starts from, the level it raises to, and what the account needs: an
  account age in hours, a number of logins, and a number of posts. Any of
  these can be 0. Pick a rule to edit it; `[D]elete` removes one.
- **Each row shows what the rule needs, how many accounts are ready, and
  what the step opens**: `0 → 10 (Member)  1 login  3  1 read · 1 post`.
  READY counts the accounts the rule would promote at their next login.
- **How rules run:**
  - They are checked when an account logs in, after that login is counted.
    Nothing runs on a timer, so an account that qualifies while away is
    promoted on its next call.
  - The session starts at the new level, and the first main menu says
    `You're now level 10 (Member).`
  - One rule per starting level, and one step per login, so a ladder of
    rules is climbed one call at a time.
  - A rule never lowers a level and never reaches 255.
  - Posts count when they are approved posts written on this node. An
    edited post counts once.
  - Each promotion is in the moderation log with `(system)` as the actor,
    for example `user_level 0 -> 10 (rule: 24h, 2 logins)`.
- **Rules leave these accounts alone:** SysOps, staff, accounts awaiting
  approval, disabled accounts, and the guest login's account, even while
  guest login is off.
- **They also leave alone any account whose level a person set.** Changing
  a level by hand, from the console or through `set_user_level`, marks the
  account, so a demotion is not undone at the next login. An account
  created at a level other than 0 is marked too. The account screen shows
  `Auto promotion: on`, `off (level set by hand)`, or `on, but skipped (staff)` and the like. **`A[u]to promotion`** turns the rules back on or
  off for that account. Staff with the manage-accounts permission can use
  it; only a SysOp can edit the rules.
- **On upgrade, nothing is promoted.** A node starts with no rules, so no
  level changes until you create one.
- **Migration 108** adds two fields to each account:
  - a login count, filled from the login history the node still holds. That
    history keeps at most the last 20 logins per account, and 500 across the node, so an old account
    can start with a lower count than its real number of calls;
  - the set-by-hand mark, set for every account the moderation log shows as
    **demoted**. Earlier promotions are not marked, because they can't be
    told apart from a script's, so those accounts can still climb.
- **MANUAL, if you raise levels with your own script:** remove the script
  and its cron entry, and add the same rule under `Pr[o]motion rules`. A
  script that keeps calling `set_user_level` marks every account it touches
  as set by hand, and the rules then skip those accounts.
- Rules are stored with the node's settings, so backups carry them. Each
  change to the rules is in the moderation log.

### Also fixed

- **A draft editor now shows why a save was refused.** The `Could not save: …` line was drawn just before the screen redrew, so with redraw in place
  on (the default) it was never seen. It now appears above the next prompt.
  This applies to every draft editor in the console, not only promotion
  rules. (#1023)

## Link

This part works through what the Phase 4 trust and recovery exercise (#915)
found. All four blockers it named in v7.14.0 are fixed here except #83, the
sustained dogfood run: recovery no longer needs a restart (#802), one refused
event no longer wedges a push (#897), and a compromise now reaches nodes that
know the signer only by introduction (#914). Phase 4 (#131) is still not
complete, and no public-readiness claim is made.

**A SysOp will notice after the upgrade:**

- **Link status has a Waiting section and `[R]etry now`.** The hour-long
  Link waits are listed, and you can end them all at once after fixing what
  caused them.
- **Your node signs equivocation signals on its own.** This is **on by
  default**. Turn it off under **Settings → Policy trust → Signals**
  (`Si[g]nals`). See
  *Equivocation signals*.
- **Probation can now end by itself.** Activity days are recorded from the
  upgrade on. Nothing is backfilled, so automatic graduation comes no earlier
  than the third distinct UTC date of contact, counting the day you upgrade.
- **Verified ages and names now travel only as sealed snapshots.** A
  recipient node running v7.14.0 or earlier gets nothing from yours until its
  SysOp upgrades. **Published identity** shows such a recipient as "needs a
  newer NetBBS to receive this".
- **A Linked board's origin decides who posts on it** (`[W]ho posts`).
- **Callers on probation at a channel's origin are heard there.** Their chat
  lines are accepted, and lines from subscriber nodes reach the origin live.
- **A trusted reporter scope saved as `dimension:*`** now grants every
  category of that dimension. Before, it granted nothing.
- **Self-verifying trust signals received before the upgrade stop counting**
  until this node reproduces their evidence. Signals minted by hand with
  placeholder evidence no longer quarantine anything.

### What a SysOp sees on Link status

**Waiting, and `[R]etry now` (#700, #1029).** Several Link retries wait an
hour, so that a refusal nobody can fix doesn't cost a request on every pass.
These waits used to be invisible. In the exercise every human step then took
an hour, because nobody could tell whether a fix had worked.

- **Link status → Waiting** lists every wait with its next try, in the node's
  display format. The kinds are:
  - **Set aside:** events from a node held back by its trust state here, or
    signed by a node not yet known here, or waiting on an earlier event.
  - **Refused there:** your events a peer refused one by one, with the reason
    in words.
  - **Introduction:** a node no carrier could identify, or a reporter being
    looked up again.
  - **Trust deposit:** a trust deposit a relay refused.
- At most three rows of each kind are shown, then "... and N more". The
  section is left out when nothing waits.
- **`[R]etry now`** ends every wait and starts a sync pass at once. The
  result line says what was released, e.g. "Released 2 set-aside event(s), …
  A sync pass is starting now." A trust-deposit refusal keeps its text on the
  vouch screen until the relay answers.
- **Trust changes release waits by themselves.** A Subjects override or
  clear starts a pass at once. Saving a change under Anchors, Domains,
  Reporters or Exceptions releases everything set aside and starts a pass. A
  trust change made with `python -m netbbs.admin` while the node runs is
  noticed on the next pass. A node that graduates or recovers on its own has
  what was held from it released.
- A peer list carrying a newer, correctly signed descriptor for a node known
  only by introduction now updates it. A reporter that just gained a relay is
  reachable without waiting an hour.

**Identity changes show when they were seen (#1043, #1047).** The IDENTITY
CHANGES section is a **When / Change** table. Times are in the node's display
format and time zone. Older entries show their real time too; nothing
migrates.

### Trust and probation

**Recovery holds release without a restart (#802, #994).** A quarantined
subject's 24-hour recovery hold was checked only at startup or when new
evidence arrived. Now every sync pass (five minutes by default) re-evaluates
every trust subject. That also covers an override's expiry, a signal's expiry
and probation's age requirement. Each change is logged at INFO, e.g. `Link trust: <subject> identity_integrity went from quarantined to probationary (automatic_recovery)`.

**Automatic graduation now works (#1035, #1038).** Design doc §12.4 lets a
node graduate from probation after direct contact on three distinct UTC
dates, and a remote caller after accepted activity on three. Nothing recorded
those days, so every subject sat at 0 of 3, and only Establish or an override
ended probation.

- A **node** gains a day when its hello to you or yours to it completes, or
  when it pushes something you accept.
- A **caller** gains a day when an event they wrote is accepted, however it
  arrived.
- Content a carrier brings is not contact with its origin node.
- A blocked or quarantined peer gains nothing.
- **Days count from the upgrade; nothing is backfilled.** The age and vouch
  conditions still apply.

**Pushed events are judged one by one (#897, #998).** One refused event used
to make the receiver answer 403 to the whole push. The sender then resent the
same batch every pass, and nothing behind it ever arrived. In the exercise
that was 68 pushes out of 68 over a night.

- The receiver now accepts what it can and names what it refused.
- **On the sender**, a refused event is held, not dropped. It is offered
  again every hour while the peer still asks for it, and shows under
  **Waiting → Refused there**. Once that author may post there, it arrives.
- The node log gets one WARNING per pass for newly refused events: `Link sync: node X refused N event(s) from this node one by one (<reason_code>: N, …) and took the rest. They are offered again every 60 minutes while that node still asks for them.`
- A refusal of your whole node is still a 403 for the whole push. So is a push in which every event was refused; the refused ones are still named and held.
- **Compatibility:** an older sender's batch now gets through an upgraded
  receiver. An older receiver still refuses the whole push.

**`*` in a reporter scope expands (#745, #996).** A scope typed as
`identity_integrity:*` was stored literally and matched no signal, while the
screens showed it as a normal grant.

- `*` now becomes every category of that dimension this version knows, when
  you save. You see the concrete list and can narrow it. A category added by a
  later version is never granted without you.
- **The upgrade expands `*` scopes already stored**, and pulls those
  reporters' objects again.
- A category this version doesn't know is still accepted, but the console now
  says so: "Not a category this version knows, so it has no effect yet: …"
- Spaces around the colon are ignored, so `identity_integrity: *` works.

**The explanation says how far reports are from quarantine (#752, #1030).**
Policy trust → Subjects → a subject now shows, whenever remote reports count toward the quarantine threshold, a line like `Remote reports toward quarantine: 1 of 2 domains, weight 1.0 of 2.0`. Before, that appeared only once the subject was quarantined. A
probation explanation also counts triggers in its own dimension
(`dimension_trigger_count`); `active_trigger_count` still counts every
dimension, and still blocks graduation.

**Self-verifying evidence must reproduce (#1036, #1041, #1053).** Two configured
reporters could quarantine a healthy node with a signal labelled
self-verifying, whatever its "evidence" was.

- A remote self-verifying signal now counts only if this node reproduces its
  evidence itself. The only kind that can be reproduced is signed
  equivocation: two different objects that the subject signed into the same
  slot of one chain, verified under keys this node holds for it. That means a
  key-transition chain, a post's edit chain, or a board's lifecycle chain.
- Revoked-key use, invalid authority and invalid signature delivery can't be
  checked by a third party, so from a remote issuer they never count.
- An unverified signal is kept and shown in the subject's explanation with
  `evidence_verified: false`. It counts toward no quarantine, but still holds
  back graduation, as any claim does.
- One about a node this node doesn't know yet is checked again on later
  passes, once the node is known.
- Verified evidence still needs a second trust domain to quarantine. It no
  longer becomes this node's own observation.
- A reporter you made sole authority for a category speaks alone, but its
  self-verifying signal also counts only once the evidence reproduces here.
- **Signals received before the upgrade stop counting** until their
  evidence reproduces. Signals minted by hand for the Phase 4 exercise had
  placeholder evidence and no longer quarantine anything.
- Digest-mode evidence is carried and never counts.

### Equivocation signals (#589, #1048)

**Your node now signs a trust signal when it sees another node equivocate,
that is, sign two conflicting objects into the same slot of one chain:** two
successors of one key change, two edits of the same version of a post, or two
closures or transfers of one board.

- **On by default.** The switch is under **Settings → Policy trust →
  Signals** (`Si[g]nals`).
  The screen lists the signals your node published, `[T]urn off` / `[T]urn on`
  toggles the switch, and `[R]evoke` withdraws one signal while keeping its
  evidence. Turning the switch off revokes every live signal on the next
  pass.
- **Only nodes that name yours a trusted reporter receive it**, through the
  existing trust pull and relay deposits. They check the proof themselves
  (#1036). There, one domain's signal is not enough to quarantine; a second,
  independent domain has to agree.
- **Bounds:** at most one live signal per node, at most five signed in 24
  hours, and never about your own node.
- **On your node**, the observation quarantines the forking node's identity
  integrity and is logged as a WARNING. The subject screen gains an **Observed
  here** section and `C[l]ear evidence`, which also revokes your signal about
  it.
- **Behaviour change: equivocation no longer lifts by itself.** When the
  evidence expires, the node stays quarantined with reason
  `equivocation_review_required` until you clear the evidence.
- A fork is noticed only where your node received both objects.
- **If your own node is accused:** honest nodes fork too. A node restored
  from an older backup, or a copied VM running the same identity, signs things
  its earlier self already signed. **Make sure only one copy of your node
  runs, then ask the other SysOp to clear the evidence.** The SysOp Handbook,
  "Policy trust settings", says the same.

### Keys and compromise

**A compromise reaches nodes that know the signer only by introduction
(#914, #1027).** After a node rotated its key with `--compromised`, a node that
had only been introduced to it still treated the stolen key as current. It
accepted copies signed with it, with nothing logged.

- A carrier now sends, beside the content it serves, the key history of each
  node that signed that content: at most 32 chains per response and 64
  changes per chain. A chain is sent only if it holds more than the first key.
- The receiver applies it before checking that content, so the old-key copy
  is skipped on the same pull. The log says `seed … carried newer key history for …`.
- Only identities known by introduction are updated this way. A direct
  peer's key history still comes only from that peer.
- A stale introduction bundle can no longer make a compromised key current
  again.

**Copies signed with a compromised key are fetched again (#672, #1034).** A
re-signed object keeps its content id, so carriers that held the old-signed
copy kept serving it and never asked for the fresh one. The re-signed copies
reached only the rotating node's own peers.

- A node that learns of a compromise now treats every stored copy that only
  the compromised key signed as missing. It stops declaring and serving it,
  and asks for it again.
- The re-signed copy replaces it in place. Posts, chat lines and files stay
  visible throughout and are never doubled.
- A copy marked stale stays stale across a restart.

**Key chains in a hello are capped (#1039, #1051).** A hello or introduction
carrying more than 256 key changes is refused as malformed before any
signature is checked. Each rotation adds two, so that is over a hundred
rotations.

### Linked boards

**The origin decides who posts (#993, #1021, #1024).** On the origin node, a
Linked board's screen has **`[W]ho posts`**. Each press steps to the next
mode, and every node carrying the board follows it:

- `anyone` (the default): every node's callers post, under each node's own
  write level.
- `origin_threads`: only the origin's callers start threads; anyone replies.
- `origin_only`: only the origin's callers post, replies included.

What follows from it:

- On every node, the board's SysOp console screen shows a **Who posts** line.
- On other nodes, callers aren't offered `[P]ost` or `[R]eply` where the mode
  forbids it, and the board says why: "Only the board's origin node posts on
  this board." or "Only the board's origin node starts threads here; you can
  reply."
- A door's post there is refused before it is written.
- A post from another node that the mode forbids is kept but not shown. The
  rule follows an origin transfer.
- **A closed board now refuses posts carried from other nodes too (#1021).**
  Before, closing it stopped only the origin's own callers.
- An announcements board no longer needs moderation to keep it one-way.

**Compatibility:** the setting travels as a new signed event,
`board_posting`, sent only once the origin presses `[W]`. There is no
capability check. A node on v7.14.0 or older doesn't know the event and
refuses it. A push carrying it is refused whole, so the origin's other new content stops reaching that node by push, and a pull from any upgraded node that carries the board stops at it.
**Upgrade every node that carries a board before changing who posts on it.**

**Unknown event types are kept and relayed (#1022, #1026).** An event type a
node didn't understand used to refuse the whole batch it came in. Upgraded
nodes now keep it, after a shape and size check (at most 64 KiB). They keep
at most 500 per sending peer, for at most 90 days, and never show it or act
on it. If it names a board this node carries, it is served with that board.
After an upgrade that understands the type, kept events are checked and
applied at startup. This lets later event types roll out without every node
upgrading first. It does not help nodes on v7.14.0 or older.

### Linked chat (#860, #1028)

On the exercise nodes, a subscriber's chat lines never reached the channel's
origin. The origin was outgoing-only, but that wasn't the cause: the
subscriber's callers were new to the origin, so they were on probation there,
and a probationary caller's chat line was refused.

- **A caller on probation is now heard in Linked chat.** Their chat lines
  are accepted, as their Link mail already is (#804). Quarantine and block
  still refuse. File uploads and other content without an approval step are
  unchanged.
- **Live lines go up as well as down.** A subscriber now sends its callers'
  lines up to the origin over the live session it already holds. The origin
  shows them to its callers and relays them to its other live subscribers,
  labelled with the writer's node, never back to the writer's node. Only the
  channel's origin may relay another node's line.
- A relayed line follows the same trust rules as its signed copy. A node
  that hasn't established the writer's node sees the line neither live nor by
  sync.
- **Lines that arrive by sync appear live** to callers already in the
  channel. A line already shown live isn't shown again when its copy arrives.
- **Compatibility:** relayed lines carry new optional fields, sent only to
  peers that advertise the new `channel_relay` capability. An older
  subscriber gets other nodes' lines by sync, as before. An older origin
  shows a subscriber's live line to its own callers but doesn't relay it, and still refuses the stored copies of lines from callers on probation there.

### Attestations (#632, #1040, #1042, #1045, #1046, #1052)

**Verified ages and names now reach nodes that can't be dialed.** Before, a
recipient had to pull them from the issuer. Most real nodes are outgoing-only,
so what they published reached nobody.

- **One sealed snapshot per recipient.** The issuer sends each recipient
  everything it should hold, sealed to that node's key and signed. It goes
  directly when the recipient can be dialed, and otherwise through a relay
  the recipient names. An issuer that is itself one of the recipient's relays
  keeps it there. Only the recipient can open it; a relay sees who sent it,
  roughly how large, and when.
- **When it goes out:** a changed snapshot on the next sync pass, an
  unchanged one weekly. A failed attempt waits an hour.
- **Removing a recipient takes back what it holds.** It is sent one final,
  empty snapshot, retried for up to 90 days, and forgets what it had from
  yours.
- **Published identity** (SysOp) replaces the "Nobody can dial this node"
  warning with a **Recipient / Route / Sent / State** table. State says the
  recipient holds everything published, that an update goes out on the next
  Link sync pass, why the last attempt failed, or "retracting: sends it an
  empty snapshot".
- **The caller's Profile toggle** says `on (sent to N nodes)`, `on (sent to M of N nodes)` or `on (not delivered yet)`. It gives counts only, never which
  nodes.
- **The attestation pull is removed** in this same release, by the
  maintainer's decision (#1046). Its route answers 404.

**Compatibility, and a break.** Snapshots go only to nodes that advertise the
new `sealed_attestations` capability, and the pull is gone. **A node running
v7.14.0 or earlier stops receiving attestations from upgraded nodes until it
upgrades.** On the sending node its row reads "needs a newer NetBBS to receive
this", and it is retried hourly, so its upgrade is noticed. Relays must be
upgraded to carry snapshots; a relay that doesn't advertise the capability
isn't used.

### What crosses the Link

`NETBBS_PROTOCOL_VERSION` stays 1. New things on the wire:

- **New event type `board_posting` (#1024).** Not capability-gated; older
  nodes refuse it. See *Linked boards*.
- **New capabilities `channel_relay` (#1028) and `sealed_attestations`
  (#1042)**, advertised in the node's signed descriptor. New chat fields and
  snapshots go only to peers that advertise them.
- **New route `POST /link/v1/attestation-bundle`; removed route
  `/link/v1/attestation-pull/{fingerprint}` (#1042, #1052).** Relay pickup
  returns snapshots in a new `bundles` key, which older nodes ignore.
- **New optional response fields that older nodes ignore:** `refused` in an
  events push response (#998), and `key_chains` in an inventory response
  (#1027, #1034). With nothing to report, responses are unchanged.
- **Stricter receiving:** a hello or introduction with more than 256 key
  changes (#1051), and an unknown event type over 64 KiB or of the wrong
  shape (#1026), are refused.
- **Malformed peer input is refused cleanly (#1050).** Input breaking the
  content-id rules, such as a float or an integer out of range, is now caught
  by the ordinary malformed-input checks. It could otherwise fail a request
  or a sync pass.
- **Signals (#1048)** travel as existing trust objects; no new format.

Migrations 107 and 109–116 belong to this section: the `*` scope expansion
(107), the posting setting (109), kept unknown events (110), stale copies
(111), verified evidence (112), withdrawn observations (113), snapshot
storage and the delivery ledger (114, 115), and dropping the pull's cursors
(116).

## Security, web and smaller changes

### SSH host keys are owner-only (#976, #990)

- **The node's SSH host keys could be read by any local account.** NetBBS
  wrote `<database name>_ssh_host_key` and `<database name>_ssh_host_key_rsa`
  with the process umask, so under the common `022` they were `0644`.
  Anyone with a shell on the host could copy them and pose as the node to SSH
  callers. v7.14.0's new RSA key had the same mode.
- **New keys are created `0600`** and are never readable by anyone else,
  even while being written.
- **Existing keys are checked every time the SSH listener starts.** A key
  readable by group or others is set to `0600`, and the log says so with a
  WARNING: "SSH host key … was readable by other accounts (mode 0644);
  restricted it to its owner (0600)". This also covers a key restored from a
  backup taken before this release, since restore keeps the file's mode. If
  NetBBS can't change the mode (another owner, a read-only mount), it logs a
  WARNING asking you to make the key `0600` by hand, and starts as before.
- **MANUAL — on a host where other people have accounts:** assume the old
  keys were copied. Tell your callers first that their SSH client will warn
  about a changed host key. Then stop NetBBS, delete both key files, and start
  it again; it creates new ones. On a host only you can log in to, nothing
  needs doing. The SysOp Handbook's troubleshooting table has this as a row.
- Windows is not checked: file mode bits there don't say who can read a file.

### Each web caller keeps their own address behind a proxy (#980, #1001)

Behind the reverse proxy the Handbook recommends, every browser caller
reached NetBBS from the proxy's address, usually `127.0.0.1`. So all web
callers shared one login-throttle bucket: one caller mistyping a password, or
one script guessing, soon throttled **every** web login on the node. The
SysOp screens couldn't tell browser callers apart either.

- **New optional setting `[web] trusted_proxies`**, a list of IP addresses or
  networks, empty by default. For a connection from a listed address, NetBBS
  takes the caller's address from `X-Forwarded-For`: the rightmost entry that
  is not itself a listed proxy. Everything to its left was written by the
  caller and is ignored. A missing or malformed entry leaves the proxy's
  address, as before. The login throttle, the log and the SysOp screens all
  use this one address.
- Hostnames are refused, not resolved. `trusted_proxies` under `[telnet]` or
  `[ssh]` is an unknown setting.
- **MANUAL — a node behind a reverse proxy:** add the proxy's address to the
  `[web]` table and restart:

  ```toml
  trusted_proxies = ["127.0.0.1", "::1"]
  ```

  List only proxies you run. Caddy and Apache send `X-Forwarded-For` by
  default. **With nginx, also add**
  `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` to the
  `location` block, as in the Handbook's example.
- **An Apache example is new in the Handbook** (2.4.47 or later, with
  `mod_proxy` and `mod_proxy_http`): `ProxyPass … upgrade=websocket timeout=1800`, `ProxyPreserveHost On` and `LimitRequestBody`. NetBBS sends
  no WebSocket pings, so without `timeout=` Apache cuts off an idle browser
  caller at its global `Timeout`. On NetBSD, one host refused every backend
  connection with `AH00957 (22)Invalid argument` at `timeout=3600`, while 600
  and 1800 worked; stay at 1800 or below there.
- **Rolling back:** v7.14.0 refuses to start with `trusted_proxies` in
  `netbbs.toml` ("unknown setting"). Remove the line before starting an older
  version.

### One door-server game per registration (#983, #997)

- **New option `terminal_type` on the `rlogin` adapter.** Door servers built
  on Synchronet read the RLogin terminal-type field to decide where a caller
  lands: `xtrn=<code>` starts one door, `xtrn_sec=<section>` opens one
  section. Register the same server once per game, set for example
  `"terminal_type": "xtrn=LORD408"` under **[C]ompatibility → [Q] Adapter
  options**, and "LORD"
  becomes its own entry on your door menu. Use the codes the provider
  publishes.
- The value is sent **exactly as written**, with no speed appended. If a
  server wants one, write it in: `xtrn=LORD408/38400`. Without the option,
  NetBBS still sends `ansi/<baud>` and the caller gets the server's own menu.
- It is fixed per registration: 1 to 64 printable ASCII characters, no
  spaces, no `{...}` substitutions. Saving the profile and **[K] Check setup**
  refuse anything else.
- The door guide has a new section, "One game per registration:
  `terminal_type`". Tested against a local RLogin server, not yet against a
  live door server.

### A carried edit no longer expires on arrival (#793, #1031)

- **Before:** on a board with a maximum post age, each revision of a post was
  aged by its own timestamp. A revision carried over the Link carries its
  author's clock. If that node's clock was far behind, longer than the
  board's whole maximum age, its edit expired as soon as it arrived, and
  callers saw the previous revision instead. For a withdrawal, that meant the
  withdrawn text was shown again.
- **Now** a revision ages from the later of its own timestamp and its post's
  first revision. An edit can no longer expire before its post. A genuine
  later edit still keeps a post alive, and old history carried late to a
  newly subscribing node still expires on schedule.
- The listed post counts and the "volume" and "activity" board orders use the
  same rule. Files are unchanged; they have no revisions.
- No migration. A revision already expired before the upgrade stays
  expired.

### Colour FILE_ID.DIZ files read as plain text (#1000, #1032)

- **Colour codes are removed whole.** NetBBS used to drop only the ESC byte
  of each ANSI sequence, so a colour `FILE_ID.DIZ`, common in art packs, left
  text such as `[0m` in the description. Now whole colour, cursor and
  title sequences are removed. Colour is not kept: descriptions have no colour
  markup.
- **SAUCE records are dropped.** Anything after the DOS end-of-file byte,
  including the `SAUCE00…` record art tools append, no longer turns up as
  description text.
- The same cleaning covers descriptions typed at upload and descriptions
  carried from other nodes over the Link.
- **MANUAL — descriptions already stored are not cleaned.** To fix one, open
  the file area, highlight the file and use **[E]dit description** (a SysOp,
  or anyone with edit permission on the area, can). The edit
  changes this node's copy only; nodes that already carried the file keep the
  description they were sent.

### Test suite under load (#896, #1033, #999, #1002)

Tests only; nothing changes for callers. If you run the test suite yourself,
for example on a NetBSD node, it now holds up on a busy machine. Tests that
slept a fixed time before checking a result now wait for it. The War Dialer
mouse-input tests retry an attempt the machine was too busy to time, and skip
with a reason if no attempt keeps the timing.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Ten migrations run
on the node database, taking it from schema 106 to 116:

- **107: trusted reporter scopes saved as `dimension:*`** (#745, PR #996).
  Such a scope was stored literally and matched no signal. Each one becomes
  one row per category of its dimension, as saving a grant now does, and that
  reporter's trust pull cursors are dropped, so the next pass reads its
  objects again. Effective trust is recomputed at startup.
- **108: `login_count` and `level_set_by_hand` on accounts** (#992, PR #1023).
  `login_count` starts from the session history still stored, which keeps at
  most 20 logins per account, so a long-standing account starts with fewer
  logins than it really made. `level_set_by_hand` is set only for accounts
  the moderation log shows were demoted.
- **109: `link_posting`, `link_posting_at` and `link_posting_json` on boards**
  (#993, PR #1024), empty: anyone may post on every Linked board, as before.
- **110: the `opaque_events` table** (#1022, PR #1026), empty. It holds Link
  events of a type this node does not understand, instead of refusing the
  batch they came in.
- **111: `stale_signer` on Link events** (#672, PR #1034), empty. The running
  node marks copies signed by a key their signer has since marked compromised
  at its next sync pass, and replaces them with the re-signed copies.
- **112: `evidence_verified_at` and `reverify_attempted_at` on trust signals**
  (#1036, PR #1041), empty. **A self-verifying signal received before the
  upgrade stops counting until this node reproduces its evidence.** Signals
  minted by hand with placeholder evidence for the Phase 4 exercise no longer
  quarantine anything.
- **113: `publication_withdrawn_at` on local trust observations** (#589,
  PR #1048), empty: no node published signals before this release.
- **114: the `link_relay_attestation_bundles` table** (#632, PR #1040),
  empty. A relay keeps one sealed attestation snapshot per issuer and
  recipient here, apart from the 50 relay mail slots.
- **115: the `link_attestation_bundle_ledger` and
  `link_attestation_bundles_received` tables** (#632, PR #1042), both empty:
  the issuer's delivery record per recipient, and the recipient's last
  applied snapshot per issuer.
- **116: the `link_attestation_pull_cursors` table is dropped** (#1046,
  PR #1052). Only the attestation pull used it.

One key in `netbbs.toml` is new: **`[web] trusted_proxies`** (#980, PR #1001),
a list of IP addresses or networks, empty by default. Empty keeps today's
behavior: every web caller's address is the TCP peer. **MANUAL — on a node
behind a reverse proxy:** set it to the proxy's address, usually
`["127.0.0.1", "::1"]`, so web callers stop sharing one login-throttle bucket
and the SysOp's screens tell them apart. With nginx, also add the
`proxy_set_header X-Forwarded-For` line from the Handbook's example. Hostnames
are refused, and only `[web]` takes the key.

New settings in the database start where today's behavior is: no level
names, no promotion rules, art in masthead mode, art pacing off and the art
credit line off. One starts on: **automatic equivocation signals.**

On the first start after the upgrade:

- **SSH host keys are made owner-only** when the SSH listener starts. On POSIX systems, a host key file
  readable by group or others is changed to `0600`, and the log says
  `SSH host key ... was readable by other accounts (mode 0644); restricted it to its owner (0600)` (#976, PR #990). New keys are created `0600`. On
  Windows the check is skipped. **MANUAL — if other people have accounts on
  the host:** assume they could have copied the keys. Tell callers to expect a
  host-key-changed warning, stop the node, delete
  `<database name>_ssh_host_key` and `<database name>_ssh_host_key_rsa`, and
  start it again. If the log instead says the key `could not be restricted`,
  the node still starts; make the file `0600` by hand.
- **A reporter scope you saved as `identity_integrity:*` now works** (#745).
  The reporter's objects are read again on the next pass, so a subject can
  become quarantined that was not before. The scope screen shows the
  expanded category list.
- **Probation activity days start counting** (#1035, PR #1038). Nothing is
  backfilled: every node and caller already met counts its first day the
  next time it says hello or has an event accepted. Automatic graduation from
  probation therefore comes no earlier than the third UTC date with activity,
  counting the upgrade day itself.
- **Your node signs equivocation signals automatically** (#589, PR #1048).
  When it sees another node sign two conflicting objects into one chain, it
  quarantines that node's identity integrity here and signs a trust signal.
  Only nodes that name yours a trusted reporter receive it, and they check
  the proof themselves. A node signs at most one live signal per subject and
  five a day, never about itself. The switch is under **Settings → Policy
  trust → Signals**; turning it off revokes every live signal on the next
  pass. An equivocation your node observed no longer lifts when its evidence
  expires: **MANUAL —** clear it on the subject's trust screen
  (`C[l]ear evidence`).
- **Automatic promotion does nothing until you write a rule** (#992). No
  rules exist after the upgrade. Rules skip the guest account, pending and
  disabled accounts, staff, SysOps, and accounts whose level a person set,
  but the upgrade marks only the accounts the moderation log shows were
  demoted. **An account you promoted by hand before this release can still be
  promoted further by a rule.** **MANUAL — before adding rules under Users → Pr[o]motion rules:** turn
  `A[u]to promotion` off on any account that should stay where it is. After
  adding a rule, check its `READY` count on the rules screen: rules apply at
  each caller's next login. A ReLink style cron script that promotes callers
  can be replaced by a rule. **Stop the script first:** every level it sets
  after the upgrade counts as set by hand, so the rules leave that account
  alone (turn `A[u]to promotion` back on to undo it).
- **Nodes on v7.14.0 or earlier stop receiving your attestations** (#1046).
  Verified ages and names now travel only as sealed snapshots. Published
  identity shows such a recipient as `needs a newer NetBBS to receive this`,
  and the node retries it hourly. An older node pulling from yours logs a
  WARNING each pass, `Link attestation pull: rejected a response from authority ...`, with HTTP 404, until it upgrades.
- **Do not set `[W]ho posts` on a Linked board until every node that carries
  it runs this release** (#993). The setting goes out as a signed
  `board_posting` event, a type v7.14.0 does not know: it refuses the whole
  batch the event arrives in, with HTTP 400, every sync pass until it upgrades, so the other content sent with
  it does not arrive either. A node on this release keeps an unknown type
  instead (#1022).

The systemd and NetBSD rc.d examples did not change.

**Rolling back needs a restore.** A 7.14.0 wheel refuses to open a schema-116
database: `Database._apply_migrations` stops with "database schema version
116 is newer than this NetBBS build supports (106)". **MANUAL — to roll back:**
stop NetBBS, install the 7.14.0 wheel, then restore the backup taken before
the upgrade. Anything since the upgrade is lost with it: promotion rules,
login counts and automatic-promotion choices, level names, `Who posts` on
Linked boards, activity days, observations and the signals your node signed,
sealed attestation snapshots sent, received or held as a relay, and every
banner and art setting. Banner art files are part of the backup and come back
as they were. Several things outlast the restore:

- **Signals your node published stay published.** Subscribers keep them until
  they expire; 7.14.0 cannot revoke them.
- **SSH host keys get their old mode back.** Restore copies each file with
  its mode, so a key that was readable before the upgrade is readable again,
  and 7.14.0 does not fix it.
- **A restore brings back the old SSH host keys.** If you made new ones after
  the upgrade, the restored backup holds the old, possibly copied, keys
  again; delete and regenerate them after rolling back.
- **`Who posts` stays set on other nodes.** Carriers on this release keep the
  last `board_posting` they received; an origin on 7.14.0 cannot change or
  clear it.
- **Sealed snapshots stay with their recipients**, and nothing this node
  issues on 7.14.0 reaches an upgraded node: those no longer pull (#1046).

## Verification boundaries

- **The release gate:** **13,720 passed, 138 skipped** in the full suite (`pytest -n 10`) and 5 of 5 `timing_sensitive` tests, on Windows, on the exact release tree. No test ran on NetBSD or Linux for this release.
- **The POSIX-only tests did not run for this release.** The SSH host-key
  change (#976) is POSIX-only. Its test of creating keys under a permissive
  umask and its test that an owner-only key is left alone are skipped on
  Windows. The tightening of an existing key ran on Windows only through a
  patched POSIX branch. No NetBSD or Linux node has run this release.
- **The three Phase 4 fixes have not been rerun on real nodes.** #914 (stale
  copies signed by a compromised key, now carried with the signer's key
  history), #897 (one refused event wedging a whole push, now judged per
  event) and #802 (a recovery hold released only at a restart, now
  re-evaluated every sync pass) are closed and tested in-process and over
  loopback. `docs/NetBBS-phase4-readiness.md` says each is to be seen on real
  nodes in a later exercise. #672's replacement of stale carrier copies is in
  the same position. Phase 4 (#131) is not complete, and no public-network
  claim is made, while #83's sustained run is not recorded.
- **Sealed attestation delivery has not run between real nodes** (#632,
  #1046). It is tested over real sockets between in-process nodes, including
  delivery through a relay, an issuer that is its recipient's relay, and a
  recipient on an older build. Removing the pull was not tried against a
  live v7.14.0 node; what that node does rests on reading its code.
- **No automatic equivocation signal has been seen on real nodes** (#589).
  An end-to-end test has two observers each detect a fork, sign through the
  sync pass and quarantine the forker on a subscriber over a real
  `LinkServer`. No real node has observed a fork. A node detects a fork only
  where it already refuses the extension, so a node that never received both
  objects does not notice. A signal's revocation signed before a key rotation
  is not re-signed after it.
- **Activity days and automatic graduation** (#1035) are tested in-process
  and over loopback, not observed on the live test network. The first real
  graduation cannot come before the third UTC date with activity after a node
  upgrades, the upgrade day included.
- **`board_posting` against v7.14.0** (#993) was not tested; the refusal
  described in *Upgrade and rollback* comes from reading v7.14.0's code.
- **SysOp art in SyncTERM was not checked for this release** (#929). The
  maintainer's SyncTERM 1.9 field test on 2026-09-30 covered steps 1 and 2.
  Steps 3 to 6 (SAUCE, iCE colours, pictographs, slot art, hand-drawn items
  and pacing) and truecolour for SyncTERM (#986, which rests on SyncTERM's
  CTerm manual) have only automated tests. #929 asks for that SyncTERM check,
  and the issue stays open.
- **`[web] trusted_proxies`** (#980) is tested with a real `WebServer` and
  WebSocket client sending `X-Forwarded-For`, not behind a real nginx, Caddy
  or Apache.
