"""Capture Lunar Lander from a published release, with its own renderer and game.

Download lunar_lander.py from a tagged release before running this helper. No
game file is vendored or modified here. The source must match one of the
release digests in `PINS`, copied from that release's SHA256SUMS; anything
else is refused rather than silently captured from an unreleased checkout.
Like the upstream capture tool, every frame is drawn through Display.draw and
Display.encode, on a fixed date.

Two kinds of screen:

* `splash`, `briefing` and `flying` draw one frame of the first mission
  directly, the flight advanced with real controls. Any pinned release.
* `approach`, `touchdown`, `debrief` and `scores` play the release's own
  `Game` -- the object its door loop drives -- against a throwaway pilot
  records database. The game is given only keys and elapsed time. A small
  autopilot in this script picks the keys (throttle digits, tilt, upright),
  so a landing is flown by the game's own physics, saved by its own records
  code and scored by its own rules. `scores` first flies the same mission
  for five fixture pilots, each with a differently tuned autopilot, so the
  best-pilots board has rows; they are made-up pilots, and the website
  labels them so. Releases with a `Game` class only (v0.1.0 onwards).

To capture a new release, add its tag and its lunar_lander.py line from
SHA256SUMS to `PINS`.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import math
from pathlib import Path
import sys
import tempfile

# Release tag -> SHA-256 of its published lunar_lander.py (from SHA256SUMS).
PINS = {
    "v0.1.0-rc.3": "75efa163b3cdadc5b8c56881460a1adea313e5e51f8c455589d623ed73aa3c78",
    "v0.1.0": "6aacc05f20d99fb35027bc04f050523bd4bedac5f47f0f24217da8ffc5d2bb71",
}
DAY = "2026-10-24"
MISSION = "tranquility"   # a ranked mission over a broad pad, approached with drift
NODE, HANDLE = "harbor-lights", "nova"
# Fixture pilots for the best-pilots board, and how their autopilots differ:
# (handle, descent rate in m/s per metre of altitude, final descent rate).
FIXTURE_PILOTS = (("Kestrel", .25, 1.0), ("Okafor", .12, 1.5), ("Lindqvist", .08, 2.0),
                  ("Moth", .10, 2.6), ("Sable", .08, 2.6))
CAPTAIN = (.18, 1.0)      # nova's own autopilot: a gold landing, one place behind Kestrel
SCREENS = ("splash", "briefing", "flying", "approach", "touchdown", "debrief", "scores")
TOUCHDOWN_AGE = 1.4       # seconds into the landing scene: dust settling, flag up


def load(source: Path, tag: str | None):
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    found = next((name for name, pin in PINS.items() if pin == digest), None)
    if found is None:
        raise SystemExit(f"{source} matches no pinned release digest; known: {', '.join(PINS)}")
    if tag and tag != found:
        raise SystemExit(f"{source} is {found}'s lunar_lander.py, not {tag}'s")
    spec = importlib.util.spec_from_file_location(f"website_lunar_lander_{found}", source)
    assert spec and spec.loader
    game = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = game
    spec.loader.exec_module(game)
    return found, game


def first_mission_frame(game, screen: str) -> str:
    """The original captures: one frame of the day's first mission."""
    display = game.Display(game.Host(80, 24, "truecolor", True, handle=HANDLE))
    catalog = game.missions(DAY)
    flight = catalog[0].launch()
    mode = {"splash": "splash", "briefing": "menu", "flying": "flying"}[screen]
    if screen == "flying":
        flight.command("6")
        flight.command("a")
        for tick in range(160):  # eight seconds of real simulated flight
            if tick == 40:
                flight.command("x")
            flight.step(0.05)
        if flight.status != "flying" or flight.fuel >= flight.initial_fuel:
            raise SystemExit("the capture flight did not reach the intended in-flight state")
    canvas = display.draw(flight, mode, mission=catalog[0], catalog=catalog,
                          scene_age=game.SCENE_SECONDS["splash"],
                          record_note="Training flight / unranked",
                          clock_note="Daily reset: UTC")
    return display.encode(canvas)


class Pilot:
    """Keys for one flight, from what the instruments would show."""

    def __init__(self, game, rate: float, final: float):
        self.game, self.rate, self.final = game, rate, final

    def keys(self, flight) -> list[str]:
        g = self.game
        pad = flight.terrain.pad
        centre = (pad.left + pad.right) / 2
        altitude = flight.y - 3 - pad.height
        # Lean against the drift and toward the pad; stand upright to land.
        want_vx = max(-4.0, min(4.0, (centre - flight.x) * .04))
        if abs(centre - flight.x) < (pad.right - pad.left) / 2 - 12:
            want_vx = 0.0  # over the pad already: just stop the drift
        lean = max(-30.0, min(30.0, (want_vx - flight.vx) * 25))
        if altitude < 6:
            lean = 0.0
        keys = []
        if lean - flight.angle >= 10:
            keys.append("d")
        elif flight.angle - lean >= 10:
            keys.append("a")
        if altitude < 6 and flight.angle:
            keys = ["x"]
        # Throttle for the descent rate this altitude allows.
        want_vy = -max(self.final, min(9.0, altitude * self.rate))
        mass = g.DRY_MASS + flight.fuel
        need = (flight.gravity + (want_vy - flight.vy) * 1.5) * mass / g.MAX_THRUST
        need /= max(.5, math.cos(math.radians(flight.angle)))
        digit = max(0, min(9, round(need * 9)))
        keys.append(str(digit))
        return keys


def play(game_module, records, user: int, handle: str, pilot: tuple[float, float],
         stop=None):
    """Launch MISSION in the release's Game and fly it; returns the game.

    `stop(game)` may end the flight early, for a mid-flight photograph.
    """
    g = game_module
    host = g.Host(80, 24, "truecolor", True, handle=handle, node=NODE, user=user)
    clock = g.DailyClock("UTC", now=lambda: datetime.fromisoformat(DAY + "T20:00:00+00:00"))
    game = g.Game(host, records, clock=clock, scenes=True)
    game.record_note = game.ranking_note()
    for _ in range(4 * len(g.CHAPTERS) * 8):
        if game.mission.key == MISSION:
            break
        game.key("d" if not any(m.key == MISSION for m in game.shelf()) else "s")
    else:
        raise SystemExit(f"{MISSION} is not on the flight director")
    game.key("enter")
    autopilot = Pilot(g, *pilot)
    for _ in range(4000):
        if game.mode == "launch":
            game.tick(.1, True)
            continue
        if game.mode != "flying":
            break
        if stop is not None and stop(game):
            return game
        for key in autopilot.keys(game.flight):
            game.key(key)
        game.tick(.05, True)
    return game


def deck(g, game):
    return g.Deck(game.chapter, game.bests, game.records is not None and game.host.ranked, game.award)


def draw(g, game) -> str:
    display = g.Display(game.host)
    canvas = display.draw(game.flight, game.mode, None, False, game.mission, game.standings,
                          game.record_note, game.scene_age, "DAILY RESET: midnight / UTC",
                          game.missions, deck(g, game))
    return display.encode(canvas)


def played_frame(g, screen: str) -> str:
    if not hasattr(g, "Game"):
        raise SystemExit(f"{screen} plays the release's Game class, which this release lacks")
    folder = Path(tempfile.mkdtemp(prefix="website-lunar-lander-"))
    records = g.Records(folder / "pilots.db")
    try:
        if screen == "scores":
            for user, (handle, rate, final) in enumerate(FIXTURE_PILOTS, start=2):
                flown = play(g, records, user, handle, (rate, final))
                if flown.flight.status != "landed":
                    raise SystemExit(f"fixture pilot {handle} did not land: {flown.flight.reason}")
        if screen == "approach":
            def low(game):
                pad = game.flight.terrain.pad
                over = pad.left <= game.flight.x <= pad.right
                return over and game.flight.y - 3 - pad.height < 30 and game.flight.throttle > 0
            game = play(g, records, 1, HANDLE, CAPTAIN, stop=low)
            if game.mode != "flying":
                raise SystemExit(f"the approach ended before the photograph: {game.flight.reason}")
            return draw(g, game)
        game = play(g, records, 1, HANDLE, CAPTAIN)
        if game.flight.status != "landed":
            raise SystemExit(f"the capture flight did not land: {game.flight.reason}")
        if screen == "touchdown":
            if game.mode != "landing":
                raise SystemExit(f"no landing scene after touchdown: {game.mode}")
            game.tick(TOUCHDOWN_AGE, True)
            return draw(g, game)
        for _ in range(200):  # the medal and promotion scenes play out by themselves
            if game.mode not in g.SCENE_SECONDS:
                break
            game.tick(.1, True)
        if game.mode != "debrief":
            raise SystemExit(f"the landing did not end on the debrief: {game.mode}")
        if screen == "scores":
            game.key("m")
            game.key("b")
            if game.mode != "scores" or len(game.standings) < 2:
                raise SystemExit("the best-pilots board did not open with rows")
        return draw(g, game)
    finally:
        records.close()
        for path in sorted(folder.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        folder.rmdir()


def capture(source: Path, screen: str, tag: str | None = None) -> tuple[str, str]:
    found, game = load(source, tag)
    if screen in ("splash", "briefing", "flying"):
        return found, first_mission_frame(game, screen)
    return found, played_frame(game, screen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("screen", choices=SCREENS)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path, required=True,
                        help="lunar_lander.py downloaded from a release named in PINS")
    parser.add_argument("--tag", choices=sorted(PINS),
                        help="refuse a source that is not this release's")
    args = parser.parse_args()
    found, body = capture(args.source, args.screen, args.tag)
    args.output.write_bytes(body.encode("utf-8"))
    print(f"wrote {args.output} ({found}, 80x24)")


if __name__ == "__main__":
    main()
