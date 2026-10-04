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


# -- what this call wrote (issue #1075) ---------------------------------------
#
# Every guest's post and upload carries the one shared account as its author,
# so "your own post" would otherwise mean every post any guest ever made: any
# guest could edit or withdraw an earlier guest's words, and the edit would
# be carried over the Link as the author's. A session that signed in without
# a credential may change only what it created during this call; the rest of
# what the account wrote is read-only to it, and says why.


def note_created_this_call(session: object, kind: str, key: object) -> None:
    """Record that this session created `key` (a post's `root_post_id`, a
    file's row id) -- a no-op for an ordinary session."""
    if not signed_in_without_credential(session):
        return
    created = getattr(session, "created_this_call", None)
    if created is None:
        created = set()
        session.created_this_call = created
    created.add((kind, key))


def authored_earlier_by_shared_account(session: object, kind: str, key: object) -> bool:
    """Whether the account's authorship of `key` is not this session's: it
    signed in without a credential, and did not create `key` during this
    call. The caller has already established that the account is the
    author."""
    if not signed_in_without_credential(session):
        return False
    return (kind, key) not in getattr(session, "created_this_call", set())


def earlier_guest_refusal(what: str) -> str:
    """The reason given on screen for `what` ("this post", "this file")
    that the shared account wrote before this call."""
    return (
        f"This session signed in without a password, so it can change only what it wrote during this call. "
        f"Every guest signs in to this same account, and {what} is from before this call."
    )
