"""
NetBBS's own first-party doors (issue #172), shipped as real installed
package data -- the same mechanism `netbbs.net.banner_presets` already
uses for bundled welcome-banner/masthead samples (`pyproject.toml`'s
`[tool.setuptools.package-data]`), for the identical reason: `examples/`
is *not* part of the installed wheel at all
(`[tool.setuptools.packages.find]` is scoped to `src/` only), so a
SysOp on a real install had no loose example scripts on disk to point a
door registration at in the first place. These aren't examples of what
a door *could* be -- Voidrunner in particular is a genuinely complete,
persistent single-player game, more capable than most doors the
original DOS-BBS era ever shipped -- so they live here as first-class
product content NetBBS ships with, not as sample code in `examples/`.

Doors are still not *bundled execution* the way a banner's bytes are
bundled content: a door is a real subprocess NetBBS launches via
`netbbs.doors.runtime` (`asyncio.create_subprocess_exec`, argv-list
form, never a shell), which needs a real filesystem path to exec, not
just readable bytes -- so `resolve_bundled_door_path` resolves each
entry to an actual path on disk via `importlib.resources`, rather than
`banner_presets`' own `load_*_preset() -> bytes` shape. A SysOp's own,
separately-authored door remains exactly what it always was: an
external program pointed at by filesystem path, registered by hand --
nothing here changes that model, this only gives NetBBS's own doors the
same "really there on every install" property banners already have.

`netbbs.net.admin_flow`'s door `[G]allery` screen lists whichever of
these actually resolve on this install (see `available_bundled_doors`)
and registers one with these fields pre-filled -- still opening the
real create-door editor to review before saving, never auto-registering
on selection alone, since a door's `executable_path` genuinely varies
by node (see that screen's own docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class BundledDoor:
    key: str
    name: str
    description: str
    resource: str  # filename within this package
    suggested_min_play_level: int = 0
    #: The door re-reads its geometry from `NETBBS_DOOR_INFO` on `SIGUSR1`.
    #: `netbbs.doors.runtime` signals a door on the strength of this only when
    #: the script it launched *is* this install's copy, so the handler is there
    #: by construction -- the signal's default action would otherwise end the
    #: caller's game (issue #645).
    follows_resize: bool = False


BUNDLED_DOORS: tuple[BundledDoor, ...] = (
    BundledDoor(
        key="retro_trivia",
        name="Retro Trivia",
        description=(
            "Eight-question multiple-choice BBS trivia, single-keystroke A/B/C/D answers, "
            "a running score, and a colored final rank. Zero dependencies, session-scoped."
        ),
        resource="retro_trivia.py",
    ),
    BundledDoor(
        key="voidrunner",
        name="Voidrunner",
        description=(
            "Persistent single-player space trading and exploration: a seeded galaxy with "
            "fog-of-war, a drifting per-system market, raider encounters, a mission board, "
            "faction reputation, and shipyard upgrades. Saves progress per caller."
        ),
        resource="voidrunner.py",
        follows_resize=True,
    ),
    BundledDoor(
        key="war_dialer",
        name="War Dialer",
        description=(
            "Asynchronous, play-by-post multiplayer: rival 80s/90s BBS-scene crews fight over "
            "ten shared phone exchanges. Actions resolve instantly against a shared world; find "
            "out what happened to you while you were away on your next login. Four-week seasons."
        ),
        resource="war_dialer.py",
        follows_resize=True,
    ),
)


def resolve_bundled_door_path(door: BundledDoor) -> Path | None:
    """The absolute path to `door`'s script on this filesystem, or
    `None` if it isn't actually there -- an incomplete/corrupted
    install, not the normal case now that these ship as real package
    data (contrast the loose-`examples/`-file era this replaced, where
    a wheel install missing the file was expected, not exceptional)."""
    path = Path(str(resources.files(__package__) / door.resource))
    return path if path.is_file() else None


def launched_bundled_door(executable_path: str, args: tuple[str, ...], install_dir: str | None = None) -> BundledDoor | None:
    """The catalogue entry a registration actually launches, or `None`.

    A door is ours when one of its arguments resolves to this install's own copy
    of the script, or names it as a module. A copy of the script somewhere else
    is somebody's fork and is not matched: it may be any version.
    """
    install = Path(install_dir).resolve() if install_dir else None
    argv = (executable_path, *args)
    for door in BUNDLED_DOORS:
        module = __package__ + "." + door.resource.removesuffix(".py")
        if any(argv[i:i + 2] == ("-m", module) for i in range(len(argv) - 1)):
            return door
        bundled = resolve_bundled_door_path(door)
        if bundled is None:
            continue
        for index, arg in enumerate(argv):
            if index and install is not None:
                arg = arg.replace("{install_dir}", str(install))
            candidate = Path(arg)
            if candidate.name != door.resource:
                continue
            if not candidate.is_absolute():
                if install is None:
                    continue
                candidate = install / candidate
            try:
                if candidate.resolve() == bundled.resolve():
                    return door
            except OSError:
                continue
    return None


def available_bundled_doors() -> list[tuple[BundledDoor, Path]]:
    """Catalog entries whose script actually resolves on this
    filesystem, paired with their resolved absolute path -- empty only
    for a broken/incomplete install, not the normal case."""
    resolved = []
    for door in BUNDLED_DOORS:
        path = resolve_bundled_door_path(door)
        if path is not None:
            resolved.append((door, path))
    return resolved
