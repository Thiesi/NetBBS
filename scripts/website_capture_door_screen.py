"""Capture one screen of a bundled door for the website gallery.

The other `website_capture_*` scripts drive NetBBS itself in-process. A door
cannot be driven that way: it is a subprocess with its own terminal, and the
rule the gallery exists to enforce is that a door is *typed at* one key at a
time rather than piped into. So this reuses `door_gallery`'s machinery --
the same cached fixture, the same walk keys, the same `capture` -- and
writes the raw ANSI of one walk instead of a page of panels.

Two things it inherits that matter for a screenshot:

* **The career has wear on it.** A fresh career draws every gauge full or
  empty, which is the least informative state a gauge can be in. The
  Voidrunner walk here names the `played` fixture: a career with a fight
  behind it, so the hull, the hold and the record all show something.
* **The walk is the gallery's own.** A screen the website shows is a screen
  the presentation review already covers, named by the same label, so the
  two cannot drift apart without one of them failing.

    python scripts/website_capture_door_screen.py voidrunner "Command Deck" \
        web/shots/raw-voidrunner.txt

`--list` names the walks a door offers.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import tempfile

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import door_gallery as gallery  # noqa: E402


def capture_walk(door_name: str, label: str, width: int, height: int,
                 page: int, preset: str, fresh: bool, fixture_override=None,
                 setup_keys: str = "") -> str:
    door = ROOT / "src/netbbs/doors/bundled" / f"{door_name}.py"
    if not door.exists():
        raise SystemExit(f"no such bundled door: {door}")

    walks = {walk[0]: (walk + ("base",))[:3] for walk in gallery.WALKS[door_name]}
    if label not in walks:
        raise SystemExit(f"{door_name} has no walk {label!r}; "
                         f"try --list")
    _label, keys, fixture_name = walks[label]
    # A walk's own fixture is the cheapest one that reaches its screen, which
    # for the deck is a career with nothing on it yet. A screenshot wants the
    # opposite: gauges that are partly full, a hold with something in it, a
    # record with a fight behind it.
    fixture_name = fixture_override or fixture_name

    extra = gallery.PRESETS[door_name][preset]
    cache = ROOT / "build" / "gallery"
    cache.mkdir(parents=True, exist_ok=True)
    fixture = gallery.base_fixture(door_name, door, cache, fresh, fixture_name)

    work = pathlib.Path(tempfile.mkdtemp(prefix="website-door-"))
    try:
        state = work / "state"
        if label in gallery.FRESH.get(door_name, ()):
            state.mkdir(parents=True)
            gallery.blank_world(door, state)
        else:
            shutil.copytree(fixture, state)
        info_extra = gallery.apply_preset(state, extra)
        prepare = gallery.PREPARED.get(door_name, {}).get(label)
        if prepare:
            prepare(door, state)
        setup = gallery.SETUP.get(door_name, {}).get(label)
        if setup_keys:
            # Played in a launch of its own, whose screens are thrown away:
            # this is how a world gets wear the cached fixture does not have,
            # by playing the door rather than by writing its tables.
            setup = (setup or b"") + setup_keys.encode()
        if setup:
            gallery.capture(door, state, setup, 80, 24, info_extra)
        pages = gallery.capture(door, state, keys, width, height, info_extra)
    finally:
        gallery.remove(work)

    if not 1 <= page <= len(pages):
        raise SystemExit(f"{label} ends on {len(pages)} page(s); asked for {page}")
    return pages[page - 1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("door", choices=sorted(gallery.WALKS))
    parser.add_argument("walk", nargs="?", help="the walk's label in door_gallery.WALKS")
    parser.add_argument("output", nargs="?", type=pathlib.Path,
                        help="raw ANSI capture to write")
    parser.add_argument("--list", action="store_true", help="name the door's walks")
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=24)
    parser.add_argument("--page", type=int, default=1,
                        help="which page, for a walk that ends on a card stack")
    parser.add_argument("--preset", default="auto")
    parser.add_argument("--fixture", help="play the walk against another fixture")
    parser.add_argument("--setup", default="",
                        help="keys played first, in a launch whose screens are discarded")
    parser.add_argument("--fresh", action="store_true",
                        help="rebuild the cached fixture first")
    args = parser.parse_args()

    if args.list:
        for walk in gallery.WALKS[args.door]:
            print(walk[0])
        return
    if not args.walk or not args.output:
        parser.error("a walk and an output path are required")

    screen = capture_walk(args.door, args.walk, args.width, args.height,
                          args.page, args.preset, args.fresh, args.fixture,
                          args.setup)
    # Bytes, not text: `write_text` turns the door's CR LF into CR CR LF on
    # Windows, which renders as a blank line between every terminal row.
    args.output.write_bytes(screen.encode("utf-8"))
    rows = screen.count("\n") + 1
    print(f"wrote {args.output} ({rows} rows)")


if __name__ == "__main__":
    main()
