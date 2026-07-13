"""
Node-wide maintenance-mode gate (design doc round 51, Phase 2
post-Track-5 fixes): once activated, new connections are refused before
login even begins (see `netbbs.net.login_flow.handle_session`) — the
piece a deliberate shutdown sequence needs to stop admitting new users
while it broadcasts a warning and disconnects everyone already
connected. Deliberately its own tiny module, not folded into
`netbbs.net.session_registry`: gating *new* connections and tracking
*existing* ones are related but distinct concerns.
"""

from __future__ import annotations

MAINTENANCE_MESSAGE = "This node is shutting down for maintenance. Please try again shortly."


class MaintenanceMode:
    """One instance per running node (constructed once in
    `netbbs.__main__`, threaded down through `handle_session` the same
    way `throttle`/`presence` already are). A plain flag, not an
    `asyncio.Event` — nothing ever needs to *wait* for it to flip, only
    check its current state at the top of each new connection."""

    def __init__(self) -> None:
        self._active = False

    def activate(self) -> None:
        self._active = True

    def is_active(self) -> bool:
        return self._active
