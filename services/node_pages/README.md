# Node pages for www.netbbs.org

Builds `https://www.netbbs.org/~<name>`, one public page per node that holds
a managed netbbs.org name and that Reliable Link has met, plus the list at
`https://www.netbbs.org/nodes/`. The design is design doc §8.13 and the §16
entry for issue #1165. This file covers what the generator needs and how it
runs.

## What it reads

| Source | What | How |
| --- | --- | --- |
| The managed-DNS service's database (`MANAGED_DNS_DB_PATH`) | which fingerprint holds which name, its status, when it was registered | opened read-only (`mode=ro`), never migrated |
| Reliable Link's node map | the caller's view of every node: name, dial-in addresses, first contact, last heard, page setting, release (major.minor) and the Linked boards its guest may read | the JSON from `python -m netbbs.admin export-node-map --output FILE` |

Nothing else. The export carries no field a caller on Reliable Link cannot
see, and the generator reads no column of the registrations table beyond
name, fingerprint, status and creation time. The address the service last saw
never reaches it.

## Which pages exist

A page exists for a fingerprint that holds a `matured`, `abandoned` or
`released` name and appears in the export as `met`, unless its descriptor's
`node_page` is `off`. A fingerprint holding several names (a rename leaves the
old one released) gets one page, at its best name: matured, then abandoned,
then released. `pending` names have no page yet; `revoked` names lose theirs.

Each page is *active* (heard within 7 days), *quiet* (within 30) or *left*
(longer, or its name released). It is `noindex` unless the node chose
`indexed`. `/nodes/` lists every page and is itself `noindex, follow`, so a
board that chose `noindex` is not found by name through the list.

## Running it

```sh
python -m netbbs.admin export-node-map --db <ReLink db> --identity-dir <ReLink identity dir> \
    --output /home/thiesi/node-pages/node-map.json
python -m services.node_pages --registrations /home/thiesi/managed-dns/registrations.db \
    --node-map /home/thiesi/node-pages/node-map.json --out /home/thiesi/node-pages/nodes
```

Run both from a periodic job, e.g. every 15 minutes, the managed-DNS
check-in interval; a SysOp's page setting takes effect at Reliable Link's next
contact with the node and the next run after that. Standard library only, so
it runs from the release's source archive like `services/managed_dns`.
The paths above are examples; the deployment runbook in the ops repository
holds the real ones.

`--out` is replaced whole on every run: the site is built in `.<out>.new`
beside it and swapped in by two renames, so the generator needs write access
to the directory that contains `--out`, and nothing else may live in `--out`.
Apache serves it as `/nodes/` and maps `/~<name>` to `<out>/<name>/index.html`
and `/~<name>/badge.svg` to `<out>/<name>/badge.svg` itself (`AliasMatch`, so
`mod_userdir` does not answer).

Each page directory also holds `badge.svg`: "NetBBS Link · member since
<month> · <state>", for a SysOp to put on their own website with the snippet
the page shows. It carries no text the node supplied. A run that cannot
read either source exits 1, prints why, and leaves the current site as it is.
