"""
FTN addresses: `zone:net/node[.point][@domain]` (FRL-1002, "5D").

Zone, net, node and point are unsigned 16-bit numbers, the width every
packet field holds them in. The domain names a network (`fidonet`,
`fsxnet`), at most eight characters; it is not a DNS name and is compared
case-insensitively. Two addresses are the same node when their 4D parts
match: the domain is carried for display and for `MSGID`, not for routing
inside one network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from netbbs.ftn import FtnFormatError

MAX_DOMAIN = 8
_MAX_NUMBER = 0xFFFF

_ADDRESS = re.compile(
    r"(?P<zone>\d{1,5}):(?P<net>\d{1,5})/(?P<node>\d{1,5})"
    r"(?:\.(?P<point>\d{1,5}))?(?:@(?P<domain>[A-Za-z0-9_-]{1,8}))?"
)


@dataclass(frozen=True)
class FtnAddress:
    zone: int
    net: int
    node: int
    point: int = 0
    domain: str | None = None

    def __post_init__(self) -> None:
        for name in ("zone", "net", "node", "point"):
            value = getattr(self, name)
            if not 0 <= value <= _MAX_NUMBER:
                raise FtnFormatError(f"FTN {name} {value} is outside 0-{_MAX_NUMBER}")
        if self.domain is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", self.domain):
                raise FtnFormatError(f"FTN domain {self.domain!r} is not 1-{MAX_DOMAIN} letters, digits, - or _")
            object.__setattr__(self, "domain", self.domain.lower())

    def __str__(self) -> str:
        text = f"{self.zone}:{self.net}/{self.node}"
        if self.point:
            text += f".{self.point}"
        if self.domain:
            text += f"@{self.domain}"
        return text

    @property
    def four_d(self) -> str:
        """`zone:net/node[.point]`, the form shown to callers."""
        text = f"{self.zone}:{self.net}/{self.node}"
        return f"{text}.{self.point}" if self.point else text

    @property
    def net_node(self) -> tuple[int, int]:
        """The 2D pair SEEN-BY and PATH are written in."""
        return (self.net, self.node)

    def same_node(self, other: FtnAddress) -> bool:
        """The same 4D address, whatever the domains say."""
        return (self.zone, self.net, self.node, self.point) == (other.zone, other.net, other.node, other.point)

    def with_domain(self, domain: str | None) -> FtnAddress:
        return FtnAddress(self.zone, self.net, self.node, self.point, domain)


def parse_address(text: str) -> FtnAddress:
    """Parse a whole string as an address; raises `FtnFormatError`."""
    match = _ADDRESS.fullmatch(text.strip())
    if match is None:
        raise FtnFormatError(f"{text!r} is not an FTN address (zone:net/node[.point][@domain])")
    return _from_match(match)


def find_address(text: str) -> FtnAddress | None:
    """The last address inside `text`, as an Origin line ends with one."""
    found = None
    for match in _ADDRESS.finditer(text):
        try:
            found = _from_match(match)
        except FtnFormatError:
            continue
    return found


def _from_match(match: re.Match[str]) -> FtnAddress:
    return FtnAddress(
        zone=int(match["zone"]),
        net=int(match["net"]),
        node=int(match["node"]),
        point=int(match["point"] or 0),
        domain=match["domain"],
    )
