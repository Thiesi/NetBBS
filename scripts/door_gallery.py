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

**Panels must be comparable.** A door generates its world from a random seed, so
a gallery built from fresh state would show different systems, names and missions
in every panel and again on every run, and a before/after review would be
meaningless. Each (door, preset) therefore gets a *fixture* -- one career or world
created once, cached under the output directory, and copied for every capture, so
only the size and the preset vary. `--fresh` rebuilds them; delete the directory
and the next run makes new ones.

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
import webbrowser

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import website_ansi_to_html as term  # noqa: E402

SPAN = re.compile(r'<span style="([^"]*)">(.*?)</span>')
CLEAR = "\x1b[2J\x1b[H"

# Each walk is (label, keys), pressed in order against a door that already has a
# career or world: registration and the first-visit guide belong to the fixture,
# not to every panel.
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
        ("The scene", b"E"),
        ("Rank", b"B"),
        ("Rivals", b"V"),
        ("Crew", b"C"),
        ("Trade preview", b"T"),
        ("Job preview", b"J"),
        ("Kit", b"S"),
        ("Help", b"?"),
    ],
}

# Keys that take a brand-new career or world through everything a first launch
# asks, so a panel never opens on registration or the first-visit guide.
ONBOARDING: dict[str, bytes] = {"voidrunner": b"\rY", "war_dialer": b"\r\r"}

# Presets are applied to the fixture, not passed as flags: Voidrunner keeps its
# display style in the career, War Dialer takes `unicode_style` from the drop file.
PRESETS: dict[str, dict[str, dict]] = {
    "voidrunner": {"auto": {"display_style": "auto"}, "plain": {"display_style": "plain"}},
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


def run(door: pathlib.Path, state: pathlib.Path, keys: bytes, width: int, height: int,
        info_extra: dict) -> str:
    """Run the door against a state directory and return everything it wrote."""
    env = dict(os.environ)
    env.update(NETBBS_DOOR_INFO=str(drop_file(state, width, height, info_extra)),
               VOIDRUNNER_SAVE_DIR=str(state / "saves"),
               WAR_DIALER_DB_PATH=str(state / "war-dialer.db"),
               PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, str(door)], input=keys, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=180)
    if done.returncode != 0:
        # A gallery that certifies a broken walk is worse than no gallery: a door
        # that exits nonzero has crashed, whatever it managed to print first.
        raise SystemExit(f"{door.name} exited {done.returncode} for keys {keys!r} at "
                         f"{width}x{height}:\n{done.stderr.decode('utf-8', 'replace')[-800:]}")
    return done.stdout.decode("utf-8", "replace")


def make_fixture(door_name: str, door: pathlib.Path, preset: str, extra: dict,
                 root_dir: pathlib.Path) -> pathlib.Path:
    """One career or world per preset, created once and reused by every panel."""
    fixture = root_dir / "fixtures" / f"{door_name}-{preset}"
    if fixture.exists():
        return fixture
    fixture.mkdir(parents=True)
    info_extra = {key: value for key, value in extra.items() if key != "display_style"}
    run(door, fixture, ONBOARDING[door_name], 80, 24, info_extra)
    style = extra.get("display_style")
    if style:
        for save in (fixture / "saves").glob("*.json"):
            if save.name.endswith(".previous") or "recovery" in save.name:
                continue
            data = json.loads(save.read_text(encoding="utf-8"))
            data["display_style"] = style
            save.write_text(json.dumps(data), encoding="utf-8")
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
    if fresh:
        shutil.rmtree(out_dir / "fixtures", ignore_errors=True)

    styles: dict[str, str] = {}
    sections = []
    for label, keys in WALKS[door_name]:
        panels = []
        for preset, extra in PRESETS[door_name].items():
            fixture = make_fixture(door_name, door, preset, extra, out_dir)
            info_extra = {key: value for key, value in extra.items() if key != "display_style"}
            for width in widths:
                height = heights.get(width, 24)
                work = pathlib.Path(tempfile.mkdtemp(prefix="gallery-"))
                try:
                    state = work / "state"
                    shutil.copytree(fixture, state)
                    screen = last_screen(run(door, state, keys + b"\x1b\x1b", width, height, info_extra))
                    painted = to_html(screen, width, height, styles)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                panels.append(
                    f'<figure><figcaption>{html.escape(f"{width}x{height} · {preset}")}</figcaption>'
                    f'<div class="screen"><pre>{painted}</pre></div></figure>')
        sections.append(f'<section><h2>{html.escape(label)}</h2><div class="row">{"".join(panels)}</div></section>')

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
<p>Rendered from the real door in a subprocess and painted by
<code>scripts/website_ansi_to_html.py</code> on a canvas the size of the terminal,
from one cached career per preset so that panels differ only by size and preset.
Attach this page to any pull request that changes a screen.</p>
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
    parser.add_argument("--fresh", action="store_true", help="rebuild the cached fixtures first")
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
