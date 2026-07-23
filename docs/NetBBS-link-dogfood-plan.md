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

**Explicitly not the goal:** this does not establish public-federation
readiness. Phase 3 remains private/experimental federation regardless
of how this goes; Phase 4's trust/quarantine model (issue #55) is the
actual public-readiness gate. This dogfood run only proves Phase 3's
existing behavior holds up under sustained real operation.

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

- [ ] Install and start all three nodes per `docs/NetBBS-operator-guide.md`.
- [ ] Configure A and B as mutual seeds (`seeds = [...]` pointing at
      each other); configure C to seed off A (or B).
- [ ] Confirm all three complete hellos: `[S]ysOp` → `[S]ystem` →
      `[L]ink status` on each node should list the other two as
      verified peers within one sync interval.
- [ ] Create a handful of real user accounts on each node (not just
      the SysOp) and do **ordinary standalone BBS things** with
      them — post to a local board, chat, send local mail — alongside
      the Link setup. Issue #83 explicitly asks for this: Link
      shouldn't be the only thing happening on these nodes, the same
      way a real operator's node wouldn't be Link-traffic-only.

### Linked boards and Link mail (days 0–2)

- [ ] Link a real board on node A (`[L]ink this board` from its
      detail screen). Post to it; confirm it materializes as a real,
      browsable board on B and C (`[J]ump to...` → the board — not
      just a rising `Known events` count on the Link status screen).
- [ ] Edit one of those posts; confirm the edit propagates and
      resolves to the latest version on B/C, not a stale one.
- [ ] Compose Link mail from a node-A user to a node-B user and a
      node-C user; confirm delivery on the recipient side and that the
      sender's own delivery status resolves (no dedicated UI for this
      yet — check via `python -m netbbs.admin` or the `[O]utbox`
      screen, per the design doc's own noted UI gap).
- [ ] From node C (outgoing-only), confirm relay selection actually
      picked a relay (`[L]ink status` should show it relaying through
      A or B) and that mail composed *from* C reaches A/B via that
      relay.

### Deliberate disruption (week 1)

- [ ] **Planned outage:** stop node B's process for a few hours (not a
      graceful shutdown — a hard kill, to simulate a real crash/power
      loss) while A and C keep running and keep posting/mailing.
      Restart B; confirm it catches up correctly on the next sync pass
      once it's back, and that nothing double-applied or went missing.
- [ ] **Restart timing:** restart node C (a real process restart, not
      just a reconnect) mid-way through some other activity (e.g.
      right after composing a Link mail message but before it's
      confirmed delivered). Confirm it resumes correctly from
      persisted state.
- [ ] **Changing address:** if practical, change node C's network
      (e.g. move the laptop to a different Wi-Fi/hotspot) partway
      through the run, so its own outbound IP changes — this is the
      ordinary case an outgoing-only node's relay relationship needs
      to keep tolerating, not a synthetic one.

### Backup, restore, and upgrade (week 2)

- [ ] Take a real backup of one node that's been running for real for
      at least a week (`python -m netbbs.backup create`), then
      **rehearse a restore onto a disposable copy** — a second
      machine/VM/directory, not the live node — following
      `docs/NetBBS-disaster-recovery-drill.md`. The point of doing
      this against a node with real accumulated state (real peers,
      real carried boards, real work-item history), not a freshly
      created one, is exactly what a single-session test can't
      exercise.
- [ ] Perform at least one real upgrade on one node using
      `docs/NetBBS-operator-guide.md`'s documented procedure (back up
      first, upgrade the package, restart). If no new NetBBS release
      exists yet when you reach this step, cut one (even a small patch
      version bump) specifically so there's something real to upgrade
      to — the point is exercising the *procedure*, including whatever
      migrations happen to be pending, not landing on a specific
      version number.

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

- Proving public federation safe against a hostile/unknown peer —
  that's Phase 4 (issue #55), not this.
- Load/scale testing beyond this project's own declared target (design
  doc §2.3: dozens–low hundreds of concurrent sessions, small-to-medium
  Link deployments) — three nodes and a handful of test accounts is
  the right scale for this exercise, not an attempt to stress-test
  capacity.
- Wiring up `netbbs.selfupdate`'s unwired apply/rollback mechanism
  (see the operator guide's own note on this, issue #82) — the upgrade
  step above uses the actually-supported package-manager path.
