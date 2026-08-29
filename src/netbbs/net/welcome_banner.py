"""
The node's login/welcome banner (design doc -- welcome banner), part of
a broader skinning initiative; deliberately independent of the larger
TUI/editor context (see design doc for that).

A SysOp who wants custom ANSI art at login places a `.ans` file
directly on the node's filesystem, at the well-known path this module
resolves (`banner_path`), then enables it via `netbbs.net.admin_flow`'s
`[W]elcome banner` screen. There is no in-BBS upload mechanism --
the file is authored externally (a normal ANSI-art-scene tool,
or a download) and placed on the node the same way its SSH host key
already is: colocated with the database file
(`netbbs.net.ssh.ensure_host_key`'s established pattern), not stored
inside SQLite (`node_config` stays reserved for small string settings)
and not routed through the content-addressed file-area storage (that
scheme exists for many uploaded files; this is a single node-wide
singleton).

`load_welcome_banner` is the login-time hot path, called on every
single connection -- every failure mode it can encounter (missing
file, oversized file, unreadable file) falls back to
`DEFAULT_WELCOME_BANNER` silently rather than ever raising or showing
a raw error to an anonymous pre-auth session. It deliberately never
calls `netbbs.rendering.reflow` (would destroy fixed-width ANSI art
alignment -- real art is authored at a fixed width, 80 columns being
the classic BBS standard) or `netbbs.rendering.sanitize_text` (would
strip the art's own ESC sequences -- see `netbbs.rendering.ansi_art`'s
module docstring for why this content is trusted, SysOp-authored
content at the same tier as `colored()` output, not something to
sanitize).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from netbbs.config import get_config, set_config
from netbbs.net.node_theme import accent_color_override, header_color_override
from netbbs.rendering import (
    ACCENT_COLOR,
    HEADER_COLOR,
    RESET,
    colored,
    decode_ansi_bytes,
    gradient_color,
    gradient_text,
    nearest_256,
)
from netbbs.rendering.layout import double_frame
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

# Style spec (round following the pre-5.0.0 "beautify" audit): the
# double-line frame is NetBBS's one standard panel style, not gated
# behind a per-account `unicode_style` check here -- this banner is
# shown pre-login, to anonymous connections with no account/preference
# to look up yet, and (see `unicode_style_preference`'s own docstring)
# NetBBS's Telnet transport already sends every screen as UTF-8
# unconditionally regardless of any preference, so there's no separate
# "safe ASCII" pre-login mode to fall back to here in the first place.
# issue #162's node-wide accent-color override applies here too (this
# reads `db`, not a `User`, so it works fine pre-auth) -- factored into
# a builder, `_build_default_banner_256`, rather than kept as a single
# hardcoded literal, so `_default_welcome_banner` can rebuild it with a
# SysOp's own override substituted in. `DEFAULT_WELCOME_BANNER` itself
# stays a precomputed constant at the *unoverridden* ACCENT_COLOR -- the
# fast path for the overwhelming majority of nodes with nothing
# configured, and the exact value many existing tests still compare
# against.
def _build_default_banner_256(
    accent: int | tuple[int, int, int], header: int | tuple[int, int, int] = HEADER_COLOR
) -> str:
    return (
        double_frame(
            [
                "",
                " " * 21 + gradient_text("N E T B B S", "rainbow", bold=True, truecolor=False),
                colored(" " * 7 + "conversations across independent nodes", fg_color=header, bold=True),
                "",
            ],
            width=58,
            header_color=header,
        )
        + "\r\n"
        + colored("  NetBBS Link", fg_color=accent, bold=True)
        + colored("  ›  private experimental federation", fg_color=header, bold=True)
    )


DEFAULT_WELCOME_BANNER = _build_default_banner_256(ACCENT_COLOR)

# The truecolor variant's own subtitle/header used hand-picked RGB
# approximations of ACCENT_COLOR/HEADER_COLOR (xterm 220 gold, xterm 51
# cyan) rather than the 256-indices themselves, since truecolor spans
# need a real RGB triple -- kept as the unoverridden defaults an
# accent/header override replaces below.
_DEFAULT_ACCENT_RGB = (255, 215, 0)
_DEFAULT_HEADER_RGB = (0, 255, 255)


def _default_welcome_banner(db: Database, *, truecolor: bool) -> str:
    """`DEFAULT_WELCOME_BANNER` stays a static, precomputed constant for
    the common unoverridden case -- correct for every client, and cheap
    to return directly -- since most of what varies here (the wordmark/
    border gradient) can't depend on a per-session flag that doesn't
    exist at import time anyway; it already gradients its own wordmark/
    subtitle at the safe 256-color depth every client is assumed to
    support (`gradient_text(..., truecolor=False)`), same as this
    function's own truecolor variant, just quantized. A SysOp's own
    accent-color override (issue #162), if set, is substituted into
    either depth's "NetBBS Link" span -- rebuilt via
    `_build_default_banner_256` for the 256 case, resolved to a real RGB
    triple directly for the truecolor case. This builds the *truecolor*
    variant per-call: the same "rainbow" wordmark plus a full-width
    gradiented border, composed alongside the surrounding flat-
    `HEADER_COLOR` blank/tagline lines via concatenated `colored()`/
    `gradient_text()` spans -- the same "one colored() call per span,
    concatenated" shape `netbbs.rendering.reflow.colored_truncate`
    already uses, just assembled by hand here since this banner isn't a
    segment list."""
    accent_override = accent_color_override(db)
    header_override = header_color_override(db)
    if not truecolor:
        if accent_override is None and header_override is None:
            return DEFAULT_WELCOME_BANNER
        accent = nearest_256(accent_override) if accent_override is not None else ACCENT_COLOR
        header = nearest_256(header_override) if header_override is not None else HEADER_COLOR
        return _build_default_banner_256(accent, header)
    # The full-width border makes negotiated truecolor unmistakable at a
    # glance instead of confining the showcase to six subtly shaded letters.
    header = header_override or _DEFAULT_HEADER_RGB
    border_text = "╔══════════════════════════════════════════════════════╗"
    border = gradient_text(border_text, "rainbow", bold=True, truecolor=True)
    bottom_border = gradient_text(
        border_text.replace("╔", "╚").replace("╗", "╝"), "rainbow", bold=True, truecolor=True
    )
    wordmark = gradient_text("N E T B B S", "rainbow", bold=True, truecolor=True)

    # Dogfood report: the top/bottom borders and wordmark already carry
    # the rainbow theme, but the "║" bars down each side of every row in
    # between stayed flat HEADER_COLOR (cyan) -- a single box-drawing
    # character has no internal width for gradient_text's own per-
    # character gradient to run across. Dogfood follow-up correction: an
    # earlier version of this fix swept the bar color top-to-bottom by
    # row instead, which wasn't what was actually being asked for -- the
    # visible effect callers actually see is the *horizontal* rainbow
    # culminating in red on the left edge and purple on the right, so
    # each side's own bar takes that same edge's endpoint color (t=0.0
    # for the left column, t=1.0 for the right) and holds it for every
    # row, rather than varying per row -- a vertical continuation of
    # each side's own corner, not an independent top-to-bottom sweep.
    # Always on regardless of a SysOp's own header-color override,
    # matching the border/wordmark's own established "always rainbow"
    # precedent right next to it (the flat `header` color below still
    # governs everything else on these rows -- the padding and text,
    # never the borders themselves).
    left_bar = colored("║", fg_color=gradient_color("rainbow", 0.0, truecolor=True), bold=True)
    right_bar = colored("║", fg_color=gradient_color("rainbow", 1.0, truecolor=True), bold=True)

    welcome_line = (
        left_bar + colored("                      ", fg_color=header, bold=True)
        + wordmark
        + colored("                     ", fg_color=header, bold=True) + right_bar
    )
    blank_top = left_bar + colored(" " * 54, fg_color=header, bold=True) + right_bar
    tagline = (
        left_bar
        + colored("        conversations across independent nodes        ", fg_color=header, bold=True)
        + right_bar
    )
    blank_bottom = left_bar + colored(" " * 54, fg_color=header, bold=True) + right_bar
    subtitle = (
        colored("  NetBBS Link", fg_color=accent_override or _DEFAULT_ACCENT_RGB, bold=True)
        + colored("  ›  private experimental federation", fg_color=header, bold=True)
    )
    return "\r\n".join([border, blank_top, welcome_line, tagline, blank_bottom, bottom_border, subtitle])

# Comfortably covers realistic ANSI art (typically a few KB, rarely
# above ~150 KB even for elaborate multi-panel pieces) while bounding a
# SysOp accidentally pointing the path at something pathological. Not
# admin-configurable.
MAX_BANNER_SIZE_BYTES = 262_144  # 256 KiB

_WELCOME_BANNER_ENABLED_CONFIG_KEY = "welcome_banner_enabled"


def is_welcome_banner_enabled(db: Database) -> bool:
    return get_config(db, _WELCOME_BANNER_ENABLED_CONFIG_KEY) == "1"


def set_welcome_banner_enabled(db: Database, enabled: bool) -> None:
    set_config(db, _WELCOME_BANNER_ENABLED_CONFIG_KEY, "1" if enabled else "0")


def banner_path(db: Database) -> Path:
    """The well-known path a custom banner file must be placed at,
    colocated with the database file. Deliberately does not
    auto-create anything (unlike `netbbs.net.ssh.ensure_host_key`) --
    a missing file is normal, expected state here, not an error to
    paper over.

    `.resolve()` matters here: `db.path` is commonly a relative default
    (e.g. `Path("netbbs.db")`), and joining a relative `.parent` of "."
    collapses away entirely, leaving a bare filename with no directory
    at all -- exactly the "which folder?" a SysOp reading this path
    off a help screen from a different shell session needs answered
    (dogfood report)."""
    return (db.path.parent / f"{db.path.stem}_welcome_banner.ans").resolve()


@dataclass(frozen=True)
class WelcomeBannerStatus:
    enabled: bool
    path: Path
    exists: bool
    size_bytes: int | None


def welcome_banner_status(db: Database) -> WelcomeBannerStatus:
    """Cheap, `stat()`-based introspection for the admin screens --
    never reads the file's actual content."""
    path = banner_path(db)
    exists = path.exists()
    size_bytes = path.stat().st_size if exists else None
    return WelcomeBannerStatus(
        enabled=is_welcome_banner_enabled(db), path=path, exists=exists, size_bytes=size_bytes
    )


def load_welcome_banner(db: Database, *, truecolor: bool = False) -> str:
    """
    Resolve the banner to show at login: the SysOp's custom file if
    enabled and usable, the default banner otherwise. Synchronous
    -- matches existing precedent (`netbbs.config.get_config`,
    `netbbs.net.ssh.ensure_host_key`) of plain blocking local disk/DB
    calls made directly from async functions; a sub-256KB read isn't
    worth `asyncio.to_thread`.

    `truecolor` (default `False`, the safe universal choice) selects
    whether the *default* banner's "NetBBS" name is rendered with a
    truecolor gradient (`_default_welcome_banner`) -- callers should
    pass their session's negotiated/effective truecolor support (see
    `netbbs.net.session.Session.supports_truecolor`). Has no effect on
    the SysOp's custom `.ans` file path: that content is trusted,
    already-composed art, shown exactly as authored regardless of this
    flag.

    Every fallback here is silent to the connecting user (never show a
    raw error to an anonymous pre-auth session -- every visitor would
    see it) but logged server-side at WARNING level so a SysOp can
    diagnose a vanished/oversized/unreadable file after enabling it.
    `netbbs.net.admin_flow`'s `[E]nable` screen already checks for
    these conditions proactively before allowing enable, so they
    shouldn't normally arise here -- but this function must defend
    against them independently anyway, since it runs unattended on
    every login regardless of how the flag got set.
    """
    if not is_welcome_banner_enabled(db):
        return _default_welcome_banner(db, truecolor=truecolor)

    path = banner_path(db)
    if not path.exists():
        _logger.warning("welcome banner enabled but missing at %s -- using default", path)
        return _default_welcome_banner(db, truecolor=truecolor)

    try:
        size = path.stat().st_size
        if size > MAX_BANNER_SIZE_BYTES:
            _logger.warning(
                "welcome banner at %s is %d bytes, over the %d byte limit -- using default",
                path, size, MAX_BANNER_SIZE_BYTES,
            )
            return _default_welcome_banner(db, truecolor=truecolor)
        data = path.read_bytes()
    except OSError:
        _logger.warning("could not read welcome banner at %s -- using default", path, exc_info=True)
        return _default_welcome_banner(db, truecolor=truecolor)

    # decode_ansi_bytes cannot raise (see its own docstring) -- no
    # decode-failure fallback is needed here, by construction.
    return decode_ansi_bytes(data) + RESET
