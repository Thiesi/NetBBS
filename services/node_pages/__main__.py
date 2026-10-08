"""
`python -m services.node_pages --registrations DB --node-map JSON --out DIR`
-- builds the www.netbbs.org node pages once (design doc §8.13, issue
#1165). Run it periodically, after `python -m netbbs.admin export-node-map
--output JSON` on Reliable Link; see `services/node_pages/README.md`.

Exits 1 without touching `--out` when a source cannot be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.node_pages.build import SourceError, build
# The stdlib CLI-prose wrapper the other standalone service already has.
from services.reliable_nodes.check_roster import print_wrapped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.node_pages",
        description="Build the www.netbbs.org node pages from the managed-DNS registrations and "
                    "Reliable Link's exported node map.",
    )
    parser.add_argument("--registrations", type=Path, required=True,
                        help="the managed-DNS service's database (MANAGED_DNS_DB_PATH); opened read-only")
    parser.add_argument("--node-map", type=Path, required=True,
                        help="the JSON written by `python -m netbbs.admin export-node-map --output`")
    parser.add_argument("--out", type=Path, required=True,
                        help="the directory served as /nodes/; replaced whole on every run")
    args = parser.parse_args(argv)
    try:
        count = build(args.registrations, args.node_map, args.out)
    except SourceError as exc:
        print_wrapped(f"Not built: {exc}", file=sys.stderr)
        return 1
    print_wrapped(f"Wrote {count} node pages to {args.out}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
