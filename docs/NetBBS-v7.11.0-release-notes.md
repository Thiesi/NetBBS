# NetBBS v7.11.0

A node can now replace its Link keys. Until this release the design
document promised that rotating an operational key was "a guided SysOp
action", but nothing performed one. A node whose signing or transport
key leaked had no response short of becoming a new node. This release
builds that action, in two kinds, and fixes the thing that would have
made the first real rotation break the rotating node's own boards. The
other change gives SysOps a way to recover expired uploads without a
shell.

**Nothing migrates.** The node database stays at schema 72, War Dialer
worlds at world schema 10, and Voidrunner careers at save schema 2 with
tactical ruleset 3. `NETBBS_PROTOCOL_VERSION` is 1,
`REALTIME_PROTOCOL_VERSION` is 4, `DOOR_API_VERSION` is 3, and no setting
in `netbbs.toml` changed.

## For SysOps

### Replace a node's keys: Link status → Keys (#624)

A node's **technical identity** is its root key, and it never changes. Day
to day the node signs with an operational *signing key* and connects live
with a *transport key*. Either one can now be replaced without changing
the node's address or losing its reputation.

**Link status → `[K]eys`** opens *Node keys*, which shows the node's fingerprint and the history of
both keys: current, retired or compromised, with dates. `[S]igning key`
and `[T]ransport key` each open a screen with two actions. Each takes one
confirmation, and the outcome is shown on the Keys screen:

- **`[R]otate`** retires the old key. Everything it signed stays valid, so
  peers notice nothing except that new content carries the new key.
- **`[C]ompromised`** is for a key someone else may hold. Peers refuse
  anything that key signed which they have not already accepted. Copies
  they already hold stay where they are. For the signing key, your node
  signs its own boards, channels, posts, messages, file areas, files and
  outgoing mail again under the new key. The objects keep their identities, so peers see the same content
  with a valid signature.

Replacing the transport key ends every live chat session at once, and
peers reconnect with the new key. A caller watching a channel linked from
another node is told the live link dropped and gets it back by
re-entering the channel. A handshake that was already under way
when you rotated cannot slip through on the old key.

With the node stopped, the same action is

```
python -m netbbs.admin rotate-key signing|transport [--compromised] \
    --db /path/to/netbbs.db --identity-dir /path/to/netbbs_identity
```

It refuses while the node is running. It checks this *before* it opens
the database, so a newer tool cannot migrate a live older node's
database. It also refuses a database and an identity directory that
belong to two different nodes. Both surfaces write a `rotate_node_key`
entry to the audit log.

A rotation is saved before anything in the running node changes. A crash
part-way through leaves a directory the next start completes rather than
refuses. Signing keys a rotation retires are kept under `retired/` in the identity
directory for one purpose: opening Link mail a peer sealed to the old key
before it learned the new one. Back up after rotating. A backup taken
before the rotation restores the old key.

If `root.identity` itself leaked, as it does with a copied identity
directory or backup, rotation is no remedy: whoever holds the root can
authorize keys of their own. The Handbook's new *Node keys* section and
Troubleshooting row say so.

### Recover an expired upload from the console (#639)

In an area with a maximum file age, an expired file has been unreachable
from any terminal since 7.10.0 until it is purged. The file area's admin
detail screen now has **`E[x]pired files`**. It lists every file that has
expired but not yet been purged, oldest first. Each file's own screen
shows its purge date, and `[D]ownload` there recovers it over Zmodem or a
browser link. The screen recovers files but does not relist them; a
re-upload does that. A browser link minted while a file was listed stops
working once the file expires, except for someone holding approve
permission on the area, which is who recovers it.

## Link

### Historical signatures verify again (#624)

Peers checked every event against the sender's **current** signing key
only. That made the design document's "historical signatures remain
verifiable" untrue. The first signing-key rotation on a real node would
have left its boards, posts, files and mail unverifiable to every peer
that had not yet received them, carriers' copies included, and a board it
originated could no longer be joined. A long-lived event now verifies
against the current key and then against every key its chain retired
routinely. Hellos, requests, withdrawals, trust objects and attestations
still check the current key alone: they are signed fresh or re-issued on
rotation, so an old key there means a replay or a stale copy.

A compromise is stated in the root-signed revoke itself, as
`"compromised": true`. A routine revoke is byte-identical to every revoke
built before, and older nodes ignore the new field.

A node pulling a carrier's copy that only a compromised key signed skips
that one object. It no longer abandons the rest of the response.

A peer's key history only grows. A later hello from a node already on file
must extend the chain this node holds for it, and its descriptor must verify
under that chain's current key. Before this, a replayed older part of the
chain, one that ended before a compromise, replaced the longer one on file.
That would have let whoever held the leaked key make it current again.

### Attestation revocations survive a rotation (#623)

A withdrawal of a verified age or name, signed just before a signing-key
rotation and not yet pulled, would have been skipped by any subscriber
that had learned the new key. The attestation it withdraws would then have
stayed accepted there for up to 90 days. The next sync pass now signs such
a revocation again under the new key. A receiver treats the repeat as a
repeat: it stores it but writes no second audit entry.

## Upgrade and rollback

Replace the wheel and restart. No migration runs.

**Upgrade the nodes you exchange content with before anyone rotates a
signing key.** An older node still checks only the current key, so after a
routine rotation it refuses that node's earlier content it has not yet
received. It is the bug this release fixes, still present on the older
node.

**Rolling back is a wheel swap**, since the schema did not change, with
one caveat. **MANUAL — a node that has rotated a signing key:** an older
wheel starts and loads the rotated identity, but it cannot open mail
sealed to a retired key, and it checks peers' content against their
current key only. Roll back only before rotating, or accept both. If a
rotation was interrupted, start the 7.11.0 node once before rolling back:
an older wheel does not finish a rotation left half-saved and refuses to
start.

## Verification boundaries

- **Rotation has not run between real nodes yet.** It is covered
  in-process and over loopback. Covered: routine and compromise rotations,
  a peer's view of each, a carrier's stale copy, a crash at each point of
  the save, the console flow, the offline command and a live rotation. The
  roadmap's step 5d runs rotation on the three test nodes, with rows in
  `docs/NetBBS-link-dogfood-plan.md`.
- **Copies another node already holds are not refreshed after a
  compromise.** A node that later fetches such a copy from that carrier
  skips it, and gets the object only from its origin. Issue #672 tracks
  carrying the re-signed copies onward.
- **A post saved within milliseconds of a compromise rotation** can keep
  the old signature until the node next starts. The startup pass signs it
  again.
- **There is no screen action yet to declare an already-retired key
  compromised.** The protocol accepts such a revoke; nothing issues one.
