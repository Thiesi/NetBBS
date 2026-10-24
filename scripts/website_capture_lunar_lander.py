"""Capture Lunar Lander RC.3 with its published renderer and flight model.

Download lunar_lander.py from the tagged release before running this helper.
No game file is vendored or modified here. The default digest is the release's
SHA256SUMS entry; refuse a different source rather than silently recapturing
from an unreleased checkout. Like the upstream capture tool, draw through
Display.draw/encode, with a fixed date and a flight advanced using real controls.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
from pathlib import Path
import sys


TAG = "v0.1.0-rc.3"
SHA256 = "75efa163b3cdadc5b8c56881460a1adea313e5e51f8c455589d623ed73aa3c78"


def capture(source: Path, screen: str) -> str:
    if hashlib.sha256(source.read_bytes()).hexdigest() != SHA256:
        raise SystemExit(f"source does not match {TAG}'s published SHA256SUMS")
    spec = importlib.util.spec_from_file_location("website_lunar_lander_rc3", source)
    assert spec and spec.loader
    game = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = game
    spec.loader.exec_module(game)
    display = game.Display(game.Host(80, 24, "truecolor", True, handle="nova"))
    catalog = game.missions("2026-10-24")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("screen", choices=("splash", "briefing", "flying"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path, required=True,
                        help=f"lunar_lander.py downloaded from {TAG}")
    args = parser.parse_args()
    args.output.write_bytes(capture(args.source, args.screen).encode("utf-8"))
    print(f"wrote {args.output} ({TAG}, 80x24)")


if __name__ == "__main__":
    main()
