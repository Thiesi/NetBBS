"""
What a caller is told while signing up and while their account waits for
approval (issue #835).

Shared by Telnet/web (`netbbs.net.login_flow`) and SSH
(`netbbs.net.ssh`), so both paths say the same thing. Its own module
because `ssh` must not import `login_flow`, which pulls in the whole
menu tree.
"""

from __future__ import annotations


def pending_approval_notice(username: str) -> str:
    """Told at signup and at every login until the account is approved.

    Plain words: a newcomer does not know what a SysOp is expected to do,
    nor that the account cannot be used to look around in the meantime.
    """
    return (
        f"Your account {username!r} is waiting for the SysOp's approval. "
        "The SysOp checks new accounts by hand; until yours is approved you can't log in "
        "or look around. Please call back later."
    )


def username_problem_line(problem: str) -> str:
    """`netbbs.auth.users.self_service_username_problem`'s reason as a
    sentence for the caller."""
    return f"{problem[0].upper()}{problem[1:]}. Please choose another."
