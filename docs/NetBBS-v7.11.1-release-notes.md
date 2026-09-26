# NetBBS v7.11.1

A patch release with two fixes found while running the Phase 4 trust
exercise on the three test nodes. It is cut from v7.11.0 and carries nothing
else. Everything else merged since then waits for v7.12.0.

**Nothing migrates.** The node database stays at schema 72, and no protocol
or door API version changed. No setting in `netbbs.toml` changed.

## Link sync could stop for good after one peer-list response (#703)

A node that had met more than 100 peers answered every peer-list request with
all of them. A receiver refuses a list longer than 100, and that refusal was
not caught anywhere, so the receiver's whole outbound Link sync stopped for the
rest of its uptime. A restart hit it again on the next pass. On the test
network one reliable node had met 495 peers, and every node it had just
established went silent.

- A node now sends at most 100 descriptors, the most recently signed first.
- A receiver treats a refused peer list like any failed request: it logs a
  warning, and the pass continues. That covers peers still on 7.11.0.
- A failure while syncing with one seed is logged with its traceback and
  counts as "not reached". It no longer ends the sync task, so the other
  seeds and the next pass still run.

**If a node's log shows `Link sync task failed -- outbound Link activity will
not resume this node uptime`**, upgrade it and restart it. Upgrading the seed
that sent the long list fixes the cause for every node that dials it.

## The NetBSD rc.d script could lose track of a running node (#693)

`examples/netbbs.rc` checks that the process it just started is NetBBS. For a
moment during startup that check can fail, and the script took one such
failure as "failed to start" and deleted the pidfile of a node that was coming
up. The node kept running untracked, and the next `start` would have launched
a second one against the same database. The script now waits: a process that
has died is still a failure at once, while one that does not look like NetBBS
yet is given until `netbbs_start_timeout`. A process that still does not look
like NetBBS then is dropped from tracking, never signalled.

**MANUAL:** the rc.d script is not part of the wheel. On NetBSD, copy the new
`examples/netbbs.rc` over your installed `/etc/rc.d/netbbs`. Until you do,
check `service netbbs status` after each start.

## Upgrade and rollback

Replace the wheel and restart. Rolling back is a wheel swap to 7.11.0, which
has the same schema. The key-rotation caveat from the 7.11.0 notes still
applies: a node that has rotated a signing key needs 7.11 to open mail sealed
to a retired key.

## Verification boundaries

- **The peer-list fix is tested in-process**, with a lowered cap and a real
  receiver, and the full suite passed on it. It has not yet run against the
  495-peer node; deploying this release to the test nodes is that check.
- **The rc.d fix has no test in the suite**, which cannot run the script on
  the development machine. It was exercised on NetBSD 11 with stand-in node
  processes: the old script dropped a live node's pidfile intermittently, and
  the fixed one never did.
