"""
FidoNet-technology networks (design doc §6.8, issue #166).

The gateway that carries FTN echomail on boards and netmail in Mail. This
package's lowest layer is pure and synchronous: addresses (`address`),
packets (`packet`), message text with its kludges and control lines
(`message`), character sets (`chrs`) and bundles (`bundle`). Nothing in
it touches the database or the network, so every byte format can be
tested on its own.

Everything read from a packet is remotely influenced. Parsers bound what
they accept and raise `FtnFormatError` rather than guessing at a malformed
structure; text is decoded but not sanitized here -- display paths
sanitize it, as for any external content.
"""

from __future__ import annotations


class FtnFormatError(ValueError):
    """Bytes that are not a valid FTN structure, or exceed a bound."""
