# NetBBS v7.15.2

A patch release for v7.15.1. It fixes the real cause of the startup database
lock reported in #1059, which v7.15.1 only partly addressed. **Nothing
migrates:** the node database stays at schema 116, and every protocol, door
API, save and world version is unchanged. Upgrading is a wheel swap and a
restart; rolling back to v7.15.1 is the reverse.

## The Link sync write lock, fixed at its root (#1059, #1064)

On v7.15.0 and v7.15.1, a node could hold its database's write lock for
several seconds after a Link sync pass, starting with the first one at
startup, with nothing running. Any write through the
main connection in that window, such as the scheduled release check, the
managed-DNS updater or a caller logging in, waited out the 5-second busy
timeout and failed with `database is locked`. v7.15.1 made the background
tasks retry and stopped the trust recompute from holding the lock, but the
lock itself remained: on the reporting node it lasted about 11 seconds.

**The cause:** every Link sync pass prunes old sealed-attestation snapshots
held for nodes this node relays for (new in v7.15.0, #632). The prune
committed only when it deleted something, but Python's `sqlite3` opens a
transaction before any `DELETE`, and the `DELETE` takes the write lock even
when it matches nothing. On a pass with
nothing to prune, the transaction stayed open until the next unrelated commit
on the same connection. On an outgoing-only node that commit came after its
first relay-candidate dial timed out, which is why the lock lasted as long as
that dial.

- The prune now always commits.
- **The whole class is guarded.** The database lane now checks every call. A
  call that returns with a transaction still open is committed and logged as
  a WARNING naming the function, so a missed commit can no longer hold the
  lock, and the log says where it happened. A call that fails has its open
  transaction rolled back, so a half-done write is never committed later.
- In the test suite, the same check fails the test instead. Running the
  whole suite with it found this prune and no other leaking call.

If your node logged `database is locked` from the update check, the
managed-DNS updater or a login, in the first seconds after starting or
later, this
release fixes it.

## Verification boundaries

- **Gate:** **13,736 passed, 138 skipped** in the full suite (`pytest -n 10`,
  with the strict transaction check on) and 5 of 5 `timing_sensitive` tests,
  on Windows, on the exact release tree.
- The fix is reproduced and tested with an outgoing-only node whose first
  relay dial hangs, on Windows. It has not yet been confirmed on the node that
  reported #1059; that check follows its upgrade.
- Nothing here ran on NetBSD or Linux for this release.
