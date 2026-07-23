"""
Node-wide account presence: how many live sessions each account
currently has open, and whether they're currently marked away (design
doc).

Deliberately separate from `netbbs.chat.hub.ChatHub`: `ChatHub` tracks
per-*channel* participants (one small dict per channel); this tracks
per-*account* state that has nothing to do with which channel, or
whether the account is in a channel at all — a user simply logged in
and browsing boards is just as "online" as one actively chatting.

One `PresenceRegistry` instance per running node (constructed once,
e.g. in `netbbs.__main__` alongside `ChatHub`, and passed all the way
down through `netbbs.net.login_flow.handle_session`) — the shared
state that makes "is this account currently online" and "are they
away" answerable from anywhere in the node.
"""

from __future__ import annotations


class PresenceRegistry:
    def __init__(self) -> None:
        self._session_counts: dict[str, int] = {}
        self._away_messages: dict[str, str] = {}

    def enter(self, username: str) -> None:
        """Register one more live session for `username` — call once
        per successful login, for the duration of that connection."""
        self._session_counts[username] = self._session_counts.get(username, 0) + 1

    def leave(self, username: str) -> None:
        """
        One fewer live session for `username`. Clears away status the
        moment the account's *final* session disconnects (design doc:
        "clears only when the account's final session
        disconnects") — away status conceptually belongs to "being
        present at all", not to any one connection, so it shouldn't
        outlive every connection that could have set it.
        """
        remaining = self._session_counts.get(username, 0) - 1
        if remaining <= 0:
            self._session_counts.pop(username, None)
            self._away_messages.pop(username, None)
        else:
            self._session_counts[username] = remaining

    def is_online(self, username: str) -> bool:
        return self._session_counts.get(username, 0) > 0

    def online_usernames(self) -> set[str]:
        """Every currently-online account — used by `/msg`/`/private`'s
        Tab completion to suggest only
        reachable targets, distinct from `is_online`'s single-account
        check."""
        return set(self._session_counts)

    def set_away(self, username: str, message: str) -> None:
        """Mark `username` away, sharing the same status across every
        one of their active sessions (design doc: "shared
        across all active sessions")."""
        self._away_messages[username] = message

    def clear_away(self, username: str) -> None:
        self._away_messages.pop(username, None)

    def is_away(self, username: str) -> bool:
        return username in self._away_messages

    def get_away_message(self, username: str) -> str | None:
        """The away message (possibly empty, if none was given), or
        `None` if `username` isn't currently marked away at all."""
        return self._away_messages.get(username)
