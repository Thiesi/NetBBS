# NetBBS v7.12.0

This release covers everything merged since v7.11.0 except the three fixes
already shipped in 7.11.1 and 7.11.2. It has five parts.

- **Carried Link resources.** A SysOp now decides what their node carries. A
  resource past the carry cap is offered instead of silently dropped.
  Deleting something another node originates hides it, where it can be
  restored or purged.
- **Doors.** A door's outbound hook works while the game runs and can speak in
  chat channels. Doors written for another platform can run in a small
  virtual machine per caller.
- **Message boards.** A board is now a list of posts with a reader, instead of
  five full posts on a page. Moderation of Linked boards was fixed.
- **The SysOp console.** Jobs that needed a shell can now be done from the
  console: scheduled backups, installing a release, uploading banner and door
  files, the search indexes, limits and Link policy, and War Dialer
  maintenance.
- **Link and transfers.** Live Link works from behind an HTTP proxy, and
  browser file transfers say why they failed.

**It migrates: the node database goes from schema 72 to 77.** `DOOR_API_VERSION`
goes from 3 to 4. The new version only adds fields, but receipt file names
also changed; see *For door authors*. Other versions stay as they were:
`NETBBS_PROTOCOL_VERSION` is 1, `REALTIME_PROTOCOL_VERSION` is 4, War Dialer
worlds are world schema 10, and Voidrunner careers are save schema 2 with
tactical ruleset 3. No key in `netbbs.toml` changed meaning, though some
settings can now also be set from the console, where an explicit TOML key
still wins. Rolling back needs a restore; see *Upgrade and rollback*.

## For SysOps

### What your node carries is now your decision (#683, #669, #671, #685, #696)

Until now, a Linked board, channel or file area this node did not carry had
no record of why. It could be past the carry cap, deleted by the SysOp, or
blocked by a name already taken here. The resource vanished without a word,
and peers kept resending it in full on every sync pass. Each outcome now has
a place on **Link status**.

- **Counts and caps.** Link status shows carried/cap for boards, channels and
  file areas, where only boards had one before, plus how many resources are
  *offered* and *excluded*. The `[O]ffered` and `E[x]cluded` keys appear once
  there is something under them.
- **`[O]ffered`** lists resources that reached this node and are waiting for
  a decision, with name, description, origin and reason. `[A]ccept` carries
  one even past the cap, and its content arrives over the next sync passes.
  `E[x]clude` declines it. A cap of 0 makes a curated node: everything new is
  offered, and nothing is carried until you accept it. Lowering a cap removes
  nothing you already carry.
- **Deleting a resource another node originates now hides it** instead. It
  stays here, but callers and content administration no longer see it. It
  is not carried and takes no new content. It appears under **`E[x]cluded`**:
  - `[R]estore` brings it back exactly as it was, including your own callers'
    posts and your local moderation, and the next sync fetches what arrived
    meanwhile.
  - `[P]urge` asks you to type its name and then deletes it for real. It
    stays excluded, so it is not carried again unasked.
  - `[R]estore` also takes back a resource you declined or purged. It is taken
    on again from its origin, without what was kept before.
  - A hidden resource's name stays reserved until you restore or purge it.
  - Deleting a resource this node originates still deletes it. If it was
    Linked, it is also listed as excluded, so peers do not send it back.
    Resources that were never Linked delete as before.
- **A Linked resource whose name is already taken here is carried under a
  free name,** `name-` followed by part of its ID. Before this, every node's
  `general` broke the carry of every other `general`, silently. Peers still
  see the original name, and you can rename your copy.
- **A peer running 7.12.0 no longer resends what you do not carry.** This
  node tells such peers which resources it holds a genesis for but does not
  carry, and they skip them. A large declined board used to starve every
  other resource from that peer, because all resources share one per-pass
  budget.
- **A large node's inventory request is sent in pages.** A node holding
  roughly 30,000 or more content IDs used to exceed the responder's size
  limit on every pass. Its pull then stopped for good. The request is now
  split into pages and walked over several passes.
- **Deleting a carried file area with a remote catalogue works.** It used to
  fail on a foreign key after the audit entry had already been written.

**On upgrade**, every resource this node holds a genesis for but has no copy
of is listed as offered. That includes past deletions, cap refusals and
earlier name collisions. Nothing is carried automatically, so there may be a
backlog to review on `[O]ffered`.

### Doors that post while they run, and speak in chat (#520)

A door's outbound hook (off by default, per door) now works during the
session:

- **Requests are picked up every two seconds** while the door runs, up to 16
  per pass, and once more when it exits. A door can read its receipt in the
  same session.
- **A door can speak in chat channels,** with one-line messages such as
  "Sector 7 has fallen." On the door's Outbound screen:
  - `[L]et it chat in a channel` and `[D]rop a channel` manage the channels.
  - `[H]ourly chat lines` sets the chat ceiling: 30 an hour by default, at
    most 240. It is separate from the board ceiling of six posts an hour.
  - Channels bridged to MRC are never offered.
  - Allowing a Linked channel asks for confirmation, because every line
    reaches every peer and chat has no way to take a line back.
- **A channel's moderators can silence a door there** with
  `/mute Blacksite.door [duration] [reason]` and `/unmute`. The mute is
  announced and logged like a caller's mute, and the door's Outbound screen
  shows it.
- **Callers can tell a door's line from a person's.** It is muted and marked
  with `»`, or `>>` without Unicode styling. A peer's door lines are styled
  the same way.
- **Chat lines are not audit-logged one by one,** unlike board posts. Allowing
  a channel is logged, and so are mutes.
- **Test launches now answer the door's requests.** "Test as SysOp" and the
  Emulator capability probe never published, and they still do not. Now each
  request gets a receipt saying what would have happened, instead of no
  answer at all.

See the door guide's section *Letting a door post to boards and chat*.

### Doors from other platforms, in a VM per caller (#474)

A new profile adapter, **`vm`**, runs a door built for another operating
system. Each caller gets their own qemu guest, booted from a kernel and
initramfs you build, and reaches the door through one virtio console. The
guest has no network and no display, and it sees only two shared
directories: the installation at `/mnt/game` and the caller's node directory
at `/mnt/node`.

- **Choose the template** "Linux x86_64 door in a VM (setup template)". The
  capability probe now boots VM profiles as well as DOSBox ones.
- **MANUAL — outside NetBBS:** install qemu and build the guest.
  - Run `pkgin -n install qemu` first; pkgin may propose upgrading unrelated
    packages.
  - Build the guest with `examples/doors/vm/build-alpine-guest.sh`. Keeping
    the image patched is your job.
  - The door guide's *Foreign-platform doors in a VM* section has the full
    contract the guest must meet.
- **Acceleration.** TCG, the default, needs no privilege. It measured about 4 s
  to the first screen and about 3% of one CPU for an idle game. `nvmm` and
  `kvm` are accepted but have not been tested on a real node, and granting
  access to them is a privilege grant on the whole host.
- **Memory.** `memory_mb` must be at least twice `guest_memory_mb` plus 512,
  so 1024 for the default 256 MiB guest.
- **POSIX only.** qemu runs as the service account, so escaping the VM lands
  where a native door already starts.

### Jobs that used to need a shell (#733)

- **Scheduled backups: Operations → Backup → `[S]chedule & destination`**
  (#727).
  - Choose off (the default), daily or weekly, a time in the display
    timezone, a weekday, and how many scheduled backups to keep (1 to 365,
    default 7). Cron is no longer needed.
  - A slot runs once and is never retried. After downtime the node makes
    one catch-up backup. A backup in progress at shutdown finishes first.
  - Retention deletes only backups the schedule made, never manual ones.
  - The **destination** applies to every backup, manual ones and the one before
    an install included. It must be an existing, writable, absolute
    directory, outside file storage, the identity directory, the Voidrunner
    saves and the door receipts. NetBBS never creates it, and it notices when
    the disk behind it is not mounted. Empty means beside the database, as
    before.
  - The dashboard shows when the next backup is due. Copying backups off the
    host and encrypting them stay your job.
- **Install a release: Settings → Update → `[I]nstall`** (#731).
  - It appears on a live node once a check has found a newer release, and
    one install runs at a time. It shows a plan first. It then downloads and
    verifies the wheel, and backs the node up to your backup destination
    before running pip. It aborts if the backup fails.
  - It downloads the release's wheel from GitHub over HTTPS and checks it
    against the SHA-256 digest GitHub publishes for it. That proves the bytes
    are what GitHub holds, not that a maintainer signed them. Dependencies
    are resolved by pip from your configured index, as a manual upgrade does.
  - It then runs pip in the node's own virtual environment. It refuses
    unless NetBBS is installed as a package in a venv the service account can
    write to, and not as an editable or VCS install.
  - `[R]estart after install`: auto, yes or no. Under systemd (auto) or
    with yes, the node shuts down gracefully and exits with status 75 so the
    supervisor starts the new version. NetBSD's rc.d does not restart
    NetBBS, so under rc.d the node keeps running the old version until you
    restart the service yourself; do that promptly, because the new files are
    already on disk. Choose yes only if something restarts NetBBS when it
    exits.
  - **MANUAL — existing systemd units:** add `RestartForceExitStatus=75` and
    `SuccessExitStatus=75`, as in `examples/netbbs.service`. Without them a
    restart for an update is logged as a failure.
  - A failed install shows the step, the reason and the last lines of pip's
    output.
- **Upload banner and door files from your own computer** (#728).
  - Each banner screen gains `[U]pload` for an `.ans` file of up to 256 KiB.
    It replaces that banner's file and never switches the banner on.
  - Content → Doors gains `[U]pload`, which places a file in the node's
    `doors/` folder. It is capped by the node's upload limit, and nothing is
    registered or made executable.
  - Both work over Zmodem or a single-use browser link, which stops working
    if the account is no longer a SysOp. Each upload is audit-logged.
- **The node's own log: Operations → `Node lo[g]`** (#729).
  - It lists `netbbs.log` entries, warnings and errors by default, newest
    first. `[L]evel` and `[O]rder` change the view, and an entry opens with
    its traceback. `[F]ollow` watches new lines.
  - It also works from `python -m netbbs.admin` with the node stopped, which
    is when a log is most wanted.
  - It reads at most the last 512 KiB and never follows a symlink. Browser
    transfer tokens that the web access log writes into `netbbs.log` are
    masked on screen, but the file itself still holds them.
- **Search indexes: Operations → `[S]earch indexes`** (#724). It checks posts,
  files and chat messages on entry and shows what is missing, stale or
  extra. `[R]ebuild` appears when something has drifted.
- **Limits and retention: Settings → `Limit[s] & retention`** (#725). This
  covers the upload cap, the grace period before an expired post is deleted,
  invitation expiry and chat scrollback. Changes apply without a restart.
  **These now have upper bounds**: 64 GiB, 3650 days and 10,000 messages. A
  larger value set earlier with `scripts/set_node_config.py` is treated as
  the maximum.
- **Link, login and shutdown policy: Settings → `Net[w]ork & login limits`**
  (#730).
  - This covers the carry caps, peering and seeds, Link rate and relay
    limits, the login throttle, and the shutdown delays.
  - For each value, a key present in `netbbs.toml`, or a `--link-*` flag,
    still wins. Otherwise the console's value applies, and otherwise the
    default. The screen shows which one is in force.
  - Changes apply at the next start.
  - Bind addresses, ports, `public_url`, paths and `[managed_dns]` stay in
    `netbbs.toml` only.
- **War Dialer: Content → Doors → the door → `[W]orld`** (#726).
  - It shows the world's status, counts and recent SysOp operations.
  - It switches maintenance mode on and off. Maintenance refuses while
    anyone is inside.
  - With maintenance on, `[N]ext season` starts the next season, and
    `Reset [c]ompetition` does the same and also clears receipts. Each asks
    for a reason and the world's exact filename, takes and verifies a full
    node backup, and then makes the change. The node no longer has to be
    stopped for this, as the command-line tool still requires. A caller
    still inside stops the change with nothing altered, and maintenance stays
    on afterwards. This needs the live node's console.
- The hidden Settings shortcuts to Diagnostics and the log follower now work
  on a node without Link, as the Operations screen already did (#732).

## For callers

### Message boards: a list of posts and a reader (#679, #710, #676, #677, #680)

- **A board opens as a list,** one row per post: number, subject, author and
  date, with `new` markers. It fits the terminal, about 6 to 12 rows at 80×24.
  - Move with Up and Down, and open a post with Enter, or with its number for
    the first nine rows.
    `[O]lder`, `[N]ewer` and `[R]ecent` page as before.
  - The header shows where you are, how many posts are new, whether the board
    is Linked, and, if you cannot post, why.
  - The board picker gains an ACTIVITY column: caught up, N new, or not
    visited yet.
- **A post counts as read once you open it** (#710), not when the list shows
  it. The list has `[M]ark all read` when something is unread, and
  `[N]ew scan` has `[M]ark read` per board. On a first visit, a board's
  existing posts count as read. Your own posts always count as read.
  Up to 500 posts opened out of order are remembered per board.
- **A post opens in a reader,** with PgUp and PgDn for long bodies and a byline
  showing author, date, *edited*, *new* and what it replies to.
  - It offers `[E]dit`, `Remove pos[t]`, `[N]ext post` and `[P]revious post`,
    and `[B]ack` returns to the list with the cursor where it was. The old
    "which post number?" prompts are gone.
  - The mail reader and the SysOp's pending-post review now use the same
    reader.
- **Outcomes stay on screen.** With redraw-in-place on, a result such as
  "Posted.", "Message deleted." or a refused send used to be wiped by the
  next redraw. It now sits above the prompt. This includes the one-time
  browser transfer link, which a caller without Zmodem could lose before.
- **Edits are reviewed like new posts** and saved with `[S]ave`. A refused edit
  stays in review with its text, instead of being lost.
  - Subject and recipient prompts open on the current value.
  - A moderated board says "Submitted. It will appear once a moderator
    approves it." instead of a post ID. A moderated edit says the post keeps
    its current text until a moderator approves it.
- **Empty screens wait.** An empty board you cannot post to, or an empty file
  area with nothing to do, now waits for `[B]ack` instead of flashing past.
- **Local board fixes:**
  - A removed post no longer also shows `[edited]`.
  - The line editor no longer refuses every change to a long body.
  - `P` then Enter no longer cancels a new post.
  - A closed board says it is closed and does not offer `[P]ost`.
  - Removal is one verb everywhere, *Remove*, and on a board carried from
    another node the confirmation says it removes the post on this node only.
  - Approving something no longer pending is refused, instead of crashing the
    console or bringing an expired post back.
  - A board's or file area's maximum age must now be at least one day.
- **Linked boards, moderated here:**
  - Approving a held carried post no longer sends the network a duplicate
    copy signed as this node's.
  - Approving an edit publishes an edit.
  - A carrying node's own *Moderated* setting now holds remote posts and
    edits. **After the upgrade a moderated Linked board may show new pending
    items.**
  - A remote edit no longer undoes your removal.
  - A probationary author's edits are held like their posts.
  - Posts hidden by trust no longer empty a page or count toward new posts and
    replies. Search no longer returns them, nor hidden carried chat messages.

### Chat: a closed channel moves you out (#716)

When the SysOp deletes a channel, hides a carried one or retires an MRC room,
everyone inside is moved to the channel list at once with "#name was closed
by the SysOp." The SysOp is told how many caller sessions were moved. Before,
callers stayed in a deleted channel, and leaving it could fail.

### Browser file transfers say why they failed (#511)

A browser download used to save an error page as the file when the link had
expired or the server was busy. The page now checks first and shows the
refusal in the server's words, with "Try again" when the server is busy.
Downloads still stream.

For reverse-proxy operators:
- **`HEAD` on a transfer link now returns what `GET` would:** 200, 403, 404
  or 429, with the reason in `X-NetBBS-Transfer-Message`. Before, it always
  returned 204. A `HEAD` never uses up a single-use link.
- **The browser now fetches `transfer/<token>` relative to the page's own
  URL,** keeping a path prefix such as `/bbs/`. Before, it fetched the
  `public_url` link. This also fixes browser uploads behind a path prefix, or
  with a `public_url` on another origin, which used to fail.
- **Uploads count the whole multipart request** and are refused with 413 once
  it passes `max_upload_bytes` plus 64 KiB. `max_upload_bytes` still limits
  the file itself.

## Link: live traffic through an HTTP proxy (#682)

A node whose only way out is an HTTP proxy had boards, mail and files, but no
live chat, direct messages, anchors or relays. Real-time Link now tunnels
through `CONNECT` when `HTTP_PROXY` or `HTTPS_PROXY` names a proxy for the
target.

- `NO_PROXY` is honoured, and loopback targets always connect directly.
  `ALL_PROXY` is ignored.
- Proxy Basic authentication comes from the proxy URL, or else from netrc.
- A proxy that inspects TLS breaks live Link; Link status shows it as
  "tunnel opened, handshake failed".
- Only `http://` proxies work. A SOCKS or `https://` proxy makes the
  connection fail; it never falls back to a direct connection.
- Link status gains a **Live proxy** line showing the proxy and its last
  outcome. Credentials are never shown.

The SysOp Handbook's new section *Behind an HTTP proxy* explains the setup,
including a port-443 arrangement for reliable nodes.

## For door authors

`door_api` is now **4**. It only adds fields: `outbound.channels` and
`outbound.chat_lines_per_hour`, and requests of the form
`{"channel": "lobby", "body": "..."}`.
- A chat line is one line of at most 4000 bytes, with control characters
  removed.
- The board request is unchanged.
- A door that refuses versions it does not recognize must accept 4.

Two changes from earlier in this release:
- **Receipt names gained a per-answer part**,
  `<launch>.<sequence>.<request>.result.json`. A door that followed the
  handbook and matched on each receipt's `request` field is unaffected. A door
  that built the name itself will no longer find its receipts.
- **A request is claimed before it is read,** so each is answered exactly
  once.
- **A SysOp's test launch now answers requests** with `"status": "rehearsal"`
  and a `"would"` field saying what a real session would have got. Do not
  report such a post as made. Before, a test launch wrote no receipts.
- **A VM door's receipts** are a copy under `/mnt/node/outbound-results`,
  topped up during the session.

The Developer Handbook's *Door outbound posting* section has the full
contract, including the new refusal reasons.

## Upgrade and rollback

Take a backup, stop NetBBS, replace the wheel and start it. Five migrations
run:
- **The carry-decision record.** Every resource held without a copy becomes
  offered.
- **A `link_hidden_at` column** on boards, channels and file areas. It starts
  empty, so nothing is hidden.
- **The door chat tables.** Every door keeps its board allowlist and budget,
  gains a chat ceiling of 30, and has no channels until you allow one.
- **The scheduled-backup record.** It starts empty, so backups you already
  have are never pruned.
- **Read-on-open for boards.** Each caller's existing position on each board
  becomes their read floor, so nobody's history is reset.

Later updates can be installed from Settings → Update. See the systemd note
under *Jobs that used to need a shell*.

Upgrading one side of a Link connection is safe. The Link changes are
negotiated: a node advertises what it understands in its signed descriptor,
and a 7.12.0 node uses the new inventory fields only with a peer that
advertises them. With a 7.11.x peer it behaves as before, and
declined resources are still resent between them. A large inventory request
is split into pages, so it is no longer refused, but pulling from an older
peer may make little progress until that peer upgrades.

**Rolling back needs a restore.** A 7.11.x wheel refuses to open a schema-77
database. **MANUAL — to roll back:** stop NetBBS, install the 7.11.2 wheel,
then restore the backup taken before the upgrade. Anything since the upgrade is lost with
it: accept, exclude, hide and purge decisions, door chat allowlists and
mutes, and all new content. A door profile using the `vm` adapter does not
load on an older build. Settings made in the new console screens are lost
with the restore: backups go back beside the database and are no longer
scheduled, and policy values revert to `netbbs.toml` or the defaults.

## Verification boundaries

- **Most of this has not run on the live test network yet.** The Link
  changes have been tested in-process and over loopback against two-node
  HTTP setups: carry decisions, hide and restore, `not_carried`, paging and
  the proxy tunnel. The proxy has been tested against a real loopback
  `CONNECT` proxy, not the proxy-only test node.
- **Compatibility with 7.11.x peers is reasoned, not measured.** The new
  capability list is an extra key in a signed payload that 7.11.0 never
  validates key by key, the same way `relays` was added before. No test runs
  a 7.11.x node against this one.
- **VM doors have run one real game end to end:** Amiga Empire on NetBSD under
  TCG. `nvmm` and `kvm` are accepted but not certified, and neither is any
  guest besides the recipe's. Guest terminal size is fixed for the session.
- **The door tests that need POSIX ran on NetBSD (Roanoke), not in CI.** The
  VM tests, and the outbound tests that need symlinks or directory handles,
  are skipped on Windows, where the full suite runs.
- **Installing a release from the console has been tested up to the
  download.** A smoke test downloaded and verified the real v7.11.2 wheel.
  No end-to-end pip
  install into a running service has been done, and the tests never install
  into a real environment. Downloaded wheels are kept in `<database>_updates/`
  and are not cleaned up.
- **Scheduled backups have not had a real multi-day run.** Console policy
  values have not been restarted into on a live node.
- **Known gaps, tracked:**
  - A file in a hidden file area can still be served to peers by its ID.
  - Search-index rows of purged boards and file areas remain.
  - Rejecting a held carried post does not survive a later
    `[R]epair carried posts`.
  - Colour work on the board screens is #711.
