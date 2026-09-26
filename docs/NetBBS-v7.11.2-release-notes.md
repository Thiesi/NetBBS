# NetBBS v7.11.2

A patch release with one fix, found while running the Phase 4 trust exercise
on the three test nodes. It is cut from v7.11.1 and carries nothing else.
Everything else merged since v7.11.0 still waits for v7.12.0.

**Nothing migrates.** The node database stays at schema 72, and no protocol,
door API or `netbbs.toml` setting changed.

## An outgoing-only node could lose its working relay for good (#712)

A node nobody can dial, the ordinary case behind a home connection, asks up
to three full peers to relay for it. It was meant to ask the most reliable
ones, but nothing ever recorded how a relay request went, so every candidate
looked equally good, and which three were asked came down to chance. On the
test network, a large peer list introduced candidates at unreachable
addresses. One node asked the same three every few minutes for hours and never
again asked its own seed, which was up the whole time. Without a relay, what it
vouches for, signs or is sent through one reaches nobody.

- **Each relay attempt records one outcome per candidate: whether that node
  was reached.** A node that answers but declines still counts as reachable,
  and so does a relay whose first address is dead and second works. An address
  answered by a different node counts for nothing.
- **A node that syncs with a seed records it as reached,** so its working seed
  ranks above strangers it has only heard of.
- **Only a relay that agrees fills a slot.** A candidate that declines or cannot
  be reached moves to the back of the queue for an hour, so every pass gets to
  candidates that have not been tried. Each pass makes at most six requests.
- **When candidates tie, one this node has met comes first.**

## Upgrade and rollback

Replace the wheel and restart. Rolling back is a wheel swap to 7.11.1 or
7.11.0, which have the same schema; the key-rotation caveat from the 7.11.0
notes still applies. A node that has been stuck recovers on its own within a
few sync passes of the upgrade, once its unreachable candidates have failed
once each.

## Verification boundaries

- **The fix is tested in-process.** Candidates that are unreachable, answered
  by another node, declining, or multi-address are each checked to count as
  one observation. Decliners and unreachable ones are checked to move to the
  back until a willing relay is reached. The full suite passed on it. It has
  not yet run on the stuck node; deploying this release there is that check.
- **The fake peers that fed this come from the test suite, now fixed on `main`
  (#714), not in this release.** Developer test runs registered fake peers on
  the live ReLink seed since 2026-09-08. The suite can no longer reach real
  hosts, and the fake peers on ReLink are being removed separately.
