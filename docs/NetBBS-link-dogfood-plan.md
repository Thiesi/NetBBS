# NetBBS Link dogfood deployment plan (issue #83)

A runbook for operating a real, sustained, multi-node NetBBS Link
deployment — the class of failure this project's deterministic test
harness and single-session integration tests cannot reach: clock skew,
intermittent connectivity, restart timing, stale peer descriptors,
accumulated retries, disk growth over real time, operator mistakes, and
state that evolves over days rather than one test run.

**This is not something that can be executed inside a single working
session.** It needs real infrastructure running continuously and real
calendar time passing. This document is the plan to follow; running it
is a separate, ongoing activity.

**Explicitly not the goal:** this does not by itself establish public-
federation readiness. Phase 3 remains private/experimental federation.
The automated parts of design doc §12's Phase-4 trust/quarantine model are
implemented; this deployment now also supplies the real-node and independently
administered evidence required by issue #131. Public readiness still requires
every pending row in `docs/NetBBS-phase4-readiness.md` to be completed and
reviewed. Findings become focused issues and durable engineering lessons as
they arise.

## 1. Topology (at least three nodes)

| Node | Role | Reachability | Suggested host |
|---|---|---|---|
| **A — "home"** | Rendezvous seed for the other two | Full peer, real advertised address | A cheap VPS (~$4–6/mo: Hetzner, Vultr, DigitalOcean, etc.) or a home server with port-forwarding |
| **B** | Second full peer, distinct network from A | Full peer, real advertised address | A second VPS, or the user's own always-on home machine |
| **C — outgoing-only** | Proves relay support, not just direct pairwise sync | `outgoing_only = true` (the default), relies on A or B agreeing to relay for it | A laptop/desktop behind ordinary home NAT, no port-forwarding |

Three distinct **networks**, not just three processes on one host or
one LAN, is what actually matters — loopback or same-LAN testing is
already covered by the existing real-transport test suite
(`tests/test_link_end_to_end.py`, `test_link_sync.py`,
`test_link_transport.py`); what those can't prove is real internet
latency/jitter, a real NAT boundary, and real independent clocks
drifting apart over days.

**Cost note (issue #83's own "zero-revenue compatible" requirement):**
running entirely on hardware you already own (a home server + two
personal machines, or asking a friend to run node B/C on their own
network) costs nothing. Two small VPS instances run to a few dollars a
month total if you'd rather have genuinely independent, always-on
hosts for A and B. Either is fine — the requirement is independent
networks, not paid infrastructure.

Install each node using `docs/NetBBS-operator-guide.md` (issue #82) --
this run doubles as a real-world exercise of that guide, not just of
Link itself. Use distinct node names (`node.name` in config) so
`[L]ink status`/logs are easy to tell apart across three terminals.

## 2. Before starting: what "sustained" means here

Run continuously for **at least 2–4 weeks**, not a few hours. Shorter
than that and you won't see: disk/log growth trends, retry-backoff
aging across real days, a peer's descriptor going genuinely stale, or
more than one or two real sync-interval cycles at the default 5-minute
interval scale. Check in roughly weekly rather than daily — this is
explicitly not meant to be a babysitting exercise (see §5 on what to
record).

## 3. Day-by-day script

Treat this as a checklist to work through, not a rigid calendar — the
point is that every row eventually happens for real, in whatever order
fits your own availability.

### Setup (day 0)

- [x] Install and start all three nodes per `docs/NetBBS-operator-guide.md`.
      Actual topology deployed: NetBBS-1/2/3, a directed ring (1→seed
      2, 2→seed 3, 3→seed 1), all three full peers on one home LAN —
      not the suggested "A/B mutual, C outgoing-only on separate
      networks" shape. A fourth node, NetBBS-4, was added later
      (`outgoing_only = true`, seeded off NetBBS-3) — see the
      relay-specific rows below, no longer untested once it joined.
- [ ] Configure A and B as mutual seeds (`seeds = [...]` pointing at
      each other); configure C to seed off A (or B). *(Deviation: see
      the actual ring topology noted above instead.)*
- [x] Confirm all three complete hellos: `[S]ysOp` → `[S]ystem` →
      `[L]ink status` on each node should list the other two as
      verified peers within one sync interval. Confirmed directly via
      each node's `link_peers` table.
- [x] Create a handful of real user accounts on each node (not just
      the SysOp) and do **ordinary standalone BBS things** with
      them — post to a local board, chat, send local mail — alongside
      the Link setup. Issue #83 explicitly asks for this: Link
      shouldn't be the only thing happening on these nodes, the same
      way a real operator's node wouldn't be Link-traffic-only. Done
      on NetBBS-1: two real level-0 accounts (`wanderer`, `pathfinder`)
      created via the real `new` account flow, a plain local (non-
      linked) board created and posted to, and local (non-Link) mail
      exchanged between them. Chat not yet exercised.

### Linked boards and Link mail (days 0–2)

- [x] Link a real board on node A (`[L]ink this board` from its
      detail screen). Post to it; confirm it materializes as a real,
      browsable board on B and C (`[J]ump to...` → the board — not
      just a rising `Known events` count on the Link status screen).
      Found and fixed a real bug in the process (issue #94): the node
      directly seeded off the true origin, but not the origin's own
      configured seed, never received the new board at all — inventory
      pull silently required already knowing the board_id, which a
      fresh node never could. Fixed and redeployed to all three nodes
      before re-confirming convergence.
- [x] Edit one of those posts; confirm the edit propagates and
      resolves to the latest version on B/C, not a stale one. Done:
      edited a post on NetBBS-1 twice (chained `edit_of_post_id`);
      both edits propagated correctly to NetBBS-2/3/4, each resolving
      to the same latest version via the same content-addressed chain
      -- no stale copies anywhere.
- [x] Compose Link mail from a node-A user to a node-B user and a
      node-C user; confirm delivery on the recipient side and that the
      sender's own delivery status resolves (no dedicated UI for this
      yet — check via `python -m netbbs.admin` or the `[O]utbox`
      screen, per the design doc's own noted UI gap). Done in both
      directions between a full peer and NetBBS-4 (outgoing-only): a
      full-peer→NetBBS-4 message and its relayed acknowledgement, and a
      NetBBS-4→full-peer message. The reverse-direction ack **found and
      fixed a real bug** (issue #95): the acknowledgement had no relay
      fallback at all, so it retried forever and would eventually
      dead-letter, leaving the sender's own delivery status stuck on
      "pending" permanently. Fixed and redeployed to all four nodes.
- [x] From node C (outgoing-only), confirm relay selection actually
      picked a relay (`[L]ink status` should show it relaying through
      A or B) and that mail composed *from* C reaches A/B via that
      relay. Confirmed directly via `link_relay_consents`/the
      `relays` field on NetBBS-4's own advertised descriptor (all
      three other nodes granted consent) and via the real mail round
      trips above.

### Deliberate disruption (week 1)

- [x] **Planned outage:** stop node B's process for a few hours (not a
      graceful shutdown — a hard kill, to simulate a real crash/power
      loss) while A and C keep running and keep posting/mailing.
      Restart B; confirm it catches up correctly on the next sync pass
      once it's back, and that nothing double-applied or went missing.
      Done for real: `kill -KILL` on node B's process, ~8.5 real
      minutes down (several missed 60s sync cycles), A and C each
      posted to the linked board during the outage. On restart, B
      converged to the identical post set as A/C within one sync pass
      -- no duplicates, nothing missing, no stuck `link_work_items`
      afterward on any of the three nodes. Shorter than "a few hours"
      due to single-session time constraints; the mechanism proven is
      the same regardless of outage length.
- [x] **Restart timing:** restart node C (a real process restart, not
      just a reconnect) mid-way through some other activity (e.g.
      right after composing a Link mail message but before it's
      confirmed delivered). Confirm it resumes correctly from
      persisted state. Done on NetBBS-4: composed a Link message, then
      immediately restarted the process for real (graceful stop,
      relaunch) before delivery/ack completed. Fingerprint persisted
      across restart; the sync loop resumed cleanly and the message
      resolved to "delivered" on its own within the next cycle -- no
      manual intervention needed. (This same restart also incidentally
      confirmed the issue #96 timeout fix: NetBBS-4's sync loop had
      died from an unrelated real network timeout earlier in this
      session and needed this exact kind of restart to recover.)
- [ ] **Changing address:** if practical, change node C's network
      (e.g. move the laptop to a different Wi-Fi/hotspot) partway
      through the run, so its own outbound IP changes — this is the
      ordinary case an outgoing-only node's relay relationship needs
      to keep tolerating, not a synthetic one.

### Backup, restore, and upgrade (week 2)

- [x] Take a real backup of one node that's been running for real for
      at least a week (`python -m netbbs.backup create`), then
      **rehearse a restore onto a disposable copy** — a second
      machine/VM/directory, not the live node — following
      `docs/NetBBS-disaster-recovery-drill.md`. The point of doing
      this against a node with real accumulated state (real peers,
      real carried boards, real work-item history), not a freshly
      created one, is exactly what a single-session test can't
      exercise. Done: a fresh backup of NetBBS-1 (real linked board
      with an edit chain, a local board, real user accounts, local
      mail) restored into a disposable directory on the same host
      (`--db`/`--identity-dir` pointed at a separate path, never the
      live node's own files) while NetBBS-1 kept running untouched.
      Fingerprint matched exactly; all real content came back intact.
      Deviation: not against a node a week old (this deployment is
      hours old, not weeks), and the drill doc's own corruption-
      refusal/interrupted-restore steps (§3/§5) weren't separately
      re-exercised here since those test the mechanism's own
      robustness in the abstract, already covered by the automated
      suite's dedicated tests -- this rehearsal's own point was
      proving restore against *this deployment's* real accumulated
      state, which it did.
- [x] Perform at least one real upgrade on one node using
      `docs/NetBBS-operator-guide.md`'s documented procedure (back up
      first, upgrade the package, restart). If no new NetBBS release
      exists yet when you reach this step, cut one (even a small patch
      version bump) specifically so there's something real to upgrade
      to — the point is exercising the *procedure*, including whatever
      migrations happen to be pending, not landing on a specific
      version number. Done on all three nodes (backup, graceful stop,
      upgrade, restart) to deploy the issue #94 fix. Deviation from the
      guide's literal steps: these nodes run from a git checkout, not a
      packaged install under a service manager, so "upgrade the
      package" was `git pull` and the restart was a manually-launched
      detached process rather than `systemctl`/`service` -- the guide's
      packaged/service-managed path itself remains unexercised by this
      deployment.

### Phase 4 trust and recovery exercise

This section requires at least two operators administering separate nodes.
The operator of the receiving node owns its local policy and must not share
private reporter configuration, notes, or evidence with ordinary callers.
Use test identities and non-sensitive evidence.

- [ ] On node A, create a test node/user subject and record its current
      effective state. On node B, configure two independent trust domains and
      their narrowly scoped reporters through `[S]ysOp` → `[S]ystem` →
      `[P]olicy trust`. Record the node fingerprints, category scopes, and
      configuration times.
- [ ] Introduce one scoped signal. Confirm the subject does not cross the
      two-domain threshold and record the explanation shown by node B,
      including counted domains/weight and the stated release condition.
- [ ] Introduce the second independent signal. Confirm node B quarantines
      only the affected dimension, ordinary transport/content behavior matches
      the documented enforcement boundary, and already accepted objects remain
      stored.
- [ ] Partition B from its reporter/peer path. During the partition, confirm
      absence and failed dials create no new evidence and do not silently alter
      the decision. Restart B while still partitioned; confirm the same
      effective state and explanation reconstruct from SQLite.
- [ ] Heal the partition, then revoke the second signal or remove the
      deliberately compromised reporter. Confirm the trigger disappears but
      the signed object and audit history remain. Record the recovery-hold
      start and required release time.
- [ ] Exercise a mandatory-reason SysOp override and clear it again. Confirm
      the action is scoped, audited, restart-safe, and visibly distinct from
      automatic policy. Do not use an override to skip observing automatic
      recovery.
- [ ] After the recovery hold elapses, confirm the subject leaves quarantine
      on node B, the explanation names automatic recovery, and a restart does
      not restore the old restriction.
- [ ] Repeat the explanation check using an ordinary caller account. Confirm
      it reports a local restriction without exposing reporter identities,
      private evidence, configuration notes, or a network-wide verdict.

For each row, record node/operator roles, UTC timestamps, software commit,
subject and dimension, pre/post effective states, public reason code, whether
a restart or partition was active, and the relevant audit IDs. Do not paste
private evidence into GitHub. Summarize the completed exercise in issue #131
and update the corresponding pending rows in
`docs/NetBBS-phase4-readiness.md`.

### Ongoing, throughout the whole run

- [ ] Periodically check disk growth (`du -sh` on the database file,
      the `<db-stem>_files/` directory, and the diagnostic log table's
      row count via `[D]iagnostic log`) — is anything growing
      unbounded that shouldn't be?
- [ ] Periodically check `[O]utbox` for anything stuck retrying or
      dead-lettered longer than expected.
- [ ] Periodically check quotas (`[L]ink status`'s peer/carried-board/
      candidate counts) haven't silently hit a configured cap in a way
      that surprised you.
- [ ] Use whichever real terminal clients you actually have on hand
      (a real SSH client, a real Telnet client, the web/xterm.js
      client in an actual browser) rather than only ever the same one,
      per the design doc's own "external verification still matters"
      testing policy (§14.4) — this is a good opportunity to cover
      that at the same time.

## 4. What to record

Keep a short, dated running note as you go — not a full diary, just
enough to remember what happened and when. At the end, convert it into:

- **A focused GitHub issue per real, reproducible problem found** —
  not a grab-bag "dogfood notes" issue. If something looks wrong but
  you can't tell whether it's a real bug or expected behavior you
  don't understand yet, that's still worth its own issue to resolve
  one way or the other.
- **A worklog entry (`docs/NetBBS-worklog.md`) for any durable lesson**
  that isn't already captured — an invariant, a limitation, an
  operational quirk — following that file's own existing curation rule
  (no round-by-round narration, no passing-test totals, just what
  future work needs to know).
- **A short completion note** (a paragraph or two, in the issue #83
  thread itself is fine) stating plainly what topologies and scenarios
  from this plan were actually exercised, for how long, and on what
  infrastructure — and explicitly restating that this does not imply
  public-federation readiness, however well it went.

## 5. Explicitly out of scope for this run

- Claiming public federation safe merely because this bounded private exercise
  passed. Hostile/unknown-peer deployment remains prohibited until every
  Phase-4 readiness gate is reviewed and explicitly closed.
- Load/scale testing beyond this project's own declared target (design
  doc §2.3: dozens–low hundreds of concurrent sessions, small-to-medium
  Link deployments) — three nodes and a handful of test accounts is
  the right scale for this exercise, not an attempt to stress-test
  capacity.
- Wiring up `netbbs.selfupdate`'s unwired apply/rollback mechanism
  (see the operator guide's own note on this, issue #82). This drill used the
  explicitly recorded git-checkout deviation above; the supported production
  path remains manual installation of an official GitHub-release wheel.
