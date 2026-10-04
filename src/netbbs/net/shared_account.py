"""
What a session that signed in without a credential may not change about
its account (issue #1073).

Guest login (issue #531) puts every anonymous caller on one account. The
password and key screens refuse such a session because it proved no
credential. The same goes for everything other callers see of the account
or that a node-wide service reads from it -- its bio, signature, name and
details, chat alias, who it blocks, its MRC hub registration: one guest
changing them changes them for every caller and every later guest.

Whether a session got in that way is the session's to say
(`authenticated_without_credential`, set by the guest branch of
`netbbs.net.login_flow._login`), not the account's: the guest account
signed in with its own password is an ordinary account and changes all of
this as before. Display settings stay open to a guest and last for the
call (`netbbs.user_preferences.session_scoped_preferences`).
"""

from __future__ import annotations


def signed_in_without_credential(session: object) -> bool:
    return bool(getattr(session, "authenticated_without_credential", False))


def shared_account_refusal(what: str) -> str:
    """The reason given on screen, `what` naming the thing refused
    ("the bio", "your chat alias")."""
    return (
        f"This session signed in without a password, so it cannot change {what}. "
        "Every guest signs in to this same account."
    )
