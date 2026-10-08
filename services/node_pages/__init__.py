"""
The www.netbbs.org node pages (design doc §8.13, issue #1165).

A periodic job on the host that runs Reliable Link and the managed-DNS
service. It reads two sources, both read-only:

- the managed-DNS service's registrations database, for which netbbs.org
  name each node fingerprint holds and since when;
- Reliable Link's node map as a caller sees it, the JSON that
  `python -m netbbs.admin export-node-map` writes,

and writes a static site: `<out>/<name>/index.html` for each page and
`<out>/index.html` listing them, served as `/~<name>` and `/nodes/`.

Stateless: every run derives every page from the two sources, so a name
that passes to another node gives it a fresh page by itself. Standard
library only, so it runs from the release's source archive beside
`services/managed_dns` without installing anything.
"""
