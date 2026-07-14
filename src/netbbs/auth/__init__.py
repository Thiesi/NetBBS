"""
Local user account management: registration, password auth, keypair auth.

Design doc §5: both password auth (simple/fallback) and keypair-based
(passwordless) auth are supported, and either may be used alone.
"""

from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    User,
    UserManagementError,
    authenticate_keypair,
    authenticate_password,
    count_sysops,
    create_user,
    delete_user,
    generate_challenge,
    get_user_by_username,
    list_users,
    set_user_disabled,
    set_user_level,
)

__all__ = [
    "SYSOP_LEVEL",
    "AuthError",
    "User",
    "UserManagementError",
    "authenticate_keypair",
    "authenticate_password",
    "count_sysops",
    "create_user",
    "delete_user",
    "generate_challenge",
    "get_user_by_username",
    "list_users",
    "set_user_disabled",
    "set_user_level",
]
