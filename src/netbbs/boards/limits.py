"""
Board post size limits (GitHub issue #32), factored out on their own so
they have exactly one definition shared by two independent enforcement
points: local post creation/editing (`netbbs.boards.posts`) and Link
protocol validation of a received `board_post`/`board_post_edit`
(`netbbs.link.protocol`, issue #60's third operational slice).

Issue #79: those two enforcement points used to each hard-code their own
copy of the same two numbers, so a future change to one could silently
diverge from the other -- locally-created content valid on one node's
own board while a peer's Link receiver rejects the identical shape, or
the reverse. This module has zero other imports so `netbbs.link.protocol`
can depend on it without acquiring `netbbs.boards.posts`' database/
business-logic dependencies -- the "generic protocol code should not
import product modules" boundary is about behavior and storage, not
about two shared integers.

Counted in encoded UTF-8 bytes, not `len()` characters, since that's
what actually gets stored/transmitted/signed and multi-byte characters
would otherwise undercount. The exact numbers are a product choice, not
a correctness one: large enough that no legitimate post ever comes
close, small enough to bound worst-case memory/disk/DB-row/wire size to
something sane.
"""

from __future__ import annotations

MAX_SUBJECT_BYTES = 300
MAX_BODY_BYTES = 200_000
