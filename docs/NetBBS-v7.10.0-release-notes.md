# NetBBS v7.10.0

This release is mostly what playing and administering a real node turned
up. Nine defects came from playing both bundled doors on a live node
(roadmap tracker #612 step 6). Other changes followed from reading the
SysOp console on a 24-row terminal, and from watching two managed names
sit silently unregistered. On the foundation side, two Link fixes let
nodes that share a board through a common seed verify each other's
content, and let a node nobody can dial get its signed trust objects to
subscribers.

**This release migrates the node database, 70 to 72.** A node that has
run 7.10.0 cannot be opened by an older wheel. Rolling back means
restoring a backup, not swapping the wheel. War Dialer worlds stay at
world schema 10 and Voidrunner careers at save schema 2, but **Voidrunner
fights now use tactical ruleset 3**. A career saved in the middle of a
fight cannot be resumed by 7.9.0 (see *Upgrade and rollback*).
`NETBBS_PROTOCOL_VERSION` is 1, `REALTIME_PROTOCOL_VERSION` is 4,
`DOOR_API_VERSION` is 3, and no setting in `netbbs.toml` changed.

## For callers

### Level changes apply without logging off again (#659)

A SysOp's change to a caller's level, or to their identity-verifier
permission, reaches a caller who is already connected. It no longer
waits until they log off and on again. This works whether the change
was made in the node's SysOp console or with `python -m netbbs.admin`.

- **A raised level** interrupts nothing. The main menu shows the new
  options straight away if the caller is sitting on it. Otherwise they
  appear when the caller comes back to it, with a line such as
  "Your access level is now 50."
- **A lowered level** takes the caller out of whatever they are doing and
  back to the main menu, redrawn for the new level, with the same line.
  That includes a door game, which is ended. Text they were composing in
  an editor is kept as a draft.
- A demoted SysOp can no longer keep using the console they were sitting
  in until they log off. The console also checks its own operator at
  each key, which is what stops a demoted operator in the standalone
  CLI.
- Changes made in the node's own console apply at once. Changes made by
  another process apply within about five seconds.
- Drain's exemption for SysOps follows the new level.

A related fix: both fullscreen editors now save the text on screen as a
recoverable draft when a session is cut off. Before, only what the last
30-second autosave had caught survived a disconnect.

### The file area speaks the same keys as every other screen

The file-area listing was the last screen that read typed lines. It
offered `/download`, `/upload`, `/describe`, `/weblink` and `/remote`
behind a `Choice or command:` prompt that nothing on screen taught. It
is now keystrokes only:

- a number key, or Enter on the cursor, downloads that file;
- `[D]ownload` acts on the cursor, on the only file on the page, or asks
  which file;
- `[U]pload`, `[E]dit description` and `[W]eb transfer` keep their keys;
- `/remote` becomes `[L]ink catalogue`;
- an empty area shows `[B]ack` and no longer drops the caller out on an
  unrecognized key.

Three bugs turned up on the way. A number key could send the wrong file
when two files in an area share a name. `²` (AltGr+2 on a German
keyboard) crashed the screen. A file removed from storage while its page
was open escaped as an error instead of being reported.

Two reaches the typed commands had are kept:
- `[E]` on the listing offers a caller their own uploads that are still
  awaiting approval.
- The SysOp's pending-file review screen gains `[D]ownload`, so a
  moderator can fetch an upload before approving it.

One reach is gone and not yet replaced. An **expired** file can no longer
be reached from a terminal at all. §16 of the design document now
decides what expiry means (#639): expiry ends a caller's reach, and a
SysOp gets a recovery screen. That screen is **not built** in this
release; see *Verification boundaries*.

### MRC starts with the rooms it last saw (#636)

The hub lists rooms only to a caller who is already in one, so the Multi
Relay Chat section began every node run with `lobby` alone. The last
complete listing is now kept across restarts, bound to the hub it came
from. It is shown with its real age until a caller's entry refreshes it,
and dropped after seven days. A room the hub no longer lists leaves
the directory. A caller's first MRC room of a session gets one line
saying how many other rooms there are and how to reach them. The node
still never asks the hub for a listing by itself.

### Voidrunner

- **A way out of a fight the ship cannot win (#647).** A raider
  *outclasses* a ship when two unguarded volleys would break its full
  hull, which for a Shuttle on its factory hull means a tier-4 raider.
  Against one, `[D] Dump` gives up half the hold and breaks contact three
  times in four, and `[E] Evade` has a floor of 30%. The combat panel
  marks such a contact `OUTCLASSED` and leads with the Dump it will make.
  The chart warns before departing for a charted system whose raiders
  can outclass the hull. Concord patrols are unchanged. This is tactical
  ruleset 3. A fight keeps the ruleset it started under.
- **Retirement ends on an ending (#644).** A new *Career Complete* screen
  shows the ending, the ship that flew the career, the career in numbers
  and its highlights, and what the next pilot starts with. It is drawn
  after the retirement is saved.
- **Notices before the first screen are seen (#641).** The First Flight
  pointer, the returning pilot's welcome and recap, and the new-career
  note used to be wiped by the first screen's clear. They now appear on
  the first Command Deck. A resumed journey gets a *Journey Resumed*
  screen that waits for a key.
- **Monochrome and Plain redraw in place (#642).** Those two presets
  stripped the clear-screen along with the color, so every screen was
  printed under the last.
- **The star map and list pickers replace the screen before them (#643)**
  instead of stacking copies of themselves.

### Both bundled doors follow a terminal resize (#645)

Voidrunner and War Dialer now redraw at the new size when a caller's
terminal is resized. Before, they kept the size they were launched with
until the caller quit and came back. The node signals only its own copy
of a bundled door, with no profile setting needed, unless its door
profile fixes the screen size. A copy of the script installed elsewhere
follows the door profile's switch like any other door. Retro Trivia does not follow a resize. The door guide explains the
details.

### War Dialer

A caller who arrives while the SysOp has the world closed for maintenance
is told so in one line. The door no longer reports that as a crash, and
door history records an ordinary exit (#646).

## For SysOps

### A console you can read on a 24-row terminal

The SysOp console had three problems:
- Status screens were walls of `Label: value` sentences in one color.
- Several screens were taller than the terminal, including the landing
  dashboard itself at 32 rows.
- Anything written just before returning to a menu was erased by that
  menu's redraw, so most action results were never seen.

All three are fixed:

- **Detail panels.** Every status and detail screen shows labels, values
  and section headings in three colors with one value column. Long
  screens page with `PgUp`/`PgDn` and `[N]`/`[P]`. Link status and a
  peer's detail, Outbox, Diagnostics, Audit log, Backup, Managed DNS, MRC
  status, the trust screens and every resource's detail screen use it.
- **The landing dashboard fits**, falling back to a compact panel and
  then to an undescribed menu when needed.
- **Results are shown above the next prompt.** About 180 actions that
  used to flash past now leave their result line on the screen drawn
  next. Seventeen "Press any key" pauses that only worked around the
  erase are gone.
- Prune drafts and a single Outbox item no longer ask yes/no: the action
  is a key on the screen that shows what it will do. A user's admin
  history is its own paged `[H]istory` screen.
- Breadcrumbs say `Settings` where they said `System`.

`scripts/sysop_gallery.py` renders every console screen for review.

### Managed DNS says when a registered name is not being kept alive (#640, #634)

A registered managed name goes live only after about a day of
uninterrupted check-ins, and before this release a node that was not
checking in said nothing. Now:

- Registering sends the first check-in straight away, and the result
  line says if it failed and why.
- Every reason a check-in pass sends nothing is recorded and shown on
  the DNS screen: an unreadable or missing credential, no service
  address, a credential issued by another service, the opt-in not
  accepted, no usable answer, or a pass that failed. A name the service
  has never heard from shows `Last contact: never`. When a problem is
  recorded, the screen explains it instead of saying "Nothing to do".
- The background updater survives a pass that fails and retries at the
  next one. Before, one exception ended it for the rest of the node's
  uptime.
- Accepting a managed name when creating the first SysOp with
  `python -m netbbs.admin`, before the node has ever started, no longer
  dead-ends. The name editor opens once at the next SysOp login or the
  next run of `netbbs.admin`.

Check-ins still connect to the service directly and never through an
HTTP proxy, because the service publishes the address they arrive from.
**A node with no direct outbound route cannot keep a managed name
today.** When a check-in cannot reach the service, the DNS screen says
that check-ins never go through an HTTP proxy.

### Documentation

- The SysOp Handbook documents custom banners and mastheads: the file
  names, and that each one must also be enabled on its own screen (#635).
- The Handbook also says what callers see when their level changes.

## Link

### A carrier can introduce the author of what it carries (#630)

Two outgoing-only nodes cannot dial each other. Two nodes that share a
board through a common seed had therefore usually never met. A node
could verify nothing signed by a node it had not met, so their posts
were refused and downloaded again on every pass.

- A carrier now serves the author's own hello bundle on request, and the
  bundle verifies against itself. New route:
  `POST /link/v1/identities/{fingerprint}`, where the fingerprint is
  the requesting node's own.
- An **introduced** identity verifies carried content it signed and
  nothing else. It is not a peer: pushing, pulling, relaying, Link mail
  and file fetches still need a completed hello.
- An introduced node is a trust subject on probation that the SysOp can
  establish. Its callers' first posts arrive pending approval.
- A carrier's response is handled one event at a time. An event that
  cannot be used yet is set aside and asked for again later, instead of
  failing the whole response.
- Migration 71 adds `link_introduced_identities` and the
  `link_known_identities` view.

### A node nobody can dial gets its trust objects out (#627)

A vouch signed by an outgoing-only node was stored and served to nobody,
because signed trust objects are pulled from their issuer.

- Such a node now deposits what it signs at the nodes that relay for it,
  using the new route `POST /link/v1/trust-deposit/{fingerprint}`.
- A relay accepts deposits only from nodes it has agreed to relay for,
  and only that node's own objects.
- Carried objects are kept apart from admitted ones. A relay admits them
  only if its own SysOp has named the depositor a reporter.
- Subscribers pull from the reporter's relays when the reporter cannot
  be dialed.
- The vouch screen says how a vouch leaves such a node, and warns when no
  relay carries it yet.
- Migration 72 adds `link_trust_carried_objects`,
  `link_trust_carriage_marks` and `link_trust_deposit_cursors`.

Attestations are not carried this way yet (#632). The Published identity
screen and the Profile toggle say so on a node nobody can dial.

**Both of these need the other side on 7.10.0.** A carrier or relay still
running 7.9.0 does not have the new routes.

## Upgrade and rollback

Replace the wheel and restart. **The node database migrates 70 → 72.**
Migration 71 adds the introduced-identity table and view, and 72 adds
the three trust-carriage tables. Nothing existing is rebuilt or
discarded. The MRC room listing, the managed-DNS contact state and
whether this node can be dialed (`link_outgoing_only`, recorded at each
Link start) are new `node_config` keys and need no migration.

**Rolling back needs a restore.** `_apply_migrations` refuses a database
whose schema is newer than the build understands, so a 7.9.0 wheel will
not open a database that 7.10.0 has run on. Back up before you upgrade,
and roll back by installing the old wheel and restoring that backup
with it. No `netbbs.toml` setting changed, so the config file needs
nothing either way.

**MANUAL — Voidrunner careers saved mid-fight:** a fight started on
7.10.0 is saved under tactical ruleset 3, which 7.9.0 does not know.
After a rollback, such a career is refused with "This fight uses an
unsupported tactical ruleset." Careers saved outside a fight are
unaffected. Fights saved on 7.9.0 resume on 7.10.0 under their old
ruleset.

**MANUAL — a managed name registered with `python -m netbbs.admin`:**
run `netbbs.admin` as the node's own account, not as root. A credential
written by root is unreadable to the node. Before this release that
stopped the updater for the node's whole uptime. It now shows on the DNS
screen as an unreadable credential. If you registered as root, `chown`
the credential file to the node's account; the node picks it up at its
next pass.

**MANUAL — a node whose only way out is an HTTP proxy:** it can register
a managed name but cannot keep it, because check-ins must connect
directly. The DNS screen shows each failed check-in. There is no
workaround in this release.

## Verification boundaries

- **The resize handling (#645) has not run in the test suite on a POSIX
  host.** Windows has no `SIGUSR1`, so the POSIX-only tests skip on the
  development machine and there is no POSIX CI. The shipped scripts were
  probed with real signals on NetBSD 11.0 outside the suite, including
  a signal in the first second of the door's life.
- **The expired-file recovery screen (#639) is decided, not built.** In
  this release neither a caller nor a SysOp can reach an expired file
  from a terminal. This matters only for areas that set a maximum file
  age.
- **Introduction and trust carriage (#630, #627) are tested in-process.**
  Three-node loop harnesses run under the enforced trust policy,
  including a key rotation learned through a relay and a relay restored
  from backup. They have not yet run between real nodes. No production
  path rotates a key (#624).
- **The root-owned credential behind one of the silent managed names
  (#640) is a hypothesis.** It follows from the code. The node's log has
  not been read to confirm it.
- **The Voidrunner combat odds (#647) are a first setting.** The 30%
  Evade floor and the half-hold Dump were chosen from simulated trials,
  not from play.
- **Career Complete (#644) has no gallery panel**, because no played
  career reaches a finale within a gallery build. It was reviewed from
  screenshots of a career whose save was edited to Legend standing.
- **Codex was over its review quota** for most of these pull requests.
  Adversarial read-only review agents and the Claude Code Review action
  reviewed them instead.
