# Reliable-nodes roster (`reliable-nodes.json`)

The live half of NetBBS's hybrid reliable-nodes discovery (design doc
§8.3 source 3 and §16 "Issue #219"). Every node that has accepted
reliable-node participation fetches this file once a day from

    https://www.netbbs.org/reliable-nodes.json

and prefers it over the software-shipped fallback in
`netbbs.link.reliable_nodes.FALLBACK_RELIABLE_NODES`. Like
`services/managed_dns/`, this is project-operated infrastructure kept in
the repository for review history; it is not part of a node install.

## Format

```json
{"version": 1, "nodes": [{"name": "Reliable Link", "url": "http://ReLink.NetBBS.org:7862"}]}
```

- `version` must be exactly `1`. A node that sees any other value rejects
  the whole document and keeps its last good copy (or the fallback) — bump
  it only for an incompatible format change, alongside a NetBBS release
  that understands the new one.
- `nodes` is an ordered list; nodes dial entries in this order after the
  operator's own configured seeds. Each entry needs a non-empty `name`
  (≤ 64 characters, no control characters; shown to SysOps only) and a
  Link base `url` (`http://` or `https://`, ≤ 256 characters — the same
  shape as one `[link] seeds` entry, no path).
- Nodes keep at most the first 32 entries. Duplicate URLs are collapsed;
  malformed entries are skipped individually.

## Checking it (issue #313)

**Check the roster before publishing it, and periodically afterwards.**
Every entry here is dialed as a seed *and* used as a relay by every node
whose SysOp accepted Link participation, so an entry that stops
answering silently degrades the whole network — and nothing else
notices. That is not hypothetical: on 2026-09-08 this roster's one node
had its own Link participation switched off, nothing listened on 7862,
and the only trace anywhere was an ordinary dial failure in other nodes'
logs, indistinguishable from routine churn.

```sh
python -m services.reliable_nodes.check_roster              # the copy in this directory
python -m services.reliable_nodes.check_roster --published  # the live www.netbbs.org one
```

Each node gets `OK` (answered a Link hello), `DOWN` (nothing listening),
`NOT_LINK` (something answered, but it is not a Link node — a stale DNS
record, or a proxy in front of a node that is itself down) or
`THROTTLED` (rate-limited before the hello route ran, so liveness is
unconfirmed — re-run once it clears). `THROTTLED` is deliberately not a
pass: Link's rate limiter answers before routing, so a roster URL with
the wrong base path is throttled exactly like a correct one while a real
dial would 404. Anything other than a fully reachable,
structurally valid roster exits non-zero, so this can gate the publish
below or run from cron; `--quiet` prints only problems, which makes
silence the healthy outcome for a cron job that mails its output.

Structural checking mirrors the node-side parser exactly — the same
normalization (both fields stripped, trailing slashes removed before
de-duplication), the same limits (64-character names, 256-character
URLs, 32 kept entries, 256 raw entries, a 64 KiB document), and the same
verdicts. Anything a node would silently drop is reported here rather
than discovered after publishing. That duplication is deliberate (this
runs where `netbbs` is not installed) and therefore load-bearing:
`tests/test_reliable_nodes_check_roster.py` cross-checks it against the
real `parse_reliable_nodes` over a corpus of documents, because every
divergence would be a roster this gate approves and the network refuses.

**An empty roster is valid and exits zero.** Publishing `"nodes": []` is
the supported way to retire every entry, including the built-in
fallback — `get_cached_reliable_nodes` deliberately preserves an empty
fetched list rather than falling back — so the gate must not block it.

The probe needs no node identity and changes nothing on the node it
probes — it POSTs an unparseable hello to the one unauthenticated Link
route and checks for the rejection a live Link server gives it. See the
module's own docstring for why that is the right signal, and
`tests/test_reliable_nodes_check_roster.py` for the test that keeps it
honest against a real `LinkServer`.

The node side of the same gap is covered separately: a node that reaches
no seed, reliable node, or fallback candidate for several consecutive
sync passes now says so as a distinct warning, which lands in its
SysOp-visible diagnostic log (`netbbs.link.sync`, design doc §13.11).

## Deploying

The file is served as plain static content from the `www.NetBBS.org`
docroot on the project's web host. Check it first (above), then upload
it to `/tmp/` and move it into place with the same ownership as the
site's other static files, then verify both its shape and its
reachability as published:

```sh
curl -s https://www.netbbs.org/reliable-nodes.json | python -m json.tool
python -m services.reliable_nodes.check_roster --published
```

Removing a node from this roster is how a retired reliable node actually
stops being dialed: the fallback list in source is only used by nodes
that have never completed a fetch.
