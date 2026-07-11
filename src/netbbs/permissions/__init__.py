"""
Permission/level-gating plumbing.

See design doc §13 (Permissions & Moderation) for the full model this is
the Phase 1 foundation of.
"""

from netbbs.permissions.levels import (
    InsufficientLevelError,
    meets_level,
    require_level,
    requires_level,
)

__all__ = [
    "InsufficientLevelError",
    "meets_level",
    "require_level",
    "requires_level",
]
