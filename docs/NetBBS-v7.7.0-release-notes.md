# NetBBS v7.7.0

Remote identity attestation finally leaves the node it was verified on,
a backup carries the receipts a door reads, and `[H]istory` starts
showing you your own calls instead of everybody's.

The theme, if there is one, is work that existed but never reached
anybody. A caller could mark their verified age or name Link-visible and
nothing was ever signed or sent. An archive held the posts a door made
and none of their outcomes. `[H]istory` described itself as "your recent
sessions" and listed the whole node. A SysOp could accept the managed
netbbs.org subdomain offer and hit a wall telling them to ask their
operator, who was themselves. Four things the interface promised and the
code did not deliver.

**This release migrates.** The node database goes from schema 66 to 67
for two new Link tables. Rolling back therefore needs a restore, not just
the previous wheel — see *Upgrade and rollback*. Voidrunner careers stay
at save schema 2 and War Dialer worlds at world schema 10. Neither Link
protocol version moves (`NETBBS_PROTOCOL_VERSION` 1,
`REALTIME_PROTOCOL_VERSION` 4), though one endpoint is added — see
*Talking to a 7.6.0 peer*.

## Remote identity attestation reaches the Link (#590)

This is most of the release. Issue #584 catalogued eighteen public
functions in `src/netbbs/link/` that nothing else in `src/` referenced —
subsystems that were built, tested and never called. Six of them were
remote identity attestation, and the caller-visible symptom was concrete:
the Profile screen let you toggle `link_visible` on a verified age or
name, and no attestation ever left the node.

All six now have production callers.

**Your node signs.** Once per sync pass it reconciles the objects it has
signed against current local consent: it mints one for a newly
Link-visible attestation, renews one approaching expiry, and signs a
revocation when the consent behind a live object is gone — the toggle
switched off, the attestation removed, the value re-verified, or the
account deleted. Issuance happens on the sync pass rather than in the
Profile screen because signing needs the node's current operational key,
and a caller's session has no business holding one.

**Your node serves.** A new `/link/v1/attestation-pull/{fingerprint}`
endpoint, with its own separately signed request object type rather than
the trust pull's request on a second URL. The object type is inside the
signature and each request type keeps its own bounded nonce cache;
sharing one type across two endpoints would let a holder of a signed
trust pull spend its one-shot nonce against the attestation endpoint and
make the trust pull fail as a replay. A node serves only what it signed
itself — a pull naming a third-party issuer is refused rather than
answered, because unlike a trust signal there is no carrier's copy worth
asking for.

**Your node pulls.** Each configured attestation authority, every pass,
cursored and restart-safe. This is a separate subscription set from the
trust reporters: design doc §5.5 and §12.3 both say reporter
configuration grants no attestation authority. Objects are ingested one
at a time, so an object this node cannot use — a revocation for an
attestation it never received — does not discard the rest of the page
with it.

**And a ratchet, so this does not happen again.**
`tests/test_link_production_callers.py` fails on any public function in
`src/netbbs/link/` that nothing in `src/` references outside its own
definition. It reproduces exactly the eighteen names #584 found by hand;
twelve remain, each allowlisted against the issue that owns it, and a
companion test forces an entry back off the list as soon as it is wired
up. The list can only shrink.

Two details decide whether that check works at all. Reference collection
is AST-based rather than a search for `name(`, because this codebase
dispatches callables by reference (`await lane.run(link_board, db, ...)`)
and a call-site search reports most of `link/` as uncalled. And a bare
import is deliberately *not* a reference, or `get_remote_file` — imported
in `transport.py` and used nowhere — would look wired up.

The Profile help text was also wrong in a way #584 never mentioned: it
said the value reaches "a remote node's trust/vouch policy", which §5.5
explicitly denies. It now says what actually happens, including that
sharing reaches only nodes whose own SysOp has accepted this node's
verifications, and that switching it off withdraws it from them.

**#584 is not closed.** Two of its three pieces landed here; trust-object
issuance is filed as #589 and needs a decision before it needs code, and
four names remain untriaged.

## Your history is yours (#592)

`[H]istory` sits in the main menu's YOU section under the description
"Your recent sessions", and listed the last twenty calls made to the node
by anybody — every other caller's connect and disconnect times, under a
heading claiming they were yours. If you wanted to know when you last
called, or whether your previous session ended cleanly, your own calls
were scattered through everyone else's, and on a busy node might not
appear at all.

The node-wide listing was not the mistake; having one screen for two
questions was. And the nicer of the two already existed — the framed
previous-callers panel shown once after login, reachable nowhere else. So
the two screens swap places:

- **`[H]istory`** is your own call record, and spends the width freed by
  dropping the name column — the same name on all twenty rows — on how
  long each call lasted.
- **`P[r]evious callers`** is the node-wide roll, on its own main-menu
  hotkey, drawn by the same renderer the post-login splash uses so the
  two can never disagree about who is listed or under what name.

```
NetBBS › Your sessions
──────────────────────
  connected 15.09.2026 06:14, still connected
  connected 15.09.2026 06:14, connection lost -- session did not end cleanly
  connected 15.09.2026 10:00, until 15.09.2026 11:02 (1h 02m 30s)
```

The menu screen differs from the splash only where being asked for rather
than offered demands it. It ignores the node setting, which reads "shown
after login" / "hidden after login" and governs exactly that — the
node-wide listing was unconditionally reachable before this change too,
as `[H]istory`. It keeps a row on a terminal too short for the splash's
budget. And where the splash may silently skip itself, every path here
draws something.

**Retention changed with it**, because a node-wide row cap cannot keep a
per-caller promise. The table was pruned to the node's newest 500 rows in
arrival order regardless of whose they were: 500 other logins between
your visits and every row you had was gone. A row now survives while it
is among its own account's newest 20 — one screenful, so no account is
worth keeping more of — and the survivors fill the same fixed 500-row
budget newest-first. One caller's flood can no longer evict another's
history.

`session_history_name_visible` only ever meant "in the listing other
callers read", so the profile field, its help text, and the
delete-account warning in the SysOp console all name the previous-callers
roll now instead of a screen that no longer shows anyone else's name.

## A backup carries the receipts a door reads (#556)

A door's outbound result receipts live beside the node database because
that is what would let a backup carry them. Nothing carried them:
`create_backup` never looked at `door-outbound/`, and restore had no
component for it. An archive held the posts a door made and none of their
outcomes, so a node recovered on another host handed its doors an unknown
outcome for work they had already had answered.

Receipts are the fifteenth recoverable artifact now, captured as a
checksummed `door-outbound` component and restored with the node
generation they describe.

**Capture runs before the database snapshot, and that direction is the
point.** A `"posted"` receipt is written only once its post is committed,
so every receipt in an archive names a post that archive's snapshot
contains. A post the node itself deleted is the exception and is
deliberately preserved — deleting a board removes its posts and leaves
the receipts, so the running node already holds that pair, and an archive
that quietly dropped them would restore a node tidier than the one it was
taken from.

Restore replaces the receipts on disk with the archive's own, including
replacing them with nothing when the archive predates this component:
receipts from a later generation standing beside an older database claim
post IDs it never issued. Older archives stay restorable.

Review of this one turned up three faults in the *shared* restore
machinery that the new artifact made reachable, and they are worth naming
because they were never specific to receipts: no two artifacts could be
validated against restoring onto the same live path or into each other;
`exists()` follows symlinks, so restoring absence over a dangling link
was a no-op and the automatic rollback then failed to put the link back
while reporting that it had; and a failed post-manifest self-check left
the destination behind, which `create_backup` then refuses to retry into.

The handbooks stop warning that receipts are omitted and state the
contract instead: a missing receipt is not proof either way, and not
grounds on its own for publishing again.

## The managed-DNS service address has a route in (#583)

On a freshly bootstrapped 7.6.0 node, accepting the managed netbbs.org
subdomain offer — the pre-set first-run answer — stopped immediately at
*"ask your operator to set the service address."* Retrying from the SysOp
console hit the same wall.

`set_service_url` had no caller anywhere in the installed package: no CLI
flag, no `netbbs.toml` key, no admin screen. The address had no route
into the database that the updater, the registration prompt and the
console's DNS screen all read it from, so every node accepting the offer
dead-ended identically.

Both halves exist now. `DEFAULT_SERVICE_URL` carries the project's own
instance, the same shape and reason as the reliable-nodes URL — a
project-run service a node should not need to be told about. And
`[managed_dns] service_url`, or `--managed-dns-service-url`, points a node
at a different instance; it is mirrored into the node database on every
startup *including when absent*, so removing the setting returns that
node to the shipped address rather than stranding it on an override it
was told about once.

**The shipped address is still `None`**, because `services.managed_dns`
is not deployed — see *Verification boundaries*. What changes today is
that a node says the service is not running yet, in two different
messages: the first-run screen tells someone their answer is recorded and
costs them nothing more, while the SysOp console tells someone who
deliberately pressed `[R]egister` why nothing happened.

Making the address configurable makes it changeable, and a managed-DNS
credential is a bearer secret for one service's registration — whoever
holds it can release or repoint that node's record. The issuing address
is now written in the same transaction as the registration it belongs to,
and it is the gate every path that would present the secret asks first:
the updater pauses its heartbeat, release and rename and cancellation
refuse while naming the two addresses that disagree, and registering with
a new service starts over as a fresh registration rather than presenting
the old secret.

## Two smaller fixes

**An identity guard defeated by clock resolution (#581).**
`touch_last_login` asked whether the row at an id was still the caller's
account by matching `id` and `created_at`. `users.id` is a reusable
rowid, so `created_at` was carrying the whole check — and it is only as
fine as the platform clock. `utc_now_iso()` stamps two accounts created
in one Windows tick with the same string, which this project's own suite
has produced. `username` is matched too now: UNIQUE, uniquely indexed
`COLLATE NOCASE`, and never rewritten.

**A page counter that could hide a page (#558).** The picker's
denominator was the whole list divided by the *current* page size while
the numerator counted pages actually walked, so a mid-browse resize
separated them — 31 items, resize from 80x22 to 80x24, press `[N]`, and
the label read `page 2/2` while item 31 was still there. Both now come
from `page_end`, the real end of the page drawn. Cosmetic only: paging is
driven by an absolute row offset, so nothing was ever lost or skipped.
This was a known boundary in the 7.6.0 notes; it is closed.

## Upgrade and rollback

Replace the wheel and restart. The node database migrates **66 → 67** on
first start, adding `link_issued_remote_attestations` and
`link_attestation_pull_cursors`.

**Rolling back to 7.6.0 requires a restore, and a restore costs more than
the migration did.** `_apply_migrations` refuses a database whose schema
is newer than the build understands, so a 7.6.0 wheel will not open a
database that 7.7.0 has opened. There is no down-migration. Going back
means restoring the backup you took before upgrading — **which rewinds
the whole database to that moment**. Every post, message, account,
permission change and configuration change made while 7.7.0 was running
is discarded, not just the two tables the migration added.

So: take the backup immediately before upgrading, and decide early. If
7.7.0 is going to be rolled back, it is far cheaper in the first hour
than on the third day.

One thing worth knowing about the new tables:
`link_issued_remote_attestations.user_id` is `ON DELETE SET NULL`, not
`CASCADE`. A signed object has to outlive the account it is about long
enough to be revoked, or every subscriber holds a live assertion about a
deleted user until it expires. The reconcile treats an active row with a
null `user_id` as "revoke".

### Talking to a 7.6.0 peer

The attestation pull is a new endpoint on an unchanged protocol version.
A 7.7.0 node pulling from a peer that has not upgraded gets an HTTP 404,
which is contained per authority: it is logged and that authority is
skipped, and the rest of the sync pass continues. Nothing else in Link
changes shape. Attestations only ever *loosen* a local gate, so a peer
that cannot serve them costs visibility, not correctness.

No new `node_config` key needs setting by hand; `managed_dns_credential_
service_url` is written by the registration path itself.

## Verification boundaries

What this release does **not** establish:

- **`services.managed_dns` is not deployed, so `DEFAULT_SERVICE_URL` is
  still `None`.** Neither `dns.netbbs.org` nor `managed.netbbs.org`
  resolves today. The only route to a working managed-DNS registration
  right now is pointing a node at your own instance with
  `[managed_dns] service_url`. A guard test has to be flipped in the same
  commit that flips the constant, and `services/managed_dns/README.md`
  carries the step.
- **Remote attestation has not been exercised between two live nodes.**
  Its validation is the new `tests/test_link_attestation_issuance.py`,
  which deliberately never calls a builder directly — every test starts
  from a caller's own act and asserts on what a subscriber ends up
  holding — plus loop-level coverage driving real `run_link_sync` passes.
  That is the right shape of test, and it is still not two hosts.
- **#584 is not finished.** Twelve names remain on the
  production-caller allowlist, trust-object issuance is unscoped (#589),
  and four names are untriaged.
- **Guest login still has not been run on a live node** — unchanged from
  7.5.0 and 7.6.0, and still the part of recent releases most worth
  exercising deliberately.
- **The DoorParty and BBSLink templates remain unverified** against live
  provider accounts (#566, #565).
- **The POSIX-only door tests have still never executed** (#509).
- **Door receipt capture does not filter against the finished snapshot**,
  and does not reconcile captured directories against a hook switched off
  mid-backup. Both were declined by decision: they ask capture to be a
  validation pass over a door's history, and both would restore a node
  tidier than the one backed up.

Gate for this release: full suite **9,550 passed / 56 skipped** in
28:30, `PYTEST_EXIT=0`, on this tree. Clean on the first pass, which
has not been true of a release for a while -- the three pre-existing
failures 7.6.0's own gate had to clean up were fixed in it.
