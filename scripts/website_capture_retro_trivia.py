"""Capture Retro Trivia by launching it and choosing a round, one key at a time.

The questions are intentionally unseeded, as in a caller's game. Committed raw
captures retain the actual round shown; regenerating may draw another question.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import tempfile

from door_gallery import Door, ROOT, remove


def capture(screen: str) -> str:
    state = Path(tempfile.mkdtemp(prefix="website-trivia-"))
    running = None
    try:
        running = Door(ROOT / "src/netbbs/doors/bundled/retro_trivia.py", state, 80, 24,
                       {"handle": "nova", "node_name": "Harbor Lights"})
        running.settle()
        if screen == "question":
            running.press(b"1")  # the game's own five-question option
        raw = running.read()
        # This scrolling game keeps its title above the question. Keep the
        # complete output so the terminal emulator shows what the caller sees.
        marker = "Question 1/5" if screen == "question" else "Round length"
        if marker not in re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw):
            raise SystemExit(f"Retro Trivia did not reach {screen}")
        running.finish()
        return re.sub(r"\r+\n", "\r\n", raw)
    except BaseException:
        if running is not None and running.proc.poll() is None:
            running.kill()
        raise
    finally:
        remove(state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("screen", choices=("title", "question"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(capture(args.screen).encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
