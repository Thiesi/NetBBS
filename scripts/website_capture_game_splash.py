"""Record a bundled game's real launch splash, through its own play_splash.

Like website_capture_door_scene.py, replace only the watching terminal and
its waits. Photograph the held final frame before the game clears it. This
also works on Windows, where a subprocess pipe is not an interactive console.
"""
from __future__ import annotations

import argparse
import contextlib
from pathlib import Path

from website_capture_door_scene import Recorder, _door, _patched


def capture(name: str) -> str:
    game = _door(name)
    recorder = Recorder()
    info = {"node_name": "Harbor Lights", "handle": "nova"}

    def pause(seconds: float, **_kwargs) -> bool:
        recorder.frame(seconds)
        return name == "voidrunner"

    def hold(*_attract) -> None:  # Voidrunner v1.5.0 passes its attract frame
        recorder.frame(0)

    with contextlib.redirect_stdout(recorder):
        if name == "voidrunner":
            with _patched(game, _OUTPUT_STYLE="auto", _PALETTE=game.Palette(truecolor=True),
                          _OUTPUT_WIDTH=80, _OUTPUT_HEIGHT=24, _RESIZE_PENDING=False,
                          motion_interrupted=lambda: False, motion_pause=pause,
                          _splash_hold=hold):
                shown = game.play_splash(info)
        elif name == "war_dialer":
            with _patched(game, _splash_ready=lambda p: True, _beat=pause, _splash_hold=hold):
                shown = game.play_splash(game.Palette(truecolor=True, color_depth="truecolor"),
                                         info, 3, 80, 24)
        else:
            class Watching:
                def waiting(self, seconds: float) -> bool:
                    return False

                def take(self, seconds: float) -> bool:
                    recorder.frame(seconds)
                    return seconds == game.SPLASH_HOLD_POLL_SECONDS

            with _patched(game, _PLAIN=False, _OUTPUT_WIDTH=80):
                shown = game.play_splash(game.Palette(truecolor=True), info, 80, 24,
                                         poll=Watching())
    if not shown or not recorder.frames:
        raise SystemExit(f"{name} drew no splash; check DOOR_SPLASH")
    # Pending output is the teardown, deliberately left out as for scene casts.
    return "".join(frame["ansi"] for frame in recorder.frames)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("door", choices=("voidrunner", "war_dialer", "retro_trivia"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(capture(args.door).encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
