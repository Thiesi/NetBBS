# NetBBS SysOp Handbook

This is a reference for running a NetBBS node day to day: what each SysOp
feature is for, how to reach and use it, and what its failure/edge-case
behavior looks like. It assumes no prior NetBBS development history.

It is not the design document. `docs/NetBBS-design-doc.md` is the
authoritative specification of product and protocol decisions — the "why"
and the exact rules. This handbook is the "how do I actually do this
tonight" companion; where the two disagree, the design doc wins, and this
document should be corrected to match it.

It is also not the engineering worklog. `docs/NetBBS-worklog.md` records
non-obvious implementation invariants for developers changing the code. If
you are running a node, not modifying it, you don't need that file.

While you're at a live SysOp prompt, look for **Ctrl-H** — many screens now
show short contextual help for their fields inline, when it's been
authored (a `(Ctrl-H for help on these fields)` hint appears under the
menu when it has). The fullscreen post/bio editor has its own equivalent,
**Ctrl+G**, listing its keybinds. Neither replaces this handbook; both are
meant for "what does this specific field do, right now," not the full
picture.

## Contents

1. [Reaching the SysOp console](#1-reaching-the-sysop-console)
2. [Accounts and registration](#2-accounts-and-registration)
3. [Identity attestation and name requirements](#3-identity-attestation-and-name-requirements)
4. [Resource gates](#4-resource-gates)
5. [Content areas: boards, files, channels, categories](#5-content-areas-boards-files-channels-categories)
6. [Communities](#6-communities)
7. [Permissions and moderation](#7-permissions-and-moderation)
8. [Chat](#8-chat)
9. [Mail](#9-mail)
10. [Post drafts: save and resume](#10-post-drafts-save-and-resume)
11. [Node operations: sessions, maintenance, drain, shutdown](#11-node-operations-sessions-maintenance-drain-shutdown)
12. [Trust policy and Link federation admin](#12-trust-policy-and-link-federation-admin)
13. [System settings](#13-system-settings)
14. [Backup and restore](#14-backup-and-restore)
15. [Diagnostics and troubleshooting](#15-diagnostics-and-troubleshooting)
16. [Getting more help](#16-getting-more-help)

---

## 1. Reaching the SysOp console

Any account at level 255 (`SYSOP_LEVEL`) sees a `[S]ysOp` option on the
main menu. Picking it opens the **SysOp operations console** — a
dashboard, not a plain menu: it shows node mode (ONLINE/MAINTENANCE/
LOCKDOWN), active session count, Link health (if Link is enabled),
moderation queue totals (pending users/posts/files), backup and update-
check recency, and recent Link diagnostics, all before you pick anything.
Press `[R]` to refresh it after taking an action elsewhere.

From the console:

- `[U]sers` — accounts, registration mode, promote/demote, enable/disable,
  identity-verification grants, deletion. See §2.
- `[C]ontent` — boards, file areas, channels, categories, Communities,
  granting/revoking moderator authority. See §5–§7.
- `[O]perations` — node/session control, Link status, outbox, diagnostics,
  log tailing, carried-post repair, backup status. See §11 and §15.
- `[S]ettings` — welcome banner, node name, joining NetBBS Link through
  the reliable nodes, update checks, timestamp format, trust policy. See
  §12–§13.
- Quick actions (`[N]ode`, `[L]ink status`, `[X]outbox`, `[K]ackup`) jump
  directly to the same screens `[O]perations`/`[S]ettings` reach, when
  their context is available (e.g. `[N]ode` only appears in a live
  session, never from the standalone admin CLI below).
- `[B]ack` returns to the main menu.

There's a second, non-interactive way in: `python -m netbbs.admin` runs
the same admin screens against a database file directly, without a live
session. It has no access to a running node's in-memory state, so
anything session-specific (who's currently online, live maintenance mode,
scheduling a drain) is unavailable there — the console tells you this
plainly ("Live node controls unavailable in standalone mode") rather than
silently omitting the option. Use it for account/content administration
when the node process isn't running, or when scripting routine
maintenance.

Historical `[M]anage`/`[S]ystem` single-letter shortcuts from an older
menu layout are still silently accepted (compatibility aliases) but no
longer shown — don't expect to find them documented anywhere on screen.

---

## 2. Accounts and registration

### Levels

NetBBS has one integer level per account, not a separate "is SysOp" flag.
`SYSOP_LEVEL = 255` is the reserved top level. Every gate in the system
(board read/write, file access, channel join, admin menu access) compares
against this one number.

The node will never let you leave yourself with **zero usable SysOps** —
promote, demote, disable, enable, approve, and delete operations that
would leave no account at level 255 that is also enabled and not pending
approval are rejected outright. You cannot lock yourself out this way even
by accident.

### Registration mode

`[U]sers` → `[R]egistration` sets one of three modes for the whole node:

- **open** — self-registration creates an immediately usable account.
- **approval required** — self-registration creates a pending account
  that cannot log in until a SysOp approves it.
- **closed** — no public registration option at all; accounts are
  SysOp-created only.

This screen also shows how many accounts are currently waiting on
approval. Approving (or rejecting, by simply not approving) a specific
pending account happens on that account's own detail screen, not here —
`[L]ist users`, pick them, `[A]pprove`.

Registration mode controls whether an account can exist and log in at
all. It has nothing to do with what an *active* account is trusted to do
over Link (that's trust/reputation — see §12) — these are deliberately
separate axes.

### Managing a specific account

`[U]sers` → `[C]reate user`, or `[L]ist users`/`[P]romote/demote`/
`[E]nable/disable`/`[D]elete user` (these four all land on the same
per-account detail screen, just opened with a different picker title —
there's no separate flow for each). From that detail screen:

- `[A]pprove` — only shown for a pending account; activates it.
- `[L]evel` — set a new integer level. Blank cancels.
- `[T]oggle enable/disabled` — disabling immediately terminates any live
  session that account has open.
- `[I]dentity verification` — grants or revokes `can_verify_identity` (see
  §3). This is a narrow, separate permission, not another moderator tier
  — it only ever controls whether the account can attest *other* users'
  age/name, nothing else.
- `[D]elete` — permanent. You must type the exact username to confirm.
  Content they authored keeps its recorded author label (posts/files
  don't become anonymous or vanish); moderator grants, channel
  membership/invitations, preferences, and blocklist entries tied to the
  account are removed. This cannot be undone.

---

## 3. Identity attestation and name requirements

This is the concrete example new SysOps have specifically asked about, so
it gets full treatment here.

### The three states

Every board, file area, and channel (and a Community's own default, which
they inherit from — see §4) can set `name_requirement` to one of three
values:

| Value | What it gates | What it shows |
|---|---|---|
| `none` | Nothing. | Nothing. |
| `verified` | Caller must have a verified name attestation on file to post/join. | Nothing — the verification is required, but never displayed anywhere. |
| `verified_and_displayed` | Same requirement as `verified`. | The caller's attested real name is shown alongside their posts, in this resource's own rendering, as `display_name_or_username (=Verified Real Name=)`. |

A verified name **never overwrites** the account's own chosen display
name — it's shown alongside it, in the `(=...=)` form, using a dedicated
color so the marker survives even with color stripped. Disclosure is
resource-scoped: a resource that requires `verified_and_displayed` shows
the real name there and nowhere else the account didn't also require it.

If a resource has `name_requirement` set to `verified` or
`verified_and_displayed` and the caller has no verified name attestation
at all, access fails closed — they simply can't post/join there, with no
override.

### Setting it: the toggle, not typed text

When creating or editing a board/file area/channel/Community, the shared
draft-based editor screen shows a `[Q]uirement` field. **Press `Q`
repeatedly** to cycle `none → verified → verified_and_displayed → none →
...` — you do not type the value. (This used to require typing the exact
literal string `verified_and_displayed`; that's gone.) The screen also
shows `(Ctrl-H for help on these fields)` when help text is available —
press Ctrl-H there for a one-line reminder of exactly this table.

The same toggle exists for a Community's own `default_name_requirement`,
which every board/area/channel in that Community inherits unless it sets
its own explicit value (see §4 for the inheritance rule).

### Actually attesting someone's identity

Setting `name_requirement` only sets the *gate*. Someone still has to
attest the age/name for a given account before that gate can be satisfied.
Two separate things have to be true:

1. **Someone needs `can_verify_identity`.** This is a SysOp-only grant
   (`[U]sers` → pick the account → `[I]dentity verification`), independent
   of moderator status — a granted verifier does **not** need to be a
   SysOp or a moderator of anything. A SysOp always has this ability
   implicitly.
2. **That verifier uses the `[V]erify` option on the main menu** (visible
   only to accounts with `can_verify_identity` or SysOp level — it isn't
   in the admin console at all, since a granted verifier may have no
   other admin access). They pick a target account, see that account's
   self-reported birthdate/display name plus any currently attested
   values, and choose to attest a birthdate and/or a real name. This
   overwrites any previous attestation for that attribute.

Verified values always take precedence over self-reported profile values
when a gate is checked. Age is computed from the attested birthdate at
check time — it's never stored as a stale precomputed number.

Attestations can optionally be shared with other Link nodes (opt-in,
defaults off, resets to off on every re-verification) — that's a
per-account preference on the account's own profile screen, not something
a SysOp sets on their behalf.

---

## 4. Resource gates

Boards, file areas, channels, and Communities share four gate types:

- minimum read level (boards/areas only — channels use a single join gate,
  not separate read/write);
- minimum write level;
- minimum age;
- name requirement (§3).

All of these are **nullable**, and null has a specific meaning: *inherit
the containing Community's default, if any, otherwise fall back to the
system default.* Setting an explicit value — including `0` or `none` —
overrides inheritance entirely, even if that makes the resource *looser*
than its Community's own default. A Community default is a default, not a
mandatory floor or ceiling.

In the shared draft-based editor screens, this shows up as "blank = keep
[current]" for text fields and "`none` = clear" conventions for the
numeric gates — clearing a field puts it back to inheriting, it doesn't
set it to a hardcoded 0/none value.

---

## 5. Content areas: boards, files, channels, categories

`[C]ontent` from the console reaches all of these. Each has the same
overall shape: `[C]reate`/`[L]ist` (list opens a picker, which lands on a
per-item detail screen with `[E]dit`/`[D]elete` and area-specific actions),
plus a dedicated `[C]ategories` screen shared across all three content
types.

### Boards

`[C]ontent` → `[M]essage boards`. A board's detail screen additionally
offers `[P]ending` — the approval queue for posts awaiting moderation
(only relevant if the board is set to require approval; see §7). If Link
is enabled and the board isn't already Linked, `[L]ink this board`
promotes it into Link scope.

Boards support categories, immutable revision history for edits (an edit
is a new revision, not destructive overwrite), and per-post pin/expiry-
exemption (governed by the same edit permission, not a separate one).

### File areas

`[C]ontent` → `[F]ile areas`. Same create/list/edit/delete shape as
boards, plus a pending-uploads queue and a `[G]C storage` action —
reference-aware garbage collection for orphaned file blobs. GC always
shows a dry-run report (what *would* be reclaimed) before asking you to
confirm actually reclaiming it; it's a one-way filesystem operation the
database can't undo, hence the two-step confirmation.

File bytes are node-local. Over Link, only catalogue/descriptor metadata
is distributed — files are fetched on demand in bounded chunks, not
mirrored to every node automatically.

### Channels

`[C]ontent` → Cha`[N]`nels. Channel visibility (listed/hidden) and join
policy (open/members-only) are independent settings — a hidden-but-open
channel is reachable by anyone who knows to `/join` it by name; that's
obscurity, not real access control, so don't rely on it as one.

### Categories

`[C]ontent` → `[C]ategories` manages the shared two-level category
structure (top-level categories with optional subcategories) used by
boards, file areas, and channels alike.

---

## 6. Communities

A Community is a topic-level container above boards/channels/file areas —
it does not merge or change how those work, it just groups them and
supplies inherited defaults (§4) plus Community-scoped moderator grants
(§7). Every board/area/channel has zero or one Community; "Uncategorized"
means no Community assigned, not a real row you can edit.

`[C]ontent` → C`[O]`mmunities: create/list/edit/delete, same shared-editor
shape as everything else, including the `[Q]` name-requirement toggle for
`default_name_requirement`.

**Deleting a Community** shows you the blast radius before you confirm:
every member resource is set back to "no Community" (never deleted), and
any Community-scoped moderator grants are revoked. Nothing about the
boards/areas/channels themselves is touched otherwise.

A Community can also be promoted into Link scope, the same object
announced via a signed Link event rather than a separate type. Two
same-named Link Communities from different origin nodes stay distinct —
there's no cross-node name collision merging.

---

## 7. Permissions and moderation

### Granting/revoking moderator authority

`[C]ontent` → `[G]rant moderator` (or `[R]evoke moderator`). Pick a
target account, then a **scope**:

- `[B]oard` / `[A]rea` / Cha`[N]`nel — one specific object.
- `[X]` / `[Y]` / `[Z]` — blanket across *all* boards / all areas / all
  channels on this node, including ones created later. You'll be asked
  whether to narrow that blanket to just one Community's resources
  instead of the whole node.

Then a preset:

- Boards/areas: **Full moderator** (edit + delete + approve) or
  **Approver only**.
- Channels: **Full moderator** (edit + moderate + manage members) or
  **Moderator only**.

A moderator does not need to be a SysOp — these are genuinely separate.
Only a SysOp can grant/revoke authority or change node configuration in
the first place. A blanket grant scoped to Link resources does **not**
imply local authority over anything, and vice versa — a person who
legitimately needs both gets both grants explicitly, there's no automatic
crossover.

Every grant/revoke, and every moderation action taken under it, is
audit-logged.

### Pending-content queues

If a board/area requires approval (set on its own edit screen, alongside
the other gates), new posts/uploads sit in a pending queue until a
moderator with approve permission clears them — reachable from that
board/area's own detail screen (`[P]ending`), not from a central sitewide
queue. The SysOp console's dashboard shows the total pending count across
everything, as an early-warning signal, not a place to act from directly.

Local content maintenance follows `active → expired → deleted`, with a
grace period between expiry and actual deletion. This is entirely
node-local pruning — it never becomes a network-wide deletion instruction
for a Linked resource.

---

## 8. Chat

Local real-time chat (channels, `/who`, `/whois`, `/names`, `/list`,
`/join`, `/leave`, `/topic`, tab completion) needs no SysOp configuration
beyond the channel's own gate/visibility settings covered in §5 and §4.
Channel moderation (mute/ban/topic control) is a moderator-permission
matter (§7), exercised in-channel by whoever holds it, not from the admin
console.

`/msg` and `/private` are ephemeral, online-only, and never silently fall
back to persisted mail. `/msg user@node-fingerprint <text>`, `/private
user@node-fingerprint` (and `[M]essage` on a remote entry in Who's online)
reach a user on a linked node the same way, over a live session -- direct,
or through one or two live relays when neither node can dial the other
(design doc §8.10.3); when no live path exists the caller is told so
plainly and pointed at Link mail. A separate mutual invite/accept direct-chat
feature exists alongside these (reachable via the Who screen's
`[I]nvite to chat`, or `/dm <user>` from inside a channel) — also fully
ephemeral, no scrollback, and exclusive with channel chat (one active chat
surface per session).

Phase 2/3 scope is one active channel membership per session at a time;
simultaneous multi-channel membership and background delivery are later
roadmap work, not currently available to configure around.

---

## 9. Mail

Local mail is a persistent, asynchronous domain, distinct from ephemeral
chat `/msg`. It needs no SysOp setup — recipient mailboxes are bounded
automatically (the oldest already-*read* message may be evicted to make
room; unread mail is never silently discarded; if no safe eviction is
possible, delivery fails explicitly rather than quietly dropping
something). There's nothing to tune here today.

Link mail extends this same mailbox rather than creating a second,
parallel inbox UI — a message that arrived over Link looks like ordinary
mail once delivered.

---

## 10. Post drafts: save and resume

Not SysOp-specific, but worth knowing since it changes what "cancelled" 
means for every caller, including you when you're posting.

Composing a new board post (or editing one) no longer forces a choice
between finishing now or losing the work. In the line editor, `/exit` or
`/quit` save the current draft and leave — distinct from `/cancel`, which
still discards it outright. In the fullscreen editor, Ctrl+X's quit
dialog gained a **"[K]eep draft & exit"** option alongside Save/Discard/
Cancel.

The next time you enter a board where you have a saved draft, you're
proactively offered `[E]dit it, [D]elete it, or [I]gnore for now` before
the ordinary post list even renders. Editing an existing post that you
previously `/exit`ed out of works the same way, just triggered by
re-opening that specific post rather than by entering the board.

A saved draft that's genuinely abandoned (nobody ever comes back to resume
or delete it) doesn't accumulate forever: `[O]perations` → `[P]rune
drafts` (§11) removes any draft file older than 30 days, after a dry-run
preview.

---

## 11. Node operations: sessions, maintenance, drain, shutdown

`[O]perations` → `[N]ode and sessions` (only present in a live session —
see §1):

- `[W]ho` — lists connected sessions.
- `[M]aintenance mode` — blocks new non-SysOp logins. **Check this
  screen if a user reports they can't log in and you don't remember
  leaving anything on** — maintenance mode's status is shown
  unconditionally on this screen (not just when active), specifically
  because a SysOp toggling it and then getting distracted, with no
  visible reminder, has actually happened and locked out real users
  before this screen was changed to always show it.
- `[D]rain` — schedules disconnecting non-SysOp sessions after a delay,
  for planned maintenance. Anyone still connected sees a warning with the
  remaining time as soon as they reconnect or the drain is scheduled
  while they're active.
- `[L]ock & drain` — maintenance mode plus a drain together, the normal
  "I'm about to take this node down for a while" combination.
- `[S]hutdown` — schedules the process itself stopping. A shutdown
  triggered by an external signal (SIGTERM/SIGINT) rather than this
  screen is shown as such and may not be cancellable from here, depending
  on how it was triggered.

`[O]perations` → `[P]rune drafts` (always present, not tied to a live
session or Link) removes stale saved-post/bio draft files (§10) older
than 30 days. Same dry-run-then-confirm shape as `[G]C storage` (§5):
shows how many files and how much space would be freed first, asks
separately before actually deleting. A draft still within the 30-day
window is never touched, no matter how often you run this.

`[O]perations` → `[A]udit log` (also always present) is the node-wide,
read-only moderation/admin action trail — "did anything happen on this
node recently," not scoped to a specific account/board/channel the way
a per-account or per-object history view is. Toggle newest-first/
oldest-first, then pick an entry for its full detail.

---

## 12. Trust policy and Link federation admin

This section only applies if Link (federation with other NetBBS nodes) is
enabled on this node. It is genuinely advanced — see
`docs/NetBBS-design-doc.md`'s trust/reputation/quarantine section for the
full model (separate trust dimensions, probation, vouching, evidence
classes, signed trust signals, quarantine effects) before making policy
changes you don't have a clear mental model for yet. What follows is
where things live, not the full semantics.

`[S]ettings` → `[P]olicy trust` (or `[O]perations`/console `[L]` for a
lighter-weight status view):

- `[S]ubjects` — inspect/override effective trust state for a specific
  remote node or remote user, across each trust dimension (identity
  integrity, resource behavior, content conduct), plus their remote
  age/name attestation acceptance state if applicable.
- `[D]omains`, `[A]nchors`, `[R]eporters` — configure which trust
  signal sources this node actually listens to.
- `[I]dentity authorities` — which remote issuers this node accepts
  identity attestations from at all, and for which attributes.
- `[E]xceptions` — sole-authority exceptions; the console flags these
  with a "SAFETY DEVIATION" badge whenever any exist, since they're a
  deliberate deviation from the normal multi-source trust model and worth
  a SysOp noticing they're still active.
- `[H]istory` — trust-policy change history.

`[O]perations` also has, when Link is enabled: `[L]ink status` (peer
count, dial-reliability, relay activity, board/event counters — a
current-state snapshot, not historical trend data yet), `[O]utbox`
(outbound work items awaiting delivery/retry, with dead-letter/replay
controls), `[D]iagnostics` and `[F]ollow log` (see §15), and
`[R]epair carried posts` — a one-off, purely additive maintenance action
for a node that carried boards before local materialization existed; a
freshly-upgraded node reporting nothing to repair is expected, not an
error.

---

## 13. System settings

`[S]ettings` (or the console's own quick actions):

- `[W]elcome banner` — `[P]review` (renders exactly what a connecting
  caller would see right now), `[E]nable`/`[D]isable`, e`[X]`dit (opens
  the fullscreen ANSI-art editor, the same tool used for other ANSI
  content, with its own crash-recovery autosave).
- `[U]pdate` — update-check settings; the console shows when the last
  check ran and its outcome.
- `[T]imestamp format` — two independent, node-wide settings (not a
  per-account preference): display *format* (the shape of a rendered
  timestamp) and display *timezone* (which real instant it shows). Both
  need to be right — fixing only one still leaves everyone looking at a
  reshaped but wrong wall-clock time.
- `[P]olicy trust` — see §12.

---

## 14. Backup and restore

Backup and restore are **not** SysOp-console menu actions — they're a
standalone command so they can be cron-scheduled:

```
python -m netbbs.backup create --db path/to/netbbs.db --identity-dir path/to/identity --to path/to/new-backup-dir
python -m netbbs.backup restore --from path/to/backup-dir --db path/to/netbbs.db --identity-dir path/to/identity
```

(`--db`/`--identity-dir` default to this node's standard locations if
omitted; `--to`/`--from` are always required.) A restore preserves the
previous generation it replaced in a rollback directory rather than
deleting it — the tool tells you where and reminds you it isn't removed
automatically, so clean it up yourself once you're satisfied the restore
is good.

A backup captures five things as one atomic set, not just the database:
the database itself, content blobs, node identity (root/operational/
transport keys), the SSH host key, and the welcome banner. A DB-only
backup would silently lose the Link node identity and the SSH host key —
the latter means every client gets a MITM warning on the next connection
after a restore that skipped it.

The console's `[K]ackup status` screen (or the `[K]` quick action) is
read-only — it shows when the last backup ran and where it went, sourced
from what the backup tool itself recorded on its last run. It does not
trigger a backup.

Restore is staged and validated, not a blind in-place overwrite — it
checks the backup before touching any live path, and can recover from
being interrupted partway through. See
`docs/NetBBS-disaster-recovery-drill.md` for a worked, documented
end-to-end drill (corrupt/truncated backups, missing components, mid-
switch interruption) if you want to actually rehearse this before you
need it for real, which is strongly recommended before calling a node's
disaster recovery "production ready."

Explicitly out of scope for the backup tool itself: encrypting backup
contents at rest, off-site transport of a finished backup, and retention/
rotation of old backups. Those are your responsibility as the operator,
the same way they would be for any other cron-driven backup job.

---

## 15. Diagnostics and troubleshooting

`[O]perations` (Link-enabled nodes):

- `[D]iagnostics` — a bounded, warning-and-above-only log of Link-
  related events, stored in the database (`link_diagnostic_log`), *not*
  full content logging — it's sized/aged out automatically, not something
  you need to manually prune.
- `[F]ollow log` — tails that same log live while you watch, useful while
  actively reproducing or waiting for a problem.
- `[R]epair carried posts` — see §12.

If a user reports they can't log in: check `[N]ode` → maintenance mode
first (§11) — this is the single most common self-inflicted cause and is
easy to forget you left on.

If Link peers seem unhealthy: `[L]ink status` for the current snapshot,
`[D]iagnostics`/`[F]ollow log` for what's actually failing, `[O]utbox` for
anything stuck retrying or dead-lettered.

If something about a specific account's access doesn't make sense
(can't post somewhere, can't verify someone, unexpectedly locked out):
check that account's own detail screen (§2) for its level and
`can_verify_identity` state, then the resource's own gate settings (§4),
before assuming something is broken — most "this shouldn't be denied"
reports turn out to be an inherited Community default the SysOp forgot
was in effect, not a bug.

If something happened on the node and you're not sure who did it or
when: `[O]perations` → `[A]udit log` (§11) is node-wide and always
present, unlike the account/resource-scoped history views elsewhere —
the right first stop for "did anything happen here recently" rather than
"what happened to this specific thing."

---

## 16. Getting more help

- **Ctrl-H**, at any SysOp screen that hints `(Ctrl-H for help on these
  fields)` — inline explanation of that screen's own fields, right where
  you are. Not every screen has this authored yet; it's being added
  incrementally, starting with name requirements (§3).
- **Ctrl+G**, inside the fullscreen post/bio editor — its keybind list,
  plus what "Keep draft & exit" does and how a saved draft comes back.
- **Arrow keys**, on any field-editor screen (create/edit board, file
  area, channel, Community, and similar draft-based screens) — Up/Down
  move a `>` cursor over the field list, Space/Enter activates whichever
  field it's on (identical to pressing that field's own hotkey letter),
  and Left/Right step a cycling field's value in place without opening a
  sub-prompt, where that field supports it. Purely additive — every
  existing hotkey keeps working exactly as before, and the screen looks
  identical until you actually press an arrow key.
- `docs/NetBBS-design-doc.md` — the authoritative "why," and the exact
  rules for anything this handbook only summarizes.
- `docs/NetBBS-worklog.md` — developer-facing engineering invariants; only
  useful to you if you're also changing NetBBS's code, not running it.
