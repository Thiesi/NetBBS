"""
Node-wide NetBBS Link onboarding state (design doc §16, issue #219):
whether this node's SysOp has accepted, declined, or not yet answered
"join NetBBS Link through the project's reliable nodes" -- and how that
decision combines with the operator's own `[link] enabled` setting.

Follows `netbbs.config`'s `get_config`/`set_config` key-value convention
directly (same shape as `netbbs.managed_dns.state.OptIn`, the sibling
decision offered on the same first-run screen) rather than a dedicated
table: three scalars, all node-wide.

**Effective Link enablement is a two-source resolution, resolved once at
startup.** `netbbs.net.nodeconfig.LinkConfig.enabled` is tri-state:
`True`/`False` when the operator set it explicitly (TOML or CLI), `None`
when the config is silent. An explicit value always wins -- an operator
who wrote `enabled = false` never has Link switched on behind their back
by a SysOp's console answer, and one who wrote `enabled = true` keeps a
Link node whether or not anyone ever answered the prompt. Only when the
config is silent does the participation decision decide: accepted means
Link runs (as an outgoing-only node by default, so no port ever needs
opening) using the reliable-nodes roster as its seeds; declined or
undecided means Link stays off exactly as before this feature existed.

The participation decision *also* governs whether the roster is dialed
at all, independently of how Link came to be enabled: an operator with an
explicit `enabled = true` and a declined participation keeps their own
seeds only. Undecided counts as "not accepted" here -- a node upgraded in
place never starts dialing project infrastructure until a SysOp says so.
"""

from __future__ import annotations

import logging
from enum import Enum

from netbbs.config import get_config, set_config
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)


class Participation(str, Enum):
    """The SysOp's answer to reliable-node participation -- a tri-state,
    not a boolean, so "never asked yet" is distinguishable from "asked
    and declined" (the first-run screen fires only while UNDECIDED)."""

    UNDECIDED = "undecided"
    ACCEPTED = "accepted"
    DECLINED = "declined"


PARTICIPATION_CONFIG_KEY = "link_onboarding_participation"

# A cached copy of what the operator's *configuration* says about
# `[link] enabled` ("true"/"false"/"unset"), written once per startup by
# `netbbs.__main__.run` the same way it caches the node fingerprint for
# managed DNS. Purely so the SysOp-facing screens (which have a `db` but
# never a `NodeConfig`) can tell the truth about whether the participation
# answer is what actually decides Link on this node, or the config already
# did. Absent until the node has started once since this feature was added.
CONFIGURED_LINK_ENABLED_CONFIG_KEY = "link_configured_enabled"


def get_participation(db: Database) -> Participation:
    """Never raises: this sits on the startup path, every sync pass, and
    every SysOp login, so an unexpected stored value (a hand edit, a
    downgrade after a future value is added, a corrupt restore) must read
    as "not decided" -- which re-asks the SysOp -- rather than take the
    node down or lock every SysOp out."""
    value = get_config(db, PARTICIPATION_CONFIG_KEY)
    if value is None:
        return Participation.UNDECIDED
    try:
        return Participation(value)
    except ValueError:
        _logger.warning("Ignoring unexpected stored Link participation value %r; treating as undecided", value)
        return Participation.UNDECIDED


def set_participation(db: Database, decision: Participation) -> None:
    set_config(db, PARTICIPATION_CONFIG_KEY, decision.value)


def participation_accepted(db: Database) -> bool:
    """Whether the reliable-nodes roster may be dialed/used at all --
    the one question `netbbs.link.sync` asks every pass."""
    return get_participation(db) is Participation.ACCEPTED


def resolve_link_enabled(configured: bool | None, db: Database) -> bool:
    """The effective answer to "does this node run Link" (module
    docstring): explicit configuration wins; a silent config defers to
    the participation decision."""
    if configured is not None:
        return configured
    return participation_accepted(db)


# Sticky: set the first time this node starts with Link effectively on, and
# never cleared. Issue #594 retires a deleted account's username on a node that
# has *ever* run Link, because what peers hold about that name does not
# evaporate when Link is later switched off.
LINK_HAS_RUN_CONFIG_KEY = "link_has_run"


def mark_link_has_run(db: Database) -> None:
    """Record that this node has started with Link effectively enabled."""
    if get_config(db, LINK_HAS_RUN_CONFIG_KEY) != "true":
        set_config(db, LINK_HAS_RUN_CONFIG_KEY, "true")


# Whether this node advertises an address other nodes can dial, as of its last
# start with Link on (issue #627). The screens that tell a SysOp or a caller
# where something they publish goes have a database and no node configuration,
# and what they have to say differs completely between the two cases.
LINK_OUTGOING_ONLY_CONFIG_KEY = "link_outgoing_only"


def record_link_reachability(db: Database, *, outgoing_only: bool) -> None:
    value = "true" if outgoing_only else "false"
    if get_config(db, LINK_OUTGOING_ONLY_CONFIG_KEY) != value:
        set_config(db, LINK_OUTGOING_ONLY_CONFIG_KEY, value)


def link_is_outgoing_only(db: Database) -> bool:
    """Whether nobody can dial this node. False for a node that has never started Link."""
    return get_config(db, LINK_OUTGOING_ONLY_CONFIG_KEY) == "true"


def link_has_ever_run(db: Database) -> bool:
    """Whether any username on this node may be known to a Link peer.

    The marker answers for every start since it existed, and the migration
    that introduced it seeded it for a node that had run Link before then,
    from every kind of artifact Link leaves behind. One fallback covers what
    the marker cannot yet know: a node whose Link setting currently resolves
    on but which has not started since, so that a deletion made from
    `netbbs.admin` in that window still counts. Reads only, so it is safe
    inside a caller's open transaction.
    """
    if get_config(db, LINK_HAS_RUN_CONFIG_KEY) == "true":
        return True
    configured = get_configured_link_enabled(db)
    return resolve_link_enabled(None if configured == "unknown" else configured, db)


def set_configured_link_enabled(db: Database, configured: bool | None) -> None:
    value = "unset" if configured is None else ("true" if configured else "false")
    set_config(db, CONFIGURED_LINK_ENABLED_CONFIG_KEY, value)


def get_configured_link_enabled(db: Database) -> bool | None | str:
    """`True`/`False` when the last startup saw an explicit setting,
    `None` when it saw a silent config, or the string `"unknown"` when
    this node hasn't started since the feature was added (e.g. `netbbs.
    admin`'s bootstrap on a brand-new database) -- distinct from `None`
    so a screen never claims "your answer decides" before knowing."""
    value = get_config(db, CONFIGURED_LINK_ENABLED_CONFIG_KEY)
    if value is None:
        return "unknown"
    if value == "unset":
        return None
    return value == "true"
