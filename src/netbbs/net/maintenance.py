"""
Node-wide maintenance-mode gate (design doc §13.8): once activated, new connections are refused before
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
one-way, unconditional, pre-login gate with no bypass and no way back
*once a shutdown actually reaches its disconnect step*, since the whole
node is going away regardless of who's asking. Deliberately named and
checked differently rather than sharing one flag: the two answer
genuinely different questions ("is this node about to disappear" vs.
"should ordinary users be kept out for now"), and conflating them would
either weaken shutdown's hard guarantee or deny a SysOp their own
reason for toggling maintenance mode on in the first place.

`deactivate()` (design doc -- node management) is the one exception to
`activate()`'s "no way back" framing above: a *scheduled* graceful
shutdown now has a countdown a SysOp can cancel before it fires
(`netbbs.net.shutdown.SequenceScheduler`) -- if they do, new-login
admission must reopen too, or a cancelled shutdown would leave the node
silently unreachable forever. `run_shutdown_sequence` is the only
caller, and only from its own cancellation handling for that countdown
window; once a shutdown has actually reached `disconnect_all()`, there
is no calling `deactivate()` anymore, matching the rest of this
docstring's claim exactly for that point onward.
"""

from __future__ import annotations

MAINTENANCE_MESSAGE = "This node is shutting down for maintenance. Please try again shortly."

LOCKDOWN_MESSAGE = "This node is in maintenance mode. Only SysOps may connect right now. Please try again later."

# Design doc -- node management, Thiesi's own dogfood-testing report:
# shown to *every* connecting client, right after the banner and before
# the username prompt, regardless of what account (if any) they're
# about to log in as -- SysOp-ness isn't known until credentials verify,
# so this can't be targeted any more narrowly. Deliberately distinct
# wording from LOCKDOWN_MESSAGE above: that one is the hard rejection a
# non-SysOp actually gets turned away with; this is a heads-up shown to
# someone who may well still get in (a SysOp), so "please try again
# later" would be actively misleading here.
LOCKDOWN_NOTICE = "Note: this node is currently in maintenance mode. Only SysOps may log in right now."


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

    def deactivate(self) -> None:
        """Reopens new-login admission -- see this class's own docstring
        for the one narrow case this exists for (a scheduled graceful
        shutdown cancelled before it fires)."""
        self._active = False

    def is_active(self) -> bool:
        return self._active

    def enable_lockdown(self) -> None:
        self._lockdown = True

    def disable_lockdown(self) -> None:
        self._lockdown = False

    def is_lockdown_active(self) -> bool:
        return self._lockdown
