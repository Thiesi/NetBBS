"""
Node-wide maintenance-mode gate (design doc round 51, Phase 2
post-Track-5 fixes): once activated, new connections are refused before
login even begins (see `netbbs.net.login_flow.handle_session`) — the
piece a deliberate shutdown sequence needs to stop admitting new users
while it broadcasts a warning and disconnects everyone already
connected. Deliberately its own tiny module, not folded into
`netbbs.net.session_registry`: gating *new* connections and tracking
*existing* ones are related but distinct concerns.

**Lockdown** (design doc §13.8) is a second, independent gate added to
this same class: a SysOp-toggleable `[M]aintenance mode` that blocks
new *non-SysOp* logins only, checked *after* credentials verify (so a
SysOp can still reach the menu that turns it back off), and reversible
-- unlike `activate()`/`is_active()` above, which is shutdown's
one-way, unconditional, pre-login gate with no bypass and no way back,
since the whole node is going away regardless of who's asking.
Deliberately named and checked differently rather than sharing one
flag: the two answer genuinely different questions ("is this node
about to disappear" vs. "should ordinary users be kept out for now"),
and conflating them would either weaken shutdown's hard guarantee or
deny a SysOp their own reason for toggling maintenance mode on in the
first place.
"""

from __future__ import annotations

MAINTENANCE_MESSAGE = "This node is shutting down for maintenance. Please try again shortly."

LOCKDOWN_MESSAGE = "This node is in maintenance mode. Only SysOps may connect right now. Please try again later."


class MaintenanceMode:
    """One instance per running node (constructed once in
    `netbbs.__main__`, threaded down through `handle_session` the same
    way `throttle`/`presence` already are). Plain flags, not
    `asyncio.Event`s — nothing ever needs to *wait* for either to flip,
    only check current state at the relevant checkpoint (pre-login for
    `is_active`, post-authentication for `is_lockdown_active`)."""

    def __init__(self) -> None:
        self._active = False
        self._lockdown = False

    def activate(self) -> None:
        self._active = True

    def is_active(self) -> bool:
        return self._active

    def enable_lockdown(self) -> None:
        self._lockdown = True

    def disable_lockdown(self) -> None:
        self._lockdown = False

    def is_lockdown_active(self) -> bool:
        return self._lockdown
