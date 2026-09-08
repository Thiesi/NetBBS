"""
The reliable-nodes roster (design doc §8.3 source 3 and §16 "Issue
#219") -- project-operated infrastructure, not part of the installable
`netbbs` package.

Mostly a data directory: `reliable-nodes.json` is the roster itself,
served as static content from the `www.NetBBS.org` docroot and kept here
for review history, exactly like `services/managed_dns/` keeps the
managed-DNS backend. `check_roster.py` is the one piece of code, and it
runs on the project's side rather than on a node -- see this directory's
README for both.

Deliberately outside `src/` so `[tool.setuptools.packages.find]` never
packages it. The *node* half of this feature -- fetching the roster,
caching it, and dialing what it lists -- is
`netbbs.link.reliable_nodes`, which every node install does have.
"""

from __future__ import annotations
