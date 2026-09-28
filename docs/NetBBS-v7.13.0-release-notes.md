# NetBBS v7.13.0

This release covers everything merged since v7.12.0. It has five parts.

- **Message boards.** Boards gain what the 2026-09 boards audit (#674) found
  missing:
  - color and art in posts;
  - replies that quote;
  - pinned posts and files that actually show;
  - following boards, channels and file areas;
  - revision history for moderators, and withdrawal for authors;
  - a moderation workflow that tells authors what happened to their held
    posts, and gives approvers a queue of their own;
  - console tooling: editable categories, and word of what Link carried in.
- **The SysOp Monitor.** A live table of who is on and what they are doing,
  with snoop and break-in chat.
- **The node map.** Callers can see which nodes this board knows and how to
  dial them. The SysOp's peer list becomes a fuller map, and its "last heard"
  no longer counts gossip about a peer as contact with it.
- **SysOp console fixes.** A read-only view of the node's startup settings,
  yes/no fields that toggle on one key, and a rejected carried post that
  stays rejected.
- **Door playtest follow-ups.** Small fixes to Voidrunner and War Dialer from
  the 2026-09-18 playtest, War Dialer's Trade payout tapers, and Voidrunner
  careers move beside the node's database.

**It migrates: the node database goes from schema 77 to 87, and War Dialer
worlds go from world schema 10 to 11.** Other versions stay as they were:
`NETBBS_PROTOCOL_VERSION` is 1, `REALTIME_PROTOCOL_VERSION` is 4,
`DOOR_API_VERSION` is 4, and Voidrunner careers are save schema 2 with
tactical ruleset 3. Three Link payloads gain optional fields, which older
nodes carry unchanged; see *Link*. No key in `netbbs.toml` changed. Rolling
back needs a restore; see *Upgrade and rollback*.

## For callers

### Color and art in posts (#711, #754)

- **A board can allow color.** The SysOp turns it on with the board editor's
  `C[o]lor in posts` field. It is off by default, and no board changes on
  upgrade. On a board that allows it, a post can use pipe codes: `|00`-`|15`
  for the foreground and `|16`-`|23` for the background. Other text with a
  pipe in it, such as `ls |grep` or `|99`, stays text.
- **Pasted color is kept.** Colored text pasted into a post on such a board is
  typed in as pipe codes. Bold becomes the bright color; underline, blink,
  256-color and truecolor are dropped. On a board without color the codes
  are dropped, as before. Subjects and every other prompt are unchanged.
- **Pasting a truecolor sequence no longer disconnects you.** An escape
  sequence longer than 32 bytes used to close the session at any prompt. The
  limit is now 64.
- **`[A]rt post`** is offered on a board that allows color. It asks for a
  subject and opens the ANSI art editor on a canvas as wide as your terminal,
  up to 80 columns. An art post keeps its lines: it is not reflowed to the
  reader's width, and only a line wider than the reader's terminal wraps.
  Pipe codes are not read inside a drawing, and your signature goes under
  it. Editing an art post reopens the art editor.
- **Profile → Pos[t] colors** turns color off for you. Posts then show as
  plain text, with the codes removed.
- **What a post cannot do.** Color, bold, underline and blink are kept. Every
  other escape sequence (cursor movement, screen clears, titles, the bell)
  and other attributes such as inverse are removed before display. 24-bit
  color becomes the nearest 256-color shade on a terminal without truecolor.
  Search matches a post's text, not its codes.

A carried board follows this node's own color setting.

### Replying, pinning, following (#675)

- **`[R]eply` in the post reader.** The subject opens as `Re: <subject>`, and
  the body opens with the post quoted (`<author> wrote:` and `> ` lines,
  stopping at the signature, at most 40 lines). A reply is a flat post on the
  same board, not a threaded view, and the reader shows which post it
  answers. `[N]ew scan`'s replies-to-you pass now has replies to find. On a
  color board the quote is plain text, and an art post is answered without a
  quote. Mail's Reply quotes the same way. A reply to a carried post now
  tells other nodes which post it answers.
- **Pinned posts and files show.** Pinning used to have no visible effect.
  Pinned posts and files now head the page a board or file area opens on,
  under a `── Pinned ──` rule, in at most half the page. They also stay in the
  dated list where they were posted, reached by paging. The reader marks a
  post `pinned` or `kept`.
- **Following.** `[F]ollow` in `[N]ew scan` follows the highlighted board,
  channel or file area, and followed items are listed first.
  `[V]iew followed` shows only those. A board and a file area have their own
  `[F]ollow`/`Un[f]ollow`, and `/follow` in a chat channel follows it.
  Communities cannot be followed yet.
- **`[W]ithdraw` for your own post.** The text becomes `[withdrawn by author]`
  and the subject stays. It hides the text rather than deleting it: a
  moderator can still read it, and you can edit the post again. A withdrawal
  is not held for moderation, and nodes that carry the board apply it too.

### Moderation tells you what happened (#678)

- **You see your own held post** in the board's list, in its dated place,
  dimmed and marked `held`. Nobody else sees it, and it opens read only,
  badged `awaiting approval`. A post with an edit of yours still held is
  also marked `held`, and its reader shows the current text badged
  `your edit awaits approval`. Posting to a moderated board now confirms:
  "Submitted. Others will see it once a moderator approves it."
- **When a moderator approves or rejects your held post or edit,** you get a
  one-line notice the next time you reach the main menu, shown once. For
  example: `Your post "X" on general was rejected: off topic`. Past ten
  notices, the rest are summed up in one line.
- **A rejection also sends you a mail** from the moderator, with the reason
  if one was given and the text you wrote, since a rejected post is deleted.
  You can reply to it. If your mailbox is full, the notice still tells you.
- **Not covered:** posts made on another node, whose author is told nothing
  here; a moderator's decision on their own post; and uploads to file areas,
  whose uploader gets neither a notice nor a mail.

### Directory → Node [m]ap (#777)

On a node with Link enabled, **Directory → Node [m]ap** lists the nodes this
board knows: each one's name, how this board knows it (*direct*, *via* a
carrying node, or *unknown*), and when it was last heard from. Nodes not
heard from in 30 days are marked stale. A node's detail view adds its DNS
name, how to dial in to it if it publishes that, and the boards, file areas
and channels carried from it that you could open.

Nodes named only in another node's peer list, which nobody has verified, are
not listed, and neither are nodes this board has quarantined or blocked. The
SysOp sets who may see the map; see *The node map* below.

### What the SysOp can see (#761)

A SysOp can now watch any live session and break into it for a chat; see
*The Monitor* below. You are not told when your session is watched, but the
node logs every time it happens. The last line of chat's full `/help` says so,
and the User Handbook has a section on it.

## For SysOps

### The Monitor: Operations → Node and sessions → M[o]nitor (#761)

A live table of everyone connected, refreshed every two seconds, in the
spirit of `top`:

- **Each row:** user, transport, address, time on, idle time, terminal size,
  and where the caller is, such as `Boards › Retro` or `Doors › Voidrunner`.
  A caller who has not logged in yet shows as `(login)`.
- **Where a caller is names places, never content.** No post subjects, chat or
  mail partners, file names or search strings.
- **Idle time counts keystrokes only.** Telnet negotiation, resizes and other
  client traffic do not reset it.
- **The header** shows the node name, uptime and caller count, and the MRC
  state, maintenance, and a scheduled drain or shutdown when they apply.
- **Actions on the selected caller:**
  - `[M]essage` sends one line.
  - `[K]ick` disconnects them, with an optional message first, and is
    audit-logged like a disconnect from Who.
  - `[U]nwind` sends them back to the main menu. It is refused while the
    caller is somewhere that cannot be unwound, and before they log in.
  - `[S]noop` shows their screen live, with their cursor, cropped to your
    terminal. The caller is not told. Each snoop's start and stop, with its
    duration, goes to the node log, and its start to the Monitor's event
    tail.
  - `[C]hat` breaks in: both screens become a two-pane chat, you above and
    the caller below, and Esc ends it. The caller's screen then comes back
    exactly as it was, with a half-typed line intact, and anything printed to
    them meanwhile appears after the chat. A caller in a door is warned about
    first, because the door keeps running. It is refused during a file
    transfer and before the caller has logged in. A password the caller
    types during the chat shows as `*`. Each chat is logged like a snoop.
  - None of these act on your own session.
- **Sorting** cycles between time on, idle and name. The sorted column's
  heading is marked.
- **A recent-events tail** of up to three rows lists the last logins,
  logoffs, kicks, snoops and chats. It is kept in memory only.
- **Narrow terminals** drop whole columns (address, then terminal size, then
  transport) rather than wrap.

Who stays where it was.

**What it costs.** Snoop and break-in need a copy of every session's screen,
so every session now keeps one, all the time, in memory. It is fed from the
same output the caller gets. Zmodem transfers are kept out of it.

### The node map (#777)

- **Link status → `[P]eers` now opens the node map.** It shows everything the
  old peer list did, plus candidates named by peer lists (marked *unverified*
  and *never heard from*), quarantined and blocked nodes with each
  dimension's state, live relay counts, and everything carried from each
  origin, and when a peer list first named each candidate. `[P]eers` is
  offered once the map has a row.
- **Every node has a number** that stays the same for every caller and for
  you.
- **Last heard replaces last contact (#766, #777).** Direct contact now
  counts only when the peer itself was heard from: its hello, a hello
  answering ours, events it sent, or an open real-time session. It used to
  move whenever the peer's record was saved, including when its descriptor
  arrived secondhand in another node's peer list, so a peer that had not
  answered in weeks could look freshly contacted. Last heard is that direct
  contact, or the time in the peer's newest descriptor if that is later. A
  descriptor's time never counts past when this node first stored it. With
  neither on file, a node shows `unknown`. On upgrade, direct contact starts
  from each peer's old last-save time, which is the best information there
  is.
- **Who sees the caller map:** **Settings → Limits & retention → Node map
  level** sets the lowest user level that sees `Node [m]ap`. The default is
  0, everyone.
- **Dial-in addresses: Link status → `[D]ial-in`.** List up to four addresses
  where callers can reach this node: `telnet://host:port`, `ssh://host:port`
  or an `https://` URL; plain `http://` is refused. `[U]se suggestions` fills
  them from the node's DNS name and listeners. Nothing is saved until
  `[S]ave`, each save is audit-logged, and peers get the list with the next
  hello. Until you save a list, the node publishes your `[web] public_url` if
  it is `https://` and the web listener was on at the last start. Saving an
  empty list publishes nothing. Link status shows a `Dial-in` row with what
  is published and why. Peers show these addresses on their node maps.

### Settings → Network & login limits → Node c[o]nfiguration (#748)

A read-only view of the settings the node read at its last start: the config
file in use, the database path, identity directory and `node.name` key label,
each listener, the Link listener and advertised addresses, and the
managed-DNS service. Each row gives its `netbbs.toml` key, the value, and
where it came from: the config file, the command line, the default, or, for
whether Link is on, Join NetBBS Link.

- **Nothing on it can be edited,** because a wrong listener or address set from
  inside NetBBS could lock you out of the session needed to fix it.
- **The managed-DNS admin token** shows only as set or not set. It is never
  stored or displayed.
- **It shows what the node recorded at its last start,** in the BBS and in
  the standalone console (`python -m netbbs.admin`) alike. Until the node has
  started once on 7.13.0, the screen says there is nothing to show yet.

### Board moderation (#692, #675, #678)

- **A rejected carried post stays rejected.** On a carrying node, rejecting a
  held carried post or edit removed it but kept its signed Link event, as it
  must. **Operations → `[R]epair carried posts`** then took the event for a gap
  and published the refused post without a word. Every rejection, local or
  carried, is now recorded with who rejected it and when, and repair skips
  it, as does any later delivery of the same event. A post the repair does
  restore gets the same trust decision sync would give it.
- **Rejecting a held post asks for an optional reason.** Enter leaves it out. The author
  is told, as described under *Moderation tells you what happened*.
- **The "Approver only" grant works.** A caller who holds APPROVE on a board
  or file area gets `[Q]ueue (N)` on its page while anything waits, and
  decides there on the same screens the SysOp console uses, without the
  node status line, and without pin and keep unless they also hold EDIT.
  Until now only a SysOp in the console could reach a queue.
- **Permission change: APPROVE may now reject.** Rejecting a held post or
  upload takes APPROVE or DELETE; it used to take DELETE only, so an approver
  could publish a held post but not turn one down. Deleting something already
  published still takes DELETE.
- **Queues say what each item is:** `post`, `reply`, `edit` or `file`, its
  submitter (for a held edit, whoever made it) and when it was submitted,
  oldest first. A queue lists the oldest 200 and says when more wait. A held
  edit is shown above the current text it would replace, and a held reply
  names the post it answers.
- **Content → `[P]ending review`** in the SysOp console lists every held post
  and upload on the node in one queue, with where each one waits. Boards and
  areas hidden from a carried Link are left out.
- **`[H]istory` on a board's or file area's detail screen** in the console
  lists up to five of its moderators with their permissions, including
  Community-wide and node-wide grants, and under them what moderators did
  there, newest first.
- **`[H]istory` in the post reader,** for anyone with the board's edit
  permission, lists the approved versions of an edited, removed or withdrawn
  post, up to the newest 50, with the moderator's edits marked. Readers and
  authors see only the `edited` badge.
- **Pin and keep from where posts and files are read.** Anyone with a
  board's or area's edit permission gets `P[i]n` and `[K]eep` in the post
  reader, and in a file area, where they pick a file starting from the
  highlighted one. `[K]eep` exempts the post or file from expiry, and is
  offered only where content expires. A withdrawn post cannot be pinned or
  kept. Pins are local: they are never carried over Link.
- **An edit of a kept post is kept.** Pin and keep used to be stored per
  revision, so editing a kept post made the edit expire and the post fall
  back to its old text. Both now belong to the post, and removing or
  withdrawing a post clears them.
- **Board lists check the full read rules.** Listing a board's posts and pins
  now checks the Community's read level and minimum age itself, not only the
  board's own read level.

### Board tooling in the console (#681)

- **You are told what Link carried in.** Carrying stays automatic: a board,
  file area or channel carried over NetBBS Link arrives Uncategorized with its
  origin's settings. Now the dashboard's ATTENTION panel shows
  `Newly carried: N`, and the board, file-area and channel lists mark each
  one `to review` until you open its detail screen, where you set its
  category and levels. Opening it is enough to clear the mark. A resource you
  accepted from `[O]ffered` is not marked, since you chose it, and a hidden
  one is never counted.
- **Offers waiting for your decision,** at the carry cap or refused for a
  name already taken here, show on the dashboard as `Offered: N`, not only on
  Link status. Both counts appear only while they are nonzero, and they show
  even while Link is stopped.
- **Categories can be edited and ordered.** For board, file-area and channel
  categories alike, the console lists them as a tree, sub-categories under
  their parent. Picking one opens its screen, with its description, parent
  and place among its siblings, and:
  - `[E]dit` for name, description and parent. Categories stay two levels
    deep: the new parent must be top-level, and a category with
    sub-categories cannot move under another.
  - `[U]p` and `[D]own` to move it among its siblings.
  - `[R]emove`, confirmed by typing its name, as before. Its sub-categories
    become top-level, and its boards, areas or channels uncategorized.

  Callers see categories in your order. Existing categories keep today's
  alphabetical order until you move one; a new or moved category goes last
  among its siblings.
- **The board and file-area lists show what is there:** a `posts` or `files`
  column such as `12 +3`, meaning 12 listed and 3 held for a moderator, and a
  status such as `open, pinned` for a pinned board or area.
- **`[B]ack` from a board's or file area's detail screen returns to the
  list,** on the item you left, as its description always said.
- **"Auto-purge" is now "expire"** in the max-age field, which says what
  happens: items expire, so readers no longer see them, and are deleted once
  the node's grace period has also passed. "Exempt from auto-purge" is now
  "Exempt from expiry".
- **Drawing the dashboard no longer writes to the database.** It used to run
  the expiry sweep for every board and area with an age limit on every draw.

### Yes/no fields toggle on one key (#751)

In the board, file area, channel, category, Community, door, door profile and
MRC editors, a yes/no field such as "pinned", "hidden" or "moderated" now
flips on its hotkey, Space, Enter, or Left/Right. There is no "Pinned?"
question any more. Nothing is saved until `[S]ave`, as before. MRC's TLS
field toggles the same way and still moves the port between 5000 and 5001
when it was the well-known one.

### Voidrunner careers live beside the node's database (#648)

Careers used to default to `~/.netbbs/voidrunner_saves`, one directory for
every node the OS account ran, so two nodes run by the same account shared
careers by user ID. The default is now `<database>.doors/voidrunner/`, next to
War Dialer's world. `VOIDRUNNER_SAVE_DIR` still overrides it, and then nothing
is copied.

- **On its first start after the upgrade,** a node that was using the old
  directory copies it into its own and logs where it copied from. It copies
  rather than moves, so another node under the same account keeps its careers
  too. If a pilot is playing at that moment, the node keeps using the old
  directory and tries again at its next start.
- **MANUAL — for a node upgraded straight from v7.4.0 or earlier:** such a
  node never recorded which directory it used, so it does not copy. Before
  its first start on 7.13.0, either set `VOIDRUNNER_SAVE_DIR` to the old
  directory or, with the node stopped, copy the old directory's contents into
  `<database>.doors/voidrunner/`. Otherwise its pilots start new careers.
- **The old directory can be removed** once every node, and any standalone
  Voidrunner run by the same account, has moved on. If two nodes shared it,
  both copies hold both nodes' careers under overlapping user IDs, as the
  shared directory did.
- Backups and restores use the new directory. A restore into it needs no
  `VOIDRUNNER_SAVE_DIR` setting.

## Doors

### Voidrunner (#648, #771)

- **Survey contracts completed by a remote scan pay half.** Arriving in person
  still pays in full. The contract, the scan terms and the completion line all
  state the figure.
- **A second session is turned away in one line** that says why: the pilot is
  already flying elsewhere, or the saves are busy with maintenance. There is
  no longer a second "Press any key".
- **Combat reads more clearly.** A range with equal ends prints as one
  number (`4-4` becomes `4`), the Guard row is reworded, the intent row names
  the ship and the pattern it flies, and `[I] Info` names the pattern.
- **"Jumping to …" is no longer kept** in the retained report after a jump;
  `Departure: …` leads it.
- **One-page screens no longer offer page keys.** Thirteen screens offered
  Next and Prev on a single page.
- **The departures table keeps ECONOMY at every width.** An uncharted
  destination's danger is `?`, and at 40 columns each destination takes two
  rows.
- Credit balances use thousands separators, survey results list charted
  systems before the mission lines, and the contract prompts end in `:`.
- **The door list clears before a door starts** for callers with in-place
  redraw on.

### War Dialer (#649)

- **Trade's payout tapers within a turn-day.** The first three Trades pay
  $20-$60. From the fourth on, the top of the range falls $5 per Trade, down
  to $20-$40, and it is back to $60 when your turns refill or a new season
  starts. The $20 minimum never moves. The Trade preview says so from the
  fourth Trade on, and Help states the rule. This is the change that needs world schema 11.
- Counts agree with their nouns ("1 defender returned"), one-page screens no
  longer offer page keys, and a refusal such as "out of turns" is said once
  instead of up to three times.
- An empty feed or log says "Nothing has happened to you yet."
- Root Exchange shows an unclaimed exchange as `owner none`, so its rows are as
  even as the others at 80 columns.
- **A pasted or garbled input sequence** now ends the visit with its own
  message and no "exited unexpectedly" line from the host.

## Link

Three signed payloads gain an optional field. None is a protocol bump: a node
older than 7.13.0 keeps and relays the signed bytes unchanged.

- **`board_post` gains `"layout": "art"`** for an art post. An older node shows
  the art post as ordinary text, reflowed.
- **`board_post_edit` gains `"withdrawn": true`** for an author's withdrawal.
  A 7.13.0 carrying node applies it without local moderation or a trust hold,
  but only when it is a withdrawal and nothing else: the body is exactly the
  placeholder and the subject is unchanged. Anything else carrying the flag
  is moderated as an ordinary edit. An older node treats a withdrawal as an
  ordinary edit, as before.
- **The endpoint descriptor gains `dial_in`,** the node's published dial-in
  addresses. A malformed entry is dropped by the reader and never fails a
  hello.

**The current revision of a post is now the one received last,** not the one
with the latest timestamp. A carried revision's time is its author's clock,
so a withdrawal stamped by a clock running behind never became current. The
Link only accepts an edit that extends a post's chain, so receipt order is
chain order.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Ten migrations
run on the node database:

- **`allow_color` on boards,** off for every board. The search index is
  rebuilt from each post's plain text, which takes longer on a node with many
  posts.
- **`last_direct_contact_at` on Link peers,** filled from each peer's old
  last-save time.
- **`layout` on posts,** `prose` for existing posts, except carried art posts
  that this node's copy of their signed Link event marks as `art`.
- **The `post_rejections` table.** Earlier rejections are carried over from
  the moderation log, so they last too. **A refused carried post that an
  earlier repair already republished is taken down at upgrade,** as long as
  nothing depends on it: no replies and no later revisions. One that has
  replies or revisions keeps its row for a moderator to remove.
- **Pin and keep move to the post.** A flag set on any revision is copied to
  the whole chain, and a trigger keeps later revisions in step. Removed posts
  lose their pin and keep, so they leave the pinned block.
- **Node-map bookkeeping:** `descriptor_first_stored_at` on Link peers,
  introduced identities and candidates, filled from each row's last update;
  `first_named_at` on candidates; `last_direct_contact_at` on introduced
  identities, starting empty; and a table of node numbers.
- **`withdrawn` on posts,** off for every existing post.
- **The `moderation_notices` table,** empty.
- **`position` on board, file-area and channel categories,** filled in
  today's alphabetical order, so nothing moves.
- **The `link_carried_to_review` table,** empty: resources carried before
  the upgrade are not marked `to review`.

War Dialer upgrades its world from schema 10 to 11 the first time it opens it.
Every player's Trade count starts at zero, so in a turn-day already under
way at the upgrade, up to three more Trades pay the full range. The door guide's advice to back up the world before an upgrading
version applies.

Voidrunner careers are copied on the node's first start; see *Voidrunner
careers live beside the node's database* for the MANUAL step on nodes
upgraded from v7.4.0 or earlier.

Upgrading one side of a Link connection is safe; see *Link*.

**Rolling back needs a restore.** A 7.12.0 wheel refuses to open a schema-87
database, and War Dialer 7.12.0 refuses a schema-11 world. **MANUAL — to roll
back:** stop NetBBS, install the 7.12.0 wheel, then restore the backup taken
before the upgrade, including the War Dialer world. Anything since the upgrade
is lost with it: posts, replies, pins, follows, withdrawals, board and reader
color settings, rejection records, notices, dial-in addresses and the node
map level.

On 7.12.0, Voidrunner reads careers from the old home directory again.
Careers played since the upgrade live in `<database>.doors/voidrunner/`.
To keep them, set `VOIDRUNNER_SAVE_DIR` to that directory before starting
7.12.0. Accounts created since the upgrade are gone after the restore, and
their user IDs can be issued again, so check for careers belonging to them.
If you instead let 7.12.0 use the old directory and later upgrade again, the
node's own directory already holds careers: the node logs a warning and copies
nothing, and careers played during the rollback stay in the old directory.

## Verification boundaries

- **None of this has run on the live test network yet.** The Link changes
  were tested in-process and over loopback: art posts, withdrawals and
  `dial_in` crossing between nodes, #766's last contact over real loopback
  sockets, and real-time contact over a real loopback Noise session. No test
  runs a 7.12.0 node against this one; compatibility rests on the new fields
  being optional, which is what the protocol's rules for optional fields
  provide.
- **The Monitor, snoop and break-in have been tested with the test harness,**
  not with a room full of real callers. Break-in was tested with a real
  Telnet session blocked at a prompt and with the web transport; SSH and the
  local console were not exercised end to end.
- **The screen copy was checked against 12 real captures** (login, menus,
  boards, files, chat, MRC, the SysOp console, the directory, Who, the door
  menu and Voidrunner's Command Deck), and matched the website's independent
  emulator on all 12. A door that does not repaint after a resize can look
  wrong in snoop until it redraws.
- **The screen copy's cost was measured in isolation,** about 3 ms per 11 KB
  of plain text, not on a busy node.
- **Pasted color has been tested with synthetic input,** through the real key
  readers. How each terminal program sends a paste has not been surveyed.
- **The Voidrunner career copy** has not been exercised on a real upgrade of a
  production node.
- **Known gaps, tracked:**
  - The Monitor shows transfers as "Uploading" or "Downloading", without
    progress.
  - The trust explanation hides counted domains and weight until the subject
    is already quarantined (#752).
