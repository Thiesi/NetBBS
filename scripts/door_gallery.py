"""Render a bundled door's screens at every supported size and preset, as a page.

    python scripts/door_gallery.py voidrunner --out build/gallery
    python scripts/door_gallery.py war_dialer --widths 80 64 40 --open

Why this exists: a door's presentation cannot be reviewed by reading a diff, and
the test suite can only ever assert that a screen *fits* -- never that it looks
like anything. Both bundled games lost their entire visual design across an
overhaul in which every individual slice passed review and every test stayed
green (issues #493, #494). The countermeasure is that a change to a screen comes
with pictures: this script drives the real door in a subprocess, keeps the bytes
it writes, and paints them with the same ANSI emulator the netbbs.org
screenshots use, so what the page shows is what a caller's terminal shows.

A scripted walk is deliberately shallow -- it opens a screen and comes back --
because the point is the look of each screen, not a playthrough. Add a walk to
`WALKS` when a door grows a screen the current keys do not reach.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
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

# Each walk is (label, keys). The keys are pressed in order; a screen that needs
# a different route gets its own walk rather than a longer one.
WALKS: dict[str, list[tuple[str, bytes]]] = {
    "voidrunner": [
        ("Title and registration", b""),
        ("Command Deck", b"\rY"),
        ("Command Deck, expanded", b"\rYX"),
        ("Commodity Market", b"\rYM"),
        ("Engineering Yard", b"\rYY"),
        ("Mission Board", b"\rYB"),
        ("Navigation Chart", b"\rYC"),
        ("Pilot Record", b"\rYS"),
        ("Hall of Fame", b"\rYH"),
        ("Pilot Guide", b"\rYG"),
        ("Trading Ledger", b"\rYT"),
        ("Viewport", b"\rYV"),
    ],
    "war_dialer": [
        ("Masthead and first visit", b""),
        ("Switchboard", b"\r\r"),
        ("The scene", b"\r\rE"),
        ("Rank", b"\r\rB"),
        ("Rivals", b"\r\rV"),
        ("Crew", b"\r\rC"),
        ("Trade preview", b"\r\rT"),
        ("Job preview", b"\r\rJ"),
        ("Help", b"\r\r?"),
    ],
}

PRESETS = {
    "voidrunner": {"auto": {}, "plain": {}},
    "war_dialer": {"auto": {"unicode_style": True}, "plain": {"unicode_style": False}},
}


def capture(door: pathlib.Path, keys: bytes, width: int, height: int, extra: dict) -> str:
    """Run the door the way NetBBS does and keep what it wrote."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="gallery-"))
    info = {
        "handle": "Thiesi", "user_id": 1, "terminal_width": width, "terminal_height": height,
        "color_depth": "truecolor", "node_name": "ReLink",
        # War Dialer refuses to launch without a host world owner.
        "war_dialer_owner": "0123456789abcdef0123456789abcdef",
    }
    info.update(extra)
    (work / "info.json").write_text(json.dumps(info), encoding="utf-8")
    env = dict(os.environ)
    env.update(NETBBS_DOOR_INFO=str(work / "info.json"),
               VOIDRUNNER_SAVE_DIR=str(work / "saves"),
               WAR_DIALER_DB_PATH=str(work / "war-dialer.db"),
               PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, str(door)], input=keys, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=180)
    if done.returncode != 0 and not done.stdout:
        raise SystemExit(f"{door.name} exited {done.returncode}: {done.stderr.decode('utf-8', 'replace')[-400:]}")
    return done.stdout.decode("utf-8", "replace")


def last_screen(raw: str) -> str:
    """The final painted screen: doors clear before each one, so the tail is it."""
    return raw.split(CLEAR)[-1].rstrip("\r\n")


def to_html(screen: str, width: int, styles: dict[str, str]) -> str:
    rows = len(screen.split("\r\n")) + 1
    fragment = term.render(screen, width=width, height=rows)
    out = []
    for row in fragment.split("\n"):
        cells, run, length = [], None, 0
        for style, char in SPAN.findall(row):
            keep = ";".join(part for part in style.split(";")
                            if part.startswith(("color:", "background:", "font-weight")))
            name = styles.setdefault(keep, f"s{len(styles)}")
            if char == " " and run in (None, name):
                run, length = name, length + 1
                continue
            if length:
                cells.append(f'<i class={run} style="width:{length}ch"></i>')
            run, length = (name, 1) if char == " " else (None, 0)
            if char != " ":
                cells.append(f"<i class={name}>{char}</i>")
        if length:
            cells.append(f'<i class={run} style="width:{length}ch"></i>')
        out.append("".join(cells))
    return "\n".join(out)


def build(door_name: str, widths: list[int], heights: dict[int, int], out_dir: pathlib.Path) -> pathlib.Path:
    door = ROOT / "src/netbbs/doors/bundled" / f"{door_name}.py"
    if not door.exists():
        raise SystemExit(f"no such bundled door: {door}")
    styles: dict[str, str] = {}
    sections = []
    for label, keys in WALKS[door_name]:
        panels = []
        for preset, extra in PRESETS[door_name].items():
            for width in widths:
                height = heights.get(width, 24)
                screen = last_screen(capture(door, keys, width, height, extra))
                panels.append(
                    f'<figure><figcaption>{html.escape(f"{width}x{height} · {preset}")}</figcaption>'
                    f'<div class="screen"><pre>{to_html(screen, width, styles)}</pre></div></figure>')
        sections.append(f'<section><h2>{html.escape(label)}</h2><div class="row">{"".join(panels)}</div></section>')

    css = "\n".join(f"i.{name}{{{style}}}" for style, name in styles.items() if style)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(door_name)} gallery</title>
<style>
 body{{margin:0;padding:32px 20px 64px;background:#080b12;color:#c7d2e2;
   font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
 h1{{margin:0 0 24px;font-size:28px;color:#eef3fa}}
 h2{{margin:0 0 12px;font-size:18px;color:#eef3fa}}
 section{{margin-bottom:32px;padding:16px;border:1px solid #232c3d;border-radius:6px;background:#111725}}
 .row{{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}}
 figure{{margin:0;display:flex;flex-direction:column;gap:6px;min-width:0}}
 figcaption{{font:11px/1.4 ui-monospace,Consolas,monospace;letter-spacing:.07em;color:#8794a8}}
 .screen{{background:#05070b;border:1px solid #232c3d;border-radius:4px;padding:10px 12px;overflow-x:auto}}
 pre{{margin:0;white-space:pre;font-family:"DejaVu Sans Mono",Consolas,monospace;
   font-size:12px;line-height:1.32;color:#d5dde9}}
 pre i{{display:inline-block;width:1ch;font-style:normal}}
{css}
</style></head><body>
<h1>{html.escape(door_name)} — every screen, every size</h1>
<p>Rendered from the real door in a subprocess; painted by
<code>scripts/website_ansi_to_html.py</code>. Attach this page to any pull request
that changes a screen.</p>
{"".join(sections)}
</body></html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{door_name}-gallery.html"
    target.write_text(page, encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("door", choices=sorted(WALKS))
    parser.add_argument("--widths", type=int, nargs="+", default=[80, 64, 40],
                        help="terminal widths to render (the floor is 40)")
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "build" / "gallery")
    parser.add_argument("--open", action="store_true", help="open the page when it is written")
    args = parser.parse_args()

    heights = {80: 24, 64: 20, 40: 12}
    target = build(args.door, args.widths, heights, args.out)
    print(f"wrote {target}")
    if args.open:
        webbrowser.open(target.as_uri())


if __name__ == "__main__":
    main()
