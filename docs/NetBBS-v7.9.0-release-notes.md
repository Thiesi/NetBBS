# NetBBS v7.9.0

Three things a federated node could not do until now: keep what it
signs away from nodes it never chose to tell, hold on to a deleted
caller's name, and say anything at all about another identity. This is
the roadmap tracker's steps 3 and 4 (#612), the foundation half of the
alternation, and all of it exists so that the sustained multi-node
dogfood run that follows has something real to exercise.

**This release migrates the node database, 67 to 70.** A node that has
run 7.9.0 cannot be opened by an older wheel — rolling back means
restoring a backup, not swapping the wheel. Nothing else moves: Voidrunner
careers stay at save schema 2, War Dialer worlds at world schema 10,
`NETBBS_PROTOCOL_VERSION` is 1 and `REALTIME_PROTOCOL_VERSION` is 4, and
no setting in `netbbs.toml` changed.

**Read this before upgrading a node that shares attestations.** A node
that signed age or name attestations for its callers served them to
every peer its trust policy admitted. It now serves them to nobody until
the SysOp names recipients. That is the intended default and it is not a
fault; see *Attestations reach only the nodes you name* below and the
**MANUAL** step in *Upgrade and rollback*.

## Attestations reach only the nodes you name (#596)

A remote attestation is this node asserting, under its own key, that one
of its callers is over 18 or that a display name is theirs. Since the
feature shipped, the pull that serves those assertions admitted any
completed peer the trust policy allowed, and the query behind it took no
requester at all. Every peer the policy admitted could therefore read
every birthdate and real name this node had ever signed — including
values whose subject had since opted out, because opting out stopped
*future* disclosure and retracted nothing.

**There is now a recipient list.** It is a SysOp-configured list of
nodes, per node and not per attribute, empty on a fresh install and
seeded from nothing on an upgrade. `load_issued_attestation_page`
requires the requesting fingerprint and refuses one that is not on the
list *before* it resolves the cursor, so a stranger cannot use a
well-formed cursor to probe for content IDs. Every change to the list is
audited.

**Per node, not per attribute**, because the caller already scopes per
attribute with two toggles of their own in Profile, and a per-attribute
grant would make a requester's stream depend on its own grant history —
which a subscriber-owned position cursor cannot express.

**A refusal is visible, not a thinned stream.** HTTP 403 with
`reason_code` `not_an_attestation_recipient`. Serving revocations only,
which looks kinder, is the trap: any page advances the requester's
cursor, so a stream thinned today steps the subscriber past attestations
a grant tomorrow should have delivered. The subscriber recognises the
refusal, leaves its cursor exactly where it was, and logs one warning
that reaches Diagnostics, so the other SysOp learns what to ask for
instead of watching a sync silently return nothing.

**A retired value is blanked where it is stored.** A revoked or expired
attestation keeps its row — a cursor naming it still resolves — and its
value, envelope and signature are emptied, on the issuer and again on
every receiver that ingests the revocation. Each sync pass sweeps
expiries. The migration blanks rows already revoked when you upgrade.
The page itself now serves every revocation plus the attestations that
are live *at read time*, where before it served the store whole,
expired and revoked objects included. This is removal from the live
database, not forensic erasure: the WAL, freed pages and any backup you
already took can still hold the bytes.

**What the two surfaces say.** Settings → Policy trust → Published
identity carries the recipient count, warns when it is zero, and opens
`[R]ecipients` — the listing, add and update as a draft editor, and
removal behind one confirm that states plainly what removal does *not*
do. The caller's sharing toggle in Profile now reads `on (reaches N
nodes)` rather than an unbounded promise, and says outright when the
count is zero that their SysOp shares with no node yet; the consent text
describes both what is enforced and the one thing that cannot be: a node
that already copied a value. The Identity authorities screen, on the
subscribing side, now warns that nothing arrives until the other node's
SysOp has named this one.

**The accepted cost, written into the design document.** A node removed
from the list receives no further revocations for what it already holds;
that lapses on its own within 90 days.

## A deleted account's name stays deleted (#594)

On the Link an account *is* its username: `local_user_id` on the wire is
`users.username`, and that column was unique only among live rows.
Deleting an account handed the next registrant of that name the previous
holder's Link mail (delivery resolves a recipient by username), the
authorship of their carried posts (labelled
`username@home-node-fingerprint`, retained forever), whatever trust state
peers had recorded against them, and any live age or name attestation
until its revocation propagated. Account deletion is ordinary operator
behaviour, so this had to be closed before a multi-node run, not after.

**The name is retired, not rewritten.** `delete_user` records it in
`retired_usernames` inside its own write transaction, and account
creation checks that table inside *its* transaction, so a deletion and a
registration of the same name cannot pass each other. The match is
case-insensitive, like the uniqueness index it backs up. Changing to a
stable per-account identifier would not have helped: a Link mail address
is `name@node` by design.

**On a node that has ever run Link.** A sticky marker is set when the
node starts with Link effectively on, and the migration seeds it once
from any artifact Link leaves behind, so a node that federated before
this release is covered. A node that has never run Link records nothing
and keeps reusing names as before.

**One exemption, and it took three tries to get right.** A registration
still awaiting approval that was never sent Link mail is not retired —
otherwise strangers could consume names on an approval-required node just
by registering. The test is the pending state, which is the only column
that proves no session ever happened. It is deliberately *not* "never
logged in": open registration drops a new caller straight into a first
session without stamping a login, so a NULL there never means unused.

**No oracle.** `UsernameRetiredError` reads to a remote caller exactly
like a name already in use, because both self-service registration paths
print the exception. The SysOp's create-user screen is the surface that
says why and where to undo it, and keeps the draft.

**The way out.** Users → Re[t]ired names lists what is held and releases
any of it behind one confirm, audited. The delete confirmation now says
up front that the name will be held. The accepted cost is
over-reservation on a node that once ran Link and no longer does; the
release screen covers it.

## A SysOp can vouch for an identity (#589 slice 1)

`trust_wire` has verified, stored, re-served and enforced signed trust
objects since Phase 4 — for objects *other* nodes issue. Nothing in the
source called a builder, so no node could issue one. The carrier store
was empty on every node, the trust pull served nothing, and no dogfood
run could have shown trust propagating at all, because nothing existed to
propagate.

**What a SysOp does.** Settings → Policy trust → `[S]ubjects`, open an
identity, choose `[V]ouch`. The screen says what a vouch is and where
this one stands before it asks for anything; issuing takes a reason and
one confirm, and the prompt says the reason is published inside the
signed object. Policy trust → `[V]ouches` lists everything this node
vouches for and withdraws any of it. The trust history shows both.

**An intent, and one reconcile that signs.** The screen records an
intent; `reconcile_issued_vouches` decides what should exist and signs
it — the shape issued attestations already have. The sync pass runs it
every pass and the SysOp console runs the same function immediately when
it has a node identity; the offline admin console has none, so there an
intent waits for Link. A vouch lives 90 days, renews at 30 remaining, and
is reissued when the operational key rotates. It is revoked when the
intent is withdrawn, when its reason is replaced, or when the identity
becomes quarantined or blocked here — and the intent survives that, so
lifting the restriction restores the vouch.

**Own objects sit in the carrier store** under this node's own
fingerprint, which is exactly what the existing pull already serves: no
new endpoint, no new wire type. An own vouch never counts in this node's
own trust arithmetic, because local counting admits only configured
reporters. Not vouchable: an identity this node has never met, one it
has quarantined or blocked itself, the node itself, and the node's own
users.

### The receiving side nothing could reach until something was issued

One real issuer reaches all of these on the first pass. Each of the first
five wedged or misled a subscriber for good; the last two are faults the
same work exposed next door.

- **Per-object skips.** An object outside a reporter's grant, a
  revocation for an object the subscriber does not hold or has already
  seen revoked, and an object signed by a key the issuer has since
  replaced each used to reject the entire batch. The cursor never moved,
  so every later pass met the same object first, forever. Each is now
  skipped while the rest of the page is admitted. Refusals that time or
  state can undo — a full quota, an issue time in the future — still
  reject the batch, because retrying is the right answer there.
- **An unknown key stops the page instead of skipping it.** An object
  that verifies under no key the subscriber knows is usually the
  subscriber's own copy of the issuer's key being stale: the issuer
  rotated and re-signed everything. Skipping would lose all of it. The
  issuer's transition chain tells a *replaced* key from an unknown one,
  and only the replaced one is skipped. The cursor may only ever move
  past an object the subscriber could authenticate.
- **An unresolvable cursor heals (#621).** Both subscription pulls answer
  one with `reason_code` `unknown_pull_cursor`, and the subscriber
  forgets the cursor and re-reads from the start rather than stalling.
- **A widened grant re-reads the stream.** A skipped object is not
  stored, so changing a reporter's grant resets that reporter's cursor.
- **The issuer serves in storage order.** Ordered by receipt time, a
  revocation signed while the issuer's clock was behind sorted ahead of
  the vouch it retires, and a fresh subscriber skipped the revocation and
  then admitted the vouch. The page is now ordered by row, which is the
  order things actually arrived in.
- **A signature is checked before anything else about an object.**
  Protocol version, object type and payload shape are judged only after
  the envelope verifies, which is what makes "authentic but not for me"
  distinguishable from "refuse the page". It also fixes the wire's
  forward compatibility: a 7.9.0 subscriber skips an authentic object of
  a type it does not know instead of wedging its cursor on it, so a
  future object type does not strand every node on this release.
- **One unusable key no longer costs the whole sync task.** A reporter or
  authority whose signing key cannot be resolved — a key chain ending in
  a bare revoke, say — used to raise out of the pull loop and end the
  background sync task for every other peer with it. It now costs that
  one pull and a warning.

**One thing a SysOp has to know**, now in the handbook and the dogfood
plan: a running node enforces trust policy, and under it a reporter that
is not established is neither pulled nor counted. Naming a reporter is
not enough — the subscriber's SysOp establishes it by override, or it
graduates.

## Upgrade and rollback

Replace the wheel and restart. **The node database migrates 67 → 70**:
migration 68 adds the attestation recipient list, marks both attestation
tables with a redaction column and blanks already-revoked rows; 69 adds
the retired-username table and seeds, once, the marker that says this
node has ever run Link; 70 adds the vouch intent table. Nothing existing
is rebuilt and no data is discarded beyond the revoked attestation values
described above.

**Rolling back needs a restore.** `_apply_migrations` refuses a database
whose `user_version` is newer than the build understands, so a 7.8.x
wheel will not open a database 7.9.0 has run on. Back up before you
upgrade, and roll back by restoring that backup — not by reinstalling the
old wheel over a migrated database. No `netbbs.toml` setting changed in
this release, so the config file itself needs nothing either way.

**MANUAL — for a SysOp whose node signs attestations:** after the
upgrade, go to Settings → Policy trust → Published identity →
`[R]ecipients` and name the nodes that should receive them. Until you do,
this node serves no attestations to anyone and its peers log a refusal.
The screen warns while the list is empty. If your node signs no
attestations, there is nothing to do.

**MANUAL — before deleting an account on a Link node:** the name is now
held afterwards. That is the point, but it means "delete and recreate"
as a way to fix an account no longer returns the name. Users →
Re[t]ired names releases one deliberately.

**MANUAL — for anyone about to try trust propagation:** naming a reporter
does not establish it. On a live node, under the enforced policy, a
probationary reporter is neither pulled nor counted; establish it by
override on the subscribing node, or wait for it to graduate.

## Verification boundaries

What this release does **not** establish:

- **None of it has run between two live nodes.** Every path here —
  the recipient refusal, the redaction sweep, vouch issuance, and all
  five receive-side wedges — is exercised in the suite, several of them
  through real sync passes over a real HTTP server on loopback, and on no
  node on the internet. The three-node dogfood run (#83) is step 5 of the
  tracker and is what will test this against real clocks, real key
  rotations and two operators.
- **#589 stays open.** Only vouches are issued. Trust *signals* — what a
  node says when it accuses another, which observations become a signed
  signal, and whether any of that is automatic — remain undesigned, and
  six names under #589 stay on the production-caller allowlist in
  `tests/test_link_production_callers.py` — the signal builder among
  them, still called by nothing in `src/`.
- **A key rotation still orphans attestation revocations (#623).** The
  vouch side re-signs them; the attestation side does not, so a
  subscriber that missed a revocation issued just before a rotation will
  not see it afterwards. It is open, it is known, and step 5's recovery
  exercise rotates a key on purpose.
- **Redaction is not forensic erasure**, as above: the live rows are
  blanked, backups and freed pages are not.
- **The POSIX-only door tests have executed once**, on ReLink's host for
  #509. They still have no CI.
- **The DoorParty and BBSLink templates remain unverified** against live
  provider accounts (#566, #565).
- **Guest login still has not been run on a live node** — unchanged from
  7.5.0 onwards.

Gate for this release: full suite **9,785 passed / 56 skipped**, `PYTEST_EXIT=0`,
on the release tree with the version bumped.
