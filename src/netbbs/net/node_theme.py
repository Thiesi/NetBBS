"""
Lives in `netbbs.net`, not `netbbs.rendering`, matching where
`netbbs.net.color_depth_preference` (the closest precedent) already
lives -- `netbbs.rendering` stays a dependency-free foundational layer
with nothing in it importing `netbbs.net.session`, so a `Session`-aware
resolver belongs up here instead.

Node-wide identity-color overrides (GitHub issue #162) -- part three of
the skinning initiative `netbbs.net.welcome_banner`'s own module
docstring calls itself "part one" of (the main-menu masthead, issue
#161, is part two).

Deliberately narrow: only `ACCENT_COLOR`, `HEADER_COLOR`, and
`CLOCK_COLOR` -- the "branding" colors -- are ever overridable here.
Every semantic/status color in `netbbs.rendering.theme` (`ERROR_COLOR`,
`SUCCESS_COLOR`, `WARNING_COLOR`, `PRIVILEGE_COLOR`, `ALERT_COLOR`,
`VERIFIED_COLOR`) is intentionally absent from this module and always
stays exactly as `theme.py` defines it, everywhere -- a caller who has
used several NetBBS nodes can keep trusting "red always means failure,
green always means verified/success" regardless of what any node's
SysOp has branded their own node with. See issue #162's own "why not
full palette theming" section for the full reasoning; issue #163 tracks
that wider idea as considered-and-deferred.

Node-wide, not per-user, unlike `netbbs.net.color_depth_preference`
(the closest existing precedent for this module's own
override-wins-over-default shape): a SysOp sets these once for the
whole node, the same `node_config` scope `netbbs.net.welcome_banner`'s
own enabled flag already uses, not a per-account preference.

A SysOp supplies exactly one RGB triple per slot; `effective_*_color`
below derives the 256-color fallback automatically via
`netbbs.rendering.gradient.nearest_256` (the same downgrade
`gradient_text(..., truecolor=False)` already uses) for a session that
doesn't support truecolor, so nothing here ever needs a SysOp to pick
two numbers for one color.
"""

from __future__ import annotations

from netbbs.config import get_config, set_config
from netbbs.net.session import Session
from netbbs.rendering import ACCENT_COLOR, CLOCK_COLOR, HEADER_COLOR, nearest_256
from netbbs.rendering.gradient import GRADIENTS
from netbbs.storage.database import Database

_ACCENT_CONFIG_KEY = "theme_accent_rgb"
_HEADER_CONFIG_KEY = "theme_header_rgb"
_CLOCK_CONFIG_KEY = "theme_clock_rgb"
_NODE_NAME_GRADIENT_CONFIG_KEY = "node_name_gradient"


def _parse_rgb(value: str | None) -> tuple[int, int, int] | None:
    # "" is how `_set_color_override(rgb=None)` clears a slot below --
    # `netbbs.config` has no delete, only an overwrite -- so "" must
    # parse back to "no override" the same as a row that never existed.
    if not value:
        return None
    r, g, b = (int(part) for part in value.split(","))
    return (r, g, b)


def _color_override(db: Database, config_key: str) -> tuple[int, int, int] | None:
    return _parse_rgb(get_config(db, config_key))


def _set_color_override(db: Database, config_key: str, rgb: tuple[int, int, int] | None) -> None:
    if rgb is None:
        set_config(db, config_key, "")
        return
    r, g, b = rgb
    for name, value in (("r", r), ("g", g), ("b", b)):
        if not 0 <= value <= 255:
            raise ValueError(f"color override {name} must be 0-255, got {value}")
    set_config(db, config_key, f"{r},{g},{b}")


def _effective_color(
    session: Session, db: Database, config_key: str, default: int
) -> int | tuple[int, int, int]:
    override = _color_override(db, config_key)
    if override is None:
        return default
    return override if session.supports_truecolor else nearest_256(override)


# -- accent -----------------------------------------------------------


def accent_color_override(db: Database) -> tuple[int, int, int] | None:
    """`None` means no override -- every caller falls back to
    `theme.ACCENT_COLOR`, unchanged from today."""
    return _color_override(db, _ACCENT_CONFIG_KEY)


def set_accent_color_override(db: Database, rgb: tuple[int, int, int] | None) -> None:
    """`rgb=None` clears the override, reverting to `theme.ACCENT_COLOR`."""
    _set_color_override(db, _ACCENT_CONFIG_KEY, rgb)


def effective_accent_color(session: Session, db: Database) -> int | tuple[int, int, int]:
    """The color every accent-branded render call site (board/channel/
    user names, and every other spot `theme.ACCENT_COLOR` used to be
    used directly) should resolve through instead of importing the bare
    constant -- the SysOp's override RGB (downgraded to nearest-256 for
    a session without truecolor support) if set, `theme.ACCENT_COLOR`
    otherwise."""
    return _effective_color(session, db, _ACCENT_CONFIG_KEY, ACCENT_COLOR)


def effective_accent_color_256(db: Database) -> int:
    """Always the 256-color depth, ignoring any individual viewer's own
    truecolor support -- for a renderer that composes one shared string
    for many simultaneous recipients at once (`netbbs.net.chat_flow`'s
    broadcast message formatting, in particular), where no single
    per-session depth decision is even meaningful. NetBBS's live chat
    has never emitted truecolor at all (`chat_flow` never calls
    `gradient_text`); this keeps that a uniform property of the whole
    surface rather than carving out a partial truecolor exception just
    for the accent override."""
    override = accent_color_override(db)
    return ACCENT_COLOR if override is None else nearest_256(override)


# -- header -------------------------------------------------------------


def header_color_override(db: Database) -> tuple[int, int, int] | None:
    return _color_override(db, _HEADER_CONFIG_KEY)


def set_header_color_override(db: Database, rgb: tuple[int, int, int] | None) -> None:
    _set_color_override(db, _HEADER_CONFIG_KEY, rgb)


def effective_header_color(session: Session, db: Database) -> int | tuple[int, int, int]:
    return _effective_color(session, db, _HEADER_CONFIG_KEY, HEADER_COLOR)


def effective_header_color_256(db: Database) -> int:
    """Same reasoning as `effective_accent_color_256`: the `db`-only,
    always-256 variant real `screen_title`/`double_frame`/`empty_state`
    call sites actually thread through -- these are resolved inside
    synchronous `lane.run` closures alongside `accent_color`'s own
    identical call, which have `db` but not a `Session` in scope, and a
    screen composed once for a single session has no real reason to
    special-case truecolor for its own header rule beyond what the rest
    of that same screen already renders at."""
    override = header_color_override(db)
    return HEADER_COLOR if override is None else nearest_256(override)


# -- clock ----------------------------------------------------------------


def clock_color_override(db: Database) -> tuple[int, int, int] | None:
    return _color_override(db, _CLOCK_CONFIG_KEY)


def set_clock_color_override(db: Database, rgb: tuple[int, int, int] | None) -> None:
    _set_color_override(db, _CLOCK_CONFIG_KEY, rgb)


def effective_clock_color(session: Session, db: Database) -> int | tuple[int, int, int]:
    return _effective_color(session, db, _CLOCK_CONFIG_KEY, CLOCK_COLOR)


def effective_clock_color_256(db: Database) -> int:
    """Same `db`-only convenience as `effective_header_color_256` --
    `main_menu._main_menu_prompt`'s own single real call site resolves
    it via `lane.run`, matching the accent/header wiring's own shape."""
    override = clock_color_override(db)
    return CLOCK_COLOR if override is None else nearest_256(override)


# -- node-name gradient (GitHub issue #175) ----------------------------
#
# Deliberately not one of the three RGB "branding color" slots above --
# this doesn't override any single semantic color, it recolors one
# specific piece of text (the node name breadcrumb segment,
# `netbbs.net.session.Session.node_display_name`) with a per-character
# gradient the same way `netbbs.net.welcome_banner`'s wordmark already
# gets one. A preset name only for now (`netbbs.rendering.gradient.
# GRADIENTS`'s own keys), not a custom multi-stop RGB list -- issue
# #175's own suggested shape, kept simple until a real request for
# custom stops shows up.
#
# Resolved once per session, alongside `node_display_name` itself, in
# `netbbs.net.login_flow.run_authenticated_session` -- not re-fetched
# per screen via `lane.run` the way the three RGB slots above are.
# Those need a fresh per-screen `db` lookup because their own SysOp
# preview flow (`_set_theme_color_screen`) predates `node_display_name`
# ever being cached on `Session` at all; `node_display_name` itself
# established the "cache once at login, a SysOp's change takes effect
# for new connections, not sessions already in progress" trade-off
# first (`netbbs.config`'s own docstring on that field) and the
# gradient preference shares that exact value's lifecycle, so it
# follows that precedent instead of re-deriving the older one.


def node_name_gradient_override(db: Database) -> str | None:
    """`None` means no override -- the node name renders as plain
    `header_color`, unchanged from before this feature existed."""
    value = get_config(db, _NODE_NAME_GRADIENT_CONFIG_KEY)
    return value or None


def set_node_name_gradient_override(db: Database, gradient: str | None) -> None:
    """`gradient=None` clears the override, reverting to a flat node
    name. Otherwise must be one of `netbbs.rendering.gradient.
    GRADIENTS`'s own preset names."""
    if gradient is not None and gradient not in GRADIENTS:
        raise ValueError(f"unknown gradient {gradient!r}, expected one of {sorted(GRADIENTS)} or None")
    set_config(db, _NODE_NAME_GRADIENT_CONFIG_KEY, gradient or "")


def effective_node_name_gradient(db: Database) -> str | None:
    """The gradient preset every `screen_title(node_name_gradient=...)`
    call site should thread through -- the SysOp's override if set,
    `None` (flat `header_color`, today's behavior) otherwise. Always
    resolved once and cached on `Session.node_name_gradient`, not
    looked up fresh per screen -- see this module's own section
    docstring above."""
    return node_name_gradient_override(db)
