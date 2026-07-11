"""
Local user account management: registration, password auth, keypair auth.

Design doc §5: both password auth (simple/fallback) and keypair-based
(passwordless) auth are supported, and either may be used alone.
"""

from netbbs.auth.users import AuthError, User, authenticate_keypair, authenticate_password, create_user, generate_challenge, get_user_by_username

__all__ = [
    "AuthError",
    "User",
    "authenticate_keypair",
    "authenticate_password",
    "create_user",
    "generate_challenge",
    "get_user_by_username",
]
