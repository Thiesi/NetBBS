"""
What a guest call keeps for itself, and throws away at hang-up (issue #1075).

Guest login (issue #531) signs every anonymous caller in to one shared
account. #1073 kept that account's profile out of a guest's reach and made
its display preferences last for the call
(`netbbs.user_preferences.session_scoped_preferences`). Two more things a
caller leaves behind are files rather than preferences:

- **Drafts.** Post and file-description drafts are files named after the
  account (`netbbs.net.draft_storage`), so on the shared account guest B
  would be offered guest A's unfinished post, and two guests at once would
  write the same file. A guest call keeps its drafts in its own directory
  instead (`drafts_directory` asks `current_guest_call`).
- **Door saves.** The bundled doors key their saves on the drop file's
  `user_id`, so every guest shared one Voidrunner career and one War Dialer
  crew, and Voidrunner's Hall of Fame showed whatever callsign the last guest
  typed. A guest call plays them under `door_user_id`, a number no account
  can have, with saves in this directory (`netbbs.doors.runtime.run_door`).

The directory is made at sign-in and deleted when the call ends, whichever
way it ends. Held in a context variable for the same reason the preference
overlay is: `drafts_directory(db)` is reached from call sites with no session
to hand. `netbbs.net.login_flow.run_authenticated_session` enters it, in the
session's own task, for a session that signed in without a credential.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

# Above every rowid a node will ever hand out, below the 2**63 - 1 ceiling the
# bundled doors accept: a guest call's door identity can never be an account's.
_DOOR_USER_ID_FLOOR = 2**62


@dataclass(frozen=True)
class GuestCall:
    """One guest call's private directory and door identity."""

    directory: Path
    door_user_id: int

    def subdirectory(self, name: str) -> Path:
        path = self.directory / name
        path.mkdir(parents=True, exist_ok=True)
        return path


_current: ContextVar[GuestCall | None] = ContextVar("netbbs_guest_call", default=None)


def new_guest_call() -> GuestCall:
    """A fresh directory and door identity; `discard_guest_call` removes it."""
    directory = Path(tempfile.mkdtemp(prefix="netbbs-guest-call-"))
    return GuestCall(directory=directory, door_user_id=_DOOR_USER_ID_FLOOR + secrets.randbelow(_DOOR_USER_ID_FLOOR))


def discard_guest_call(call: GuestCall) -> None:
    shutil.rmtree(call.directory, ignore_errors=True)
    if call.directory.exists():
        _logger.warning("could not remove guest call directory %s", call.directory)


@contextmanager
def guest_call() -> Iterator[GuestCall]:
    """Within this block the current context is a guest call: its drafts and
    bundled-door saves live in the call's own directory, deleted on the way
    out."""
    call = new_guest_call()
    token = _current.set(call)
    try:
        yield call
    finally:
        _current.reset(token)
        discard_guest_call(call)


def current_guest_call() -> GuestCall | None:
    return _current.get()
