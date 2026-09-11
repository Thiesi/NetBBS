"""Render a bundled door's screens at every supported size and preset, as a page.

    python scripts/door_gallery.py voidrunner --out build/gallery --open
    python scripts/door_gallery.py war_dialer --widths 80 64 40 --fresh

Why this exists: a door's presentation cannot be reviewed by reading a diff, and
the test suite can only ever assert that a screen *fits* -- never that it looks
like anything. Both bundled games lost their entire visual design across an
overhaul in which every individual slice passed review and every test stayed
green (issues #493, #494). The countermeasure is that a change to a screen comes
with pictures: this drives the real door in a subprocess, keeps the bytes it
writes, and paints them with the same ANSI emulator the netbbs.org screenshots
use, on a canvas the size of the terminal being simulated -- so a panel is the
final screen a caller sees, scrolling included, not a transcript of everything
that was ever printed.

**A door is typed at, not piped into.** Both doors treat bytes that arrive
together as an unframed paste and discard them (War Dialer's burst window is
20ms), and a screen is only on the terminal until the next keystroke redraws
over it. So `Door` writes one key at a time, waits for the output to go quiet
after each, and photographs the screen *before* anything is sent to leave it --
a walk that piped its keys in at once photographed the switchboard nine times.

**Panels must be comparable.** A door generates its world from a random seed, so
a gallery built from fresh state would show different systems, names and missions
in every panel and again on every run, and a before/after review would be
meaningless. Each door therefore gets one *fixture* -- a single career or world
created once, cached under the output directory and copied for every capture,
with the display preset applied to the copy -- so only the size and the preset
vary. `--fresh` rebuilds them; delete the directory and the next run makes new
ones.

A scripted walk is deliberately shallow: it opens a screen and comes back. Add a
walk to `WALKS` when a door grows a screen the current keys do not reach.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import website_ansi_to_html as term  # noqa: E402

SPAN = re.compile(r'<span style="([^"]*)">(.*?)</span>')
CLEAR = "\x1b[2J\x1b[H"

# A keystroke is isolated by the silence around it: comfortably more than War
# Dialer's 20ms burst window, and enough quiet afterwards for the door to have
# finished drawing what the key asked for.
QUIET = 0.15
SETTLE = 0.75
ANSWER = 6.0  # a keypress can take a second to redraw; past this it did nothing.
PATIENCE = 30.0  # a door still drawing after this is hung, not slow.
WORKERS = 4  # panels are independent subprocesses; a gallery is 150+ of them.

# Each walk is (label, keys), pressed in order against a door that already has a
# career or world: registration and the first-visit guide belong to the fixture,
# not to every panel. The key is the one the door's own dispatch uses -- check it
# there, not in the action bar, before adding a walk.
WALKS: dict[str, list[tuple[str, bytes]]] = {
    "voidrunner": [
        ("Command Deck", b""),
        ("Command Deck, expanded", b"X"),
        ("Commodity Market", b"M"),
        ("Engineering Yard", b"Y"),
        ("Mission Board", b"B"),
        ("Navigation Chart", b"C"),
        ("Pilot Record", b"S"),
        ("Hall of Fame", b"H"),
        ("Pilot Guide", b"G"),
        ("Trading Ledger", b"T"),
        ("Viewport", b"V"),
        ("Display Options", b"O"),
    ],
    "war_dialer": [
        ("Switchboard", b""),
        ("The scene", b"I"),
        ("Territory map", b"E"),
        ("Rank", b"B"),
        ("Rivals", b"V"),
        ("Log", b"H"),
        ("Contract board", b"J"),
        # An action's preview is two pickers deep: the board, the approach, and
        # only then the terms the player is actually asked to accept.
        ("Job preview", b"J11"),
        ("Trade preview", b"T"),
        ("Recruit preview", b"C"),
        ("Crew development", b"S"),
        ("Root exchange", b"X"),
        ("Garrisons", b"G"),
        ("Operations", b"O"),
        ("Help", b"?"),
    ],
}

# Keys that take a brand-new career or world through everything a first launch
# asks, so a panel never opens on registration or the first-visit guide.
ONBOARDING: dict[str, bytes] = {"voidrunner": b"\rY", "war_dialer": b"\r\r"}

# Every preset a caller can choose, applied to the fixture's copy rather than
# passed as a flag: Voidrunner keeps its display style in the career (all four of
# `DISPLAY_STYLES`), War Dialer takes `unicode_style` from the drop file.
PRESETS: dict[str, dict[str, dict]] = {
    "voidrunner": {style: {"display_style": style} for style in
                   ("auto", "basic", "mono", "plain")},
    "war_dialer": {"auto": {"unicode_style": True}, "plain": {"unicode_style": False}},
}


def drop_file(work: pathlib.Path, width: int, height: int, info_extra: dict) -> pathlib.Path:
    info = {
        "handle": "Thiesi", "user_id": 1, "terminal_width": width, "terminal_height": height,
        "color_depth": "truecolor", "node_name": "ReLink",
        # War Dialer refuses to launch without a host world owner.
        "war_dialer_owner": "0123456789abcdef0123456789abcdef",
    }
    info.update(info_extra)
    path = work / "info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


class Door:
    """A door in a subprocess, typed at one key at a time.

    `read()` returns everything written so far, so a caller can photograph a
    screen while the door is still sitting on it.
    """

    def __init__(self, door: pathlib.Path, state: pathlib.Path, width: int, height: int,
                 info_extra: dict) -> None:
        self.door = door
        env = dict(os.environ)
        env.update(NETBBS_DOOR_INFO=str(drop_file(state, width, height, info_extra)),
                   VOIDRUNNER_SAVE_DIR=str(state / "saves"),
                   WAR_DIALER_DB_PATH=str(state / "war-dialer.db"),
                   PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(door)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self.lock = threading.Lock()
        self.out, self.err = bytearray(), bytearray()
        self.spoke = time.monotonic()
        self.readers = [self._reader(self.proc.stdout, self.out, True),
                        self._reader(self.proc.stderr, self.err, False)]

    def _reader(self, stream, sink: bytearray, is_stdout: bool) -> threading.Thread:
        def pump() -> None:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    return
                with self.lock:
                    sink.extend(chunk)
                    if is_stdout:
                        self.spoke = time.monotonic()
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return thread

    def settle(self, since: int = 0, *, require: bool = False) -> None:
        """Wait for the door to answer and then stop drawing.

        Silence alone does not mean the screen is ready: a door that has been
        sitting at a prompt has been silent for as long as the caller took to
        press a key, so a settle that only measured quiet returned before the
        keypress had drawn anything and photographed the previous screen.
        `since` is the output length before the key went in; the wait is over
        only once the door has written past it and then gone quiet.

        A key that legitimately changes nothing is an answer too, so the wait
        gives up after `ANSWER` -- except when `require` says the door owes us a
        screen, as at startup: a door that is slow to boot (four of them share
        this machine) would otherwise be photographed blank.
        """
        answer = time.monotonic() + ANSWER
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            with self.lock:
                quiet = time.monotonic() - self.spoke
                answered = len(self.out) > since
            if answered and quiet >= SETTLE:
                return
            if not answered and not require and time.monotonic() >= answer:
                return
            time.sleep(0.05)
        if require:
            raise SystemExit(f"{self.door.name} printed nothing in {PATIENCE:.0f}s:\n"
                             f"{bytes(self.err).decode('utf-8', 'replace')[-800:]}")

    def press(self, key: bytes) -> None:
        time.sleep(QUIET)  # every key arrives alone; a burst is discarded as paste
        with self.lock:
            before = len(self.out)
        self.proc.stdin.write(key)
        self.proc.stdin.flush()
        self.settle(before)

    def read(self) -> str:
        with self.lock:
            return bytes(self.out).decode("utf-8", "replace")

    def finish(self) -> None:
        """Close stdin, let the door exit, and refuse to publish a crash.

        A gallery that certifies a broken walk is worse than no gallery: a door
        that exits nonzero has crashed, whatever it managed to print first.
        """
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise SystemExit(f"{self.door.name} never exited after its keys were pressed")
        for reader in self.readers:
            reader.join(timeout=5)
        if code != 0:
            raise SystemExit(f"{self.door.name} exited {code}:\n"
                             f"{bytes(self.err).decode('utf-8', 'replace')[-800:]}")


def capture(door: pathlib.Path, state: pathlib.Path, keys: bytes, width: int, height: int,
            info_extra: dict) -> str:
    """Drive a walk and return the screen it is looking at when it is done."""
    running = Door(door, state, width, height, info_extra)
    running.settle(require=True)  # the opening screen is owed, however slow the boot
    for index in range(len(keys)):
        running.press(keys[index:index + 1])
    screen = running.read()
    running.finish()
    return screen


def apply_preset(state: pathlib.Path, extra: dict) -> dict:
    """Put the preset where the door reads it; return what the drop file needs."""
    style = extra.get("display_style")
    if style:
        for save in (state / "saves").glob("*.json"):
            if save.name.endswith(".previous") or "recovery" in save.name:
                continue
            data = json.loads(save.read_text(encoding="utf-8"))
            data["display_style"] = style
            save.write_text(json.dumps(data), encoding="utf-8")
    return {key: value for key, value in extra.items() if key != "display_style"}


def base_fixture(door_name: str, door: pathlib.Path, root_dir: pathlib.Path,
                 fresh: bool = False) -> pathlib.Path:
    """One career or world per door, created once and copied by every panel.

    Built in a temporary directory and moved into place only once the door has
    exited cleanly, so an interrupted run never leaves a half-made fixture that
    the next run would accept simply because the directory exists.
    """
    fixture = root_dir / "fixtures" / door_name
    if fresh:
        shutil.rmtree(fixture, ignore_errors=True)
    if fixture.exists():
        return fixture
    staging = pathlib.Path(tempfile.mkdtemp(prefix="gallery-fixture-"))
    try:
        state = staging / "state"
        state.mkdir()
        capture(door, state, ONBOARDING[door_name], 80, 24, {})
        fixture.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(state), str(fixture))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return fixture


def last_screen(raw: str) -> str:
    """What is left on the terminal: doors clear before each screen."""
    return raw.split(CLEAR)[-1]


def to_html(screen: str, width: int, height: int, styles: dict[str, str]) -> str:
    """Paint the screen on a canvas the size of the terminal it was drawn for."""
    fragment = term.render(screen, width=width, height=height)
    out = []
    for row in fragment.split("\n"):
        cells, run_name, run_length = [], None, 0
        for style, char in SPAN.findall(row):
            keep = ";".join(part for part in style.split(";")
                            if part.startswith(("color:", "background:", "font-weight")))
            name = styles.setdefault(keep, f"s{len(styles)}")
            if char == " " and run_name in (None, name):
                run_name, run_length = name, run_length + 1
                continue
            if run_length:
                cells.append(f'<i class={run_name} style="width:{run_length}ch"></i>')
            run_name, run_length = (name, 1) if char == " " else (None, 0)
            if char != " ":
                cells.append(f"<i class={name}>{char}</i>")
        if run_length:
            cells.append(f'<i class={run_name} style="width:{run_length}ch"></i>')
        out.append("".join(cells))
    return "\n".join(out) or "&nbsp;"


def build(door_name: str, widths: list[int], heights: dict[int, int],
          out_dir: pathlib.Path, fresh: bool) -> pathlib.Path:
    door = ROOT / "src/netbbs/doors/bundled" / f"{door_name}.py"
    if not door.exists():
        raise SystemExit(f"no such bundled door: {door}")
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture = base_fixture(door_name, door, out_dir, fresh)

    shots = [(label, keys, preset, extra, width, heights.get(width, 24))
             for label, keys in WALKS[door_name]
             for preset, extra in PRESETS[door_name].items()
             for width in widths]

    def shoot(shot) -> str:
        _, keys, _, extra, width, height = shot
        work = pathlib.Path(tempfile.mkdtemp(prefix="gallery-"))
        try:
            state = work / "state"
            shutil.copytree(fixture, state)
            info_extra = apply_preset(state, extra)
            return last_screen(capture(door, state, keys, width, height, info_extra))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # Each panel is its own door in its own copy of the fixture, so they can be
    # taken at once; the page is assembled from them in order afterwards.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        screens = list(pool.map(shoot, shots))

    styles: dict[str, str] = {}
    sections, panels, current = [], [], shots[0][0]
    for (label, _, preset, _, width, height), screen in zip(shots, screens):
        if label != current:
            sections.append(f'<section><h2>{html.escape(current)}</h2>'
                            f'<div class="row">{"".join(panels)}</div></section>')
            panels, current = [], label
        panels.append(
            f'<figure><figcaption>{html.escape(f"{width}x{height} · {preset}")}</figcaption>'
            f'<div class="screen"><pre>{to_html(screen, width, height, styles)}</pre></div></figure>')
    sections.append(f'<section><h2>{html.escape(current)}</h2>'
                    f'<div class="row">{"".join(panels)}</div></section>')

    css = "\n".join(f"i.{name}{{{style}}}" for style, name in styles.items() if style)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(door_name)} gallery</title>
<style>
 body{{margin:0;padding:32px 20px 64px;background:#080b12;color:#c7d2e2;
   font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
 h1{{margin:0 0 8px;font-size:28px;color:#eef3fa}}
 h2{{margin:0 0 12px;font-size:18px;color:#eef3fa}}
 p{{margin:0 0 24px;max-width:74ch}}
 section{{margin-bottom:32px;padding:16px;border:1px solid #232c3d;border-radius:6px;background:#111725}}
 .row{{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}}
 figure{{margin:0;display:flex;flex-direction:column;gap:6px;min-width:0}}
 figcaption{{font:11px/1.4 ui-monospace,Consolas,monospace;letter-spacing:.07em;color:#8794a8}}
 .screen{{background:#05070b;border:1px solid #232c3d;border-radius:4px;padding:10px 12px;overflow-x:auto}}
 pre{{margin:0;white-space:pre;font-family:"DejaVu Sans Mono",Consolas,monospace;
   font-size:12px;line-height:1.32;color:#d5dde9}}
 pre i{{display:inline-block;width:1ch;font-style:normal}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:.92em}}
{css}
</style></head><body>
<h1>{html.escape(door_name)} — every screen, every size</h1>
<p>Rendered from the real door in a subprocess, typed at one key at a time and
painted by <code>scripts/website_ansi_to_html.py</code> on a canvas the size of
the terminal, from one cached career so that panels differ only by size and
preset. Attach this page to any pull request that changes a screen.</p>
{"".join(sections)}
</body></html>
"""
    target = out_dir / f"{door_name}-gallery.html"
    target.write_text(page, encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("door", choices=sorted(WALKS))
    parser.add_argument("--widths", type=int, nargs="+", default=[80, 64, 40],
                        help="terminal widths to render (the supported floor is 40)")
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "build" / "gallery")
    parser.add_argument("--fresh", action="store_true", help="rebuild the cached fixture first")
    parser.add_argument("--open", action="store_true", help="open the page when it is written")
    args = parser.parse_args()

    heights = {80: 24, 64: 20, 40: 12}
    out_dir = args.out.expanduser().resolve()
    target = build(args.door, args.widths, heights, out_dir, args.fresh)
    print(f"wrote {target}")
    if args.open:
        webbrowser.open(target.as_uri())


if __name__ == "__main__":
    main()
