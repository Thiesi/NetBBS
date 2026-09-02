"""
The managed netbbs.org subdomain + dynamic DNS service (design doc §16,
issue #201) -- the project-operated backend, not part of the installable
`netbbs` package.

Deliberately lives outside `src/` so `[tool.setuptools.packages.find]`
never picks it up: every SysOp's own node runs `netbbs.managed_dns` (the
*client*, `src/netbbs/managed_dns/`), but this service itself runs once,
operated by the project, the same way the already-live netbbs.org
website is a separate deployment rather than something bundled into
every node install. See `services/managed_dns/README.md` for how to
actually run it -- that operational step (a host, a real BIND server
configured with a matching TSIG key, DNS delegation) is not performed by
this code.
"""

from __future__ import annotations
