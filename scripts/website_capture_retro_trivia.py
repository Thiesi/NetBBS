"""Capture Retro Trivia by launching it and choosing a round, one key at a time.

The questions are intentionally unseeded, as in a caller's game. Committed raw
captures retain the actual round shown; regenerating may draw another question.

`question` stops on the first question of a five-question round. `streak`
plays further into the same kind of round: it answers the first three
questions correctly and stops on the fourth, so the screen shows the last
answer's result, the progress bar partly filled and the next question. The
right letter is read from the screen the game drew, matched against the
game's own question table; no answer or screen is written by this script.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import tempfile

from door_gallery import Door, ROOT, load_door, remove

DOOR = ROOT / "src/netbbs/doors/bundled/retro_trivia.py"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STREAK = 3  # questions answered correctly before the photograph


def _plain(raw: str) -> str:
    return ANSI.sub("", raw).replace("\r", "")


def _asked(raw: str, number: int) -> tuple[str, dict[str, str]]:
    """The question text and its lettered choices, as the game drew them."""
    text = _plain(raw)
    start = text.rfind(f"Question {number}/")
    if start < 0:
        raise SystemExit(f"Retro Trivia did not ask question {number}")
    rows = [row.strip().strip("│").strip() for row in text[start:].split("\n")[1:]]
    divider = next(i for i, row in enumerate(rows) if row.startswith("╞"))
    question = " ".join(row for row in rows[:divider] if row)
    choices: dict[str, str] = {}
    letter = None
    for row in rows[divider + 1:]:
        if row.startswith("╰"):
            break
        match = re.match(r"^\[([A-D])\]\s+(.*)$", row)
        if match:
            letter = match.group(1)
            choices[letter] = match.group(2)
        elif row and letter:
            choices[letter] += " " + row  # a long choice wraps under its marker
    return question, choices


def _right_letter(table: list, question: str, choices: dict[str, str]) -> str:
    for text, options, correct in table:
        if " ".join(text.split()) == question:
            answer = " ".join(options[correct].split())
            for letter, shown in choices.items():
                if shown == answer:
                    return letter
    raise SystemExit(f"no answer in the game's table for {question!r}")


def capture(screen: str) -> str:
    state = Path(tempfile.mkdtemp(prefix="website-trivia-"))
    running = None
    try:
        running = Door(DOOR, state, 80, 24,
                       {"handle": "nova", "node_name": "Harbor Lights"})
        running.settle()
        if screen in ("question", "streak"):
            running.press(b"1")  # the game's own five-question option
        if screen == "streak":
            table = load_door(DOOR).QUESTIONS
            for number in range(1, STREAK + 1):
                letter = _right_letter(table, *_asked(running.read(), number))
                running.press(letter.encode())
        raw = running.read()
        # This scrolling game keeps its title above the question. Keep the
        # complete output so the terminal emulator shows what the caller sees.
        marker = {"question": "Question 1/5", "streak": f"Question {STREAK + 1}/5"}.get(
            screen, "Round length")
        if marker not in _plain(raw):
            raise SystemExit(f"Retro Trivia did not reach {screen}")
        if screen == "streak" and _plain(raw).count("Correct!") != STREAK:
            raise SystemExit(f"Retro Trivia did not mark {STREAK} answers correct")
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
    parser.add_argument("screen", choices=("title", "question", "streak"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(capture(args.screen).encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
