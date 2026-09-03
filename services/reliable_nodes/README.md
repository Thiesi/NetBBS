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

## Deploying

The file is served as plain static content from the `www.NetBBS.org`
docroot on the project's web host. Upload it to `/tmp/` and move it into
place with the same ownership as the site's other static files, then
verify:

```sh
curl -s https://www.netbbs.org/reliable-nodes.json | python -m json.tool
```

Removing a node from this roster is how a retired reliable node actually
stops being dialed: the fallback list in source is only used by nodes
that have never completed a fetch.
