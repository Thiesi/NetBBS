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
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
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
ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
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
#
# The suffixes exist because how much fits a page depends on the terminal, so a
# walk can name a place or an entry but never a page number:
#
#   `*`  press this key until the screen stops changing, then photograph that.
#        War Dialer's switchboard is two pages at 80x24 and eleven at 40x12, so a
#        fixed number of Next presses photographed page two twice at one size and
#        never reached the feed, the orders or the season card at another.
#   `?`  page forward until the screen offers this key, then press it. The scene
#        hub shows all seven entries at 80x24 and four at 40x12, so `I5` reached
#        "Your season reports" at one size and a key the picker was ignoring at
#        another.
#   `#`  press whichever key the screen offers first, for a walk that wants *an*
#        entry rather than a named one. The root picker marks the caller's own
#        holdings `[-]`, so which digit it accepts depends on the fixture's own
#        history -- naming one there broke the build twice as the fixture grew.
#   `$`  page to the last page and press the last key offered there, for an entry
#        a picker appends after a variable list. The exchange control screen puts
#        the owner service after however many crew transfers the holding happens
#        to offer, so `3` meant the service only for as long as the fixture kept
#        exactly two of them -- and a digit that lands on a transfer instead
#        publishes a garrison preview under the service's caption.
#
# A walk photographs one screen: the one it is looking at when its keys run out.
# Passing *through* a picker on the way somewhere else therefore reviews nothing
# of it, so a screen on the way to another screen needs a walk of its own that
# stops there. `SHOWS` names what each walk's screen must say, and the build
# refuses to publish a panel that does not say it.
WALKS: dict[str, list[tuple[str, bytes]]] = {
    "voidrunner": [
        ("Command Deck", b""),
        ("Command Deck, expanded", b"X"),
        ("Commodity Market", b"M"),
        ("Engineering Yard", b"Y"),
        ("Mission Board", b"B"),
        ("Contract Details", b"B1"),
        ("Navigation Chart", b"C"),
        ("Pilot Record", b"S"),
        ("Hall of Fame", b"H"),
        ("Pilot Guide", b"G"),
        ("Trading Ledger", b"T"),
        ("Viewport", b"V"),
        ("Display Options", b"O"),
        ("Archive Contacts", b"N"),
        ("Concord Contacts", b"P"),
        ("Blackwake Contacts", b"W"),
        # The chart opens as a list; its own [V] is the spatial map, which is a
        # different drawing and the one the 40x12 floor was argued over.
        ("Navigation Chart, map", b"CV"),
    ],
    "war_dialer": [
        ("Switchboard", b""),
        # The switchboard is a card stack paged with [N]: at forty columns the
        # scene and the feed are the pages after the gauges (issue #494).
        ("Switchboard, page 2", b"N"),
        ("Switchboard, last page", b"N*"),
        ("BBS scene", b"I"),
        ("Crew insignia", b"I1?"),
        ("Insignia preview", b"I1?1?"),
        # Committing the insignia is its own screen, and the only result screen
        # in the door that reports a change costing nothing.
        ("Insignia chosen", b"I1?1?A"),
        ("Neutral dossiers", b"I2?"),
        ("Scene bulletins", b"I3?"),
        ("Season results", b"I4?"),
        ("Your season reports", b"I5?"),
        ("Hall of Fame", b"I6?"),
        # The last hub entry is the caller's own display screen; no other walk
        # opens it.
        ("Display options", b"I7?"),
        ("The scene", b"E"),
        # A digit on the scene screen is the exchange's own number, so it opens
        # that exchange's card wherever the table has been paged to.
        ("Exchange card", b"E1?"),
        ("Rank", b"B"),
        ("Rivals", b"V"),
        # The raid dispatch is its own pair of screens, and the raid preview is
        # the only one in the door that draws an empty gauge on purpose: a
        # rival's crew strength is private, so there are no odds to show.
        ("Raid targets", b"R"),
        ("Raid preview", b"R#"),
        ("Log", b"H"),
        ("Contract board", b"J"),
        # An action's preview is two pickers deep: the board, the approach, and
        # only then the terms the player is actually asked to accept. Each of the
        # three is a screen, so each is a walk: a walk to the preview passes
        # through the approach picker and photographs only the preview.
        ("Choose approach", b"J1?"),
        ("Job preview", b"J1?1?"),
        ("Job preview, terms", b"J1?1?N"),
        ("Trade preview", b"T"),
        ("Recruit preview", b"C"),
        # Recruiting is the one action whose outcome is fixed, so the result
        # screen it commits to is the same in every panel.
        ("Action result", b"C" + b"N*" + b"A"),
        ("Crew development", b"S"),
        ("Crew preview", b"S1?"),
        ("Root exchange", b"X"),
        ("Root preview", b"X#"),
        ("Garrisons", b"G"),
        ("Exchange control", b"G1?"),
        # The transfer preview and the owner service are both behind the control
        # screen. The service is the entry after the transfers, and how many of
        # those there are is the fixture's business, so it is named as the last
        # one rather than by a digit that would silently come to mean a transfer.
        ("Garrison preview", b"G1?1?"),
        ("Owner service preview", b"G1?$"),
        ("Operations", b"O"),
        ("Case an operation", b"O1?"),
        # The step-stakes card is two pickers past the hub: the contract, the
        # approach, and only then the terms the caller is asked to accept.
        ("Operation approach", b"O1?1?"),
        ("Case preview", b"O1?1?1?"),
        # Recon names its target first and prices it second; which rival the
        # fixture offers first is its own business, hence `#`.
        ("Rival recon", b"O2?"),
        ("Recon preview", b"O2?#"),
        # The dossier list as a caller first meets it. A populated one would need
        # a snapshot in the fixture, and a snapshot expires after a day, so a
        # cached fixture would quietly photograph this same empty state anyway.
        ("Your dossiers", b"O3?"),
        ("Help", b"?"),
        ("Help, later sections", b"?N"),
    ],
}

# Keys that take a brand-new career or world through everything a first launch
# asks, so a panel never opens on registration or the first-visit guide -- and
# then far enough into the game that the screens about *having* something are
# reachable at all. War Dialer's fixture captures exchange 1: it is unclaimed in
# a fresh world, so the attempt is a certainty rather than a dice roll, and
# without it every garrison screen was a panel of "No exchanges held" and the
# switchboard's holdings and income gauges were permanently zero.
ONBOARDING: dict[str, bytes] = {"voidrunner": b"\rY", "war_dialer": b"\r\r"}

# Keys pressed after `seed()` has given the world its company and its past, to
# put the caller back in the game: dismiss the crackdown receipt the archived
# season left unread, then capture exchange 2 so the garrison screens exist.
RESUME: dict[str, bytes] = {"war_dialer": b"\r" + b"X#" + b"N*" + b"A" + b"\r"}


def load_door(door: pathlib.Path):
    """The door as a module, for seeding a world through its own functions."""
    spec = importlib.util.spec_from_file_location(f"gallery_{door.stem}", door)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before it runs: `@dataclass` resolves a field's type through
    # `sys.modules[cls.__module__]`, which is not there yet otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def seed_war_dialer(door: pathlib.Path, state: pathlib.Path) -> None:
    """Rivals, receipts and one closed season.

    A gallery built from a brand-new world reviews empty states: the rival
    directory says there are no other crews, recon has nothing to choose, the
    feed and the log are empty, and the season archive and Hall of Fame have no
    rows -- so the rebuilt rival table, toned receipts, podium and recognition
    cards appear in no panel at all.

    Everything that can be done by the door's own functions is: players are
    created with `load_or_create_player`, receipts with `record_event`, and the
    season is closed by rewinding the anchor and letting `_settle_world` archive
    it exactly as a real rollover would. The rank counters are written directly,
    because the only other way to give a rival a Rank is to play dozens of turns
    for them, and a fixture is allowed to start partway in.
    """
    game = load_door(door)
    conn = game.connect(state / "war-dialer.db")
    try:
        now = game.now_utc()
        season = game.current_world_season(conn, now)
        for user_id, handle in ((2, "Kilobaud"), (3, "Nightline")):
            game.load_or_create_player(conn, user_id, handle, now, season)
        with conn:
            # Old enough to be raidable, and ranked enough to take a medal.
            conn.execute("UPDATE players SET created_at=?, crew_recruited_total=12, "
                         "successful_raids=4, successful_jobs=6 WHERE user_id=2",
                         (game.to_iso(now - game.GRACE * 3),))
            conn.execute("UPDATE players SET created_at=?, crew_recruited_total=3 "
                         "WHERE user_id=3", (game.to_iso(now - game.GRACE * 3),))
            # Close the season the way a rollover does: rewind the anchor and let
            # the door's own settle archive it, podium, receipts and all.
            anchor = game.get_or_create_season_anchor(conn, now)
            conn.execute("INSERT INTO meta(key,value) VALUES ('season_anchor',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (game.to_iso(anchor - game.SEASON),))
        with game._write_transaction(conn):
            game._settle_world(conn, game.now_utc())
        now = game.now_utc()
        with conn:
            # The new season's live state: a rival holding so the ring shows one,
            # and ranks so the standings table and the ladder have something in
            # them. Counters again, for the same reason as above.
            conn.execute("UPDATE players SET crew_recruited_total=12, successful_raids=4, "
                         "successful_jobs=6 WHERE user_id=2")
            conn.execute("UPDATE players SET crew_recruited_total=3 WHERE user_id=3")
            conn.execute("UPDATE exchanges SET controller_user_id=2, garrison=3, "
                         "controlled_since=? WHERE id=3", (game.to_iso(now),))
        game.record_event(conn, 1, "Kilobaud",
                          "Kilobaud raided you and got away with $340! "
                          "All-attacker raid shield: 24 hours.", now)
        game.record_event(conn, 1, None,
                          "Rooted 212-555 Uptown Exchange; +$2/hour and +50 Rank.", now,
                          seen=True)
    finally:
        conn.close()


SEED = {"war_dialer": seed_war_dialer}

# What has to be legible on the screen a walk stops at, checked against the
# painted panel rather than the door's bytes. A caption is a claim about a
# picture, and a walk drives a picker whose entries move as the fixture grows:
# without this the gallery could publish the exchange control screen's transfer
# preview under "Owner service preview" and read as a complete review. Every walk
# of a door listed here must name its screen, so a new walk cannot be added
# unchecked.
SHOWS: dict[str, dict[str, str]] = {
    "war_dialer": {
        "Switchboard": "SWITCHBOARD",
        "Switchboard, page 2": "SWITCHBOARD",
        "Switchboard, last page": "SWITCHBOARD",
        "BBS scene": "BBS SCENE",
        "Crew insignia": "CREW INSIGNIA",
        "Insignia preview": "INSIGNIA PREVIEW",
        "Insignia chosen": "CREW IDENTITY",
        "Neutral dossiers": "NEUTRAL DOSSIERS",
        "Scene bulletins": "SCENE BULLETINS",
        "Season results": "SEASON RESULTS",
        "Your season reports": "YOUR SEASON REPORTS",
        "Hall of Fame": "HALL OF FAME",
        "Display options": "DISPLAY",
        "The scene": "THE SCENE",
        "Exchange card": "DEFENCE",
        "Rank": "SEASON STANDINGS",
        "Rivals": "RIVAL DIRECTORY",
        "Raid targets": "RAID TARGETS",
        "Raid preview": "RAID PREVIEW",
        "Log": "EVENT LOG",
        "Contract board": "CONTRACT BOARD",
        "Choose approach": "CHOOSE APPROACH",
        "Job preview": "JOB PREVIEW",
        "Job preview, terms": "JOB PREVIEW",
        "Trade preview": "TRADE PREVIEW",
        "Recruit preview": "RECRUIT PREVIEW",
        "Action result": "ACTION RESULT",
        "Crew development": "CREW DEVELOPMENT",
        "Crew preview": "CREW PREVIEW",
        "Root exchange": "ROOT EXCHANGE",
        "Root preview": "ROOT PREVIEW",
        "Garrisons": "YOUR GARRISONS",
        "Exchange control": "EXCHANGE CONTROL",
        "Garrison preview": "GARRISON PREVIEW",
        "Owner service preview": "SERVICE PREVIEW",
        "Operations": "OPERATIONS / RECON",
        "Case an operation": "CASE AN OPERATION",
        "Operation approach": "OPERATION APPROACH",
        "Case preview": "CASE PREVIEW",
        "Rival recon": "RIVAL RECON",
        "Recon preview": "RECON PREVIEW",
        "Your dossiers": "YOUR DOSSIERS",
        "Help": "HOW TO PLAY",
        "Help, later sections": "HOW TO PLAY",
    },
}

# Every preset a caller can choose, applied to the fixture's copy rather than
# passed as a flag: Voidrunner keeps its display style in the career (all four of
# `DISPLAY_STYLES`), War Dialer takes `unicode_style` from the drop file.
PRESETS: dict[str, dict[str, dict]] = {
    "voidrunner": {style: {"display_style": style} for style in
                   ("auto", "basic", "mono", "plain")},
    # War Dialer splits its presentation in two: `unicode_style` arrives in the
    # drop file, while the caller's own display switches live in the world's
    # `meta` table, which is where Fast mode -- the one deliberately unframed
    # layout -- is read from.
    "war_dialer": {"auto": {"unicode_style": True},
                   "plain": {"unicode_style": False},
                   "mono": {"unicode_style": True, "display": {"monochrome": True}},
                   "fast": {"unicode_style": True, "display": {"fast": True}}},
}


DROP_USER_ID = 1  # the caller every panel is drawn for; display rows are keyed by it


def remove(path: pathlib.Path) -> None:
    """Delete a capture's directory, once the door has let go of it.

    Windows keeps a deleted-but-open file alive, and the door's SQLite handle
    outlives its process by a moment, so a single `rmtree(ignore_errors=True)`
    quietly left the world file -- and its directory -- behind on every panel.
    """
    for _ in range(30):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.1)
    print(f"could not remove {path}", file=sys.stderr)


def drop_file(work: pathlib.Path, width: int, height: int, info_extra: dict) -> pathlib.Path:
    info = {
        "handle": "Thiesi", "user_id": DROP_USER_ID, "terminal_width": width, "terminal_height": height,
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
        self.size = f"{width}x{height}"
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

    def settle(self, since: int = 0, what: str = "startup", *, expect: bool = True) -> None:
        """Wait for the door to answer and then stop drawing.

        Silence alone does not mean the screen is ready: a door that has been
        sitting at a prompt has been silent for as long as the caller took to
        press a key, so a settle that only measured quiet returned before the
        keypress had drawn anything and photographed the previous screen.
        `since` is the output length before the key went in; the wait is over
        only once the door has written past it and then gone quiet.

        A screen is owed at startup and after every walk key, because a walk's
        keys come from the door's own dispatch: silence means the key stopped
        being accepted, and publishing the screen before it under the next
        screen's name is the exact failure this script exists to catch. Only
        `expect=False` tolerates silence, for the one case that earns it --
        first-launch keys, where a door that asks one question fewer than the
        next door leaves a key with nothing to answer.
        """
        answer = time.monotonic() + ANSWER
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            with self.lock:
                quiet = time.monotonic() - self.spoke
                answered = len(self.out) > since
            if answered and quiet >= SETTLE:
                return
            if not answered and time.monotonic() >= answer:
                if not expect:
                    return
                break  # nothing is coming; say so now rather than at the deadline
            time.sleep(0.05)
        # Either the door printed nothing where a screen was owed, or it never
        # stopped printing. Neither is what a caller would be looking at.
        raise SystemExit(
            f"{self.door.name} {'never stopped drawing' if answered else 'printed nothing'} "
            f"after {what} at {self.size}:\n"
            f"{bytes(self.err).decode('utf-8', 'replace')[-800:]}")

    def press(self, key: bytes, *, expect: bool = True) -> None:
        """Press one key and wait for the screen it is supposed to open.

        A walk's keys are chosen from the door's dispatch, so a key that draws
        nothing has stopped being accepted -- and the panel would then publish
        the screen before it under the next screen's name. That is the failure
        this whole script exists to catch, so it is an error, not a shrug.
        """
        time.sleep(QUIET)  # every key arrives alone; a burst is discarded as paste
        with self.lock:
            before = len(self.out)
        self.proc.stdin.write(key)
        self.proc.stdin.flush()
        self.settle(before, f"key {key!r}", expect=expect)

    #: How a screen says which keys it will accept right now. Matched on the hint
    #: row rather than on the frame, because Fast mode has no frame -- and an
    #: entry's own `[2]` marker can be on the screen while the entry's last row,
    #: and therefore its key, is on the next page.
    OFFERS = ("pick ", "no choice on this page", "inspect ")

    def offered(self) -> str:
        """The screen's own list of the keys it will take, or "" if it offers none."""
        for row in ANSI.sub("", last_screen(self.read())).split("\r\n"):
            row = row.strip()
            if row.startswith(self.OFFERS):
                return row
        return ""

    def press_first_offered(self, *, limit: int = 24) -> None:
        """Press the first key the screen offers, paging forward to find one.

        The root picker's first page can legitimately offer nothing at forty
        columns: the caller's own holdings are marked `[-]`, and one unselectable
        entry is most of a twelve-row page, so the choices are on page two.
        """
        for _ in range(limit):
            keys = re.findall(r"\[(\w)\]", self.offered())
            if keys:
                self.press(keys[0].encode())
                return
            before = last_screen(self.read())
            self.press(b"N")
            if last_screen(self.read()) == before:
                break
        raise SystemExit(f"{self.door.name} offered no key at {self.size}:\n"
                         f"{self.offered()!r}")

    def press_last_offered(self, *, limit: int = 24) -> None:
        """Page to the last page and press the last key offered there.

        The only stable way to name an entry a picker appends after a list whose
        length is the fixture's business: the exchange control screen's owner
        service sits after one crew transfer per offer the holding can make.
        """
        self.press_to_end(b"N", limit=limit)
        keys = re.findall(r"\[(\w)\]", self.offered())
        if not keys:
            raise SystemExit(f"{self.door.name} offered no key on its last page "
                             f"at {self.size}:\n{self.offered()!r}")
        self.press(keys[-1].encode())

    def press_when_offered(self, key: bytes, *, limit: int = 24) -> None:
        """Page forward until the screen offers `key`, then press it.

        How many entries fit a page depends on the terminal, so a walk can name
        the entry it wants but never the page the entry is on: the scene hub
        offers all seven at 80x24 and four at 40x12.
        """
        wanted = f"[{key.decode()}]"
        for _ in range(limit):
            if wanted in self.offered():
                self.press(key)
                return
            before = last_screen(self.read())
            self.press(b"N")
            if last_screen(self.read()) == before:
                break
        raise SystemExit(f"{self.door.name} never offered {wanted} at {self.size}:\n"
                         f"{self.offered()!r}")

    def press_to_end(self, key: bytes, *, limit: int = 24) -> None:
        """Press `key` until the screen it redraws stops changing.

        A paging key on the last page still redraws -- the door clears and draws
        the same page again -- so "stopped changing" is the only signal for "this
        is the end", and it is the same end at every terminal size.
        """
        for _ in range(limit):
            before = last_screen(self.read())
            self.press(key)
            if last_screen(self.read()) == before:
                return

    def read(self) -> str:
        with self.lock:
            return bytes(self.out).decode("utf-8", "replace")

    def kill(self) -> None:
        """Leave nothing running behind a failed capture."""
        self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for reader in self.readers:
            reader.join(timeout=5)

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
            self.kill()  # reap it, rather than leaving one child per panel behind
            raise SystemExit(f"{self.door.name} never exited after its keys were pressed")
        for reader in self.readers:
            reader.join(timeout=5)
        if code != 0:
            raise SystemExit(f"{self.door.name} exited {code}:\n"
                             f"{bytes(self.err).decode('utf-8', 'replace')[-800:]}")


def capture(door: pathlib.Path, state: pathlib.Path, keys: bytes, width: int, height: int,
            info_extra: dict, *, expect: bool = True) -> str:
    """Drive a walk and return the screen it is looking at when it is done."""
    running = Door(door, state, width, height, info_extra)
    try:
        running.settle()
        index = 0
        while index < len(keys):
            key, suffix = keys[index:index + 1], keys[index + 1:index + 2]
            if suffix == b"*":
                running.press_to_end(key)
                index += 2
                continue
            if suffix == b"?":
                running.press_when_offered(key)
                index += 2
                continue
            if key == b"#":
                running.press_first_offered()
                index += 1
                continue
            if key == b"$":
                running.press_last_offered()
                index += 1
                continue
            running.press(key, expect=expect)
            index += 1
        screen = running.read()
    except BaseException:
        # A door that hung or crashed the build must not outlive it, or a failed
        # gallery leaves a process per panel behind.
        running.kill()
        raise
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
    display = extra.get("display")
    if display:
        # Exactly what the door's own display screen writes: one JSON row keyed
        # by the caller, read back by `read_display()`. The connection is closed
        # by hand because `with sqlite3.connect(...)` commits without closing,
        # and on Windows the open handle keeps the capture's directory alive.
        conn = sqlite3.connect(state / "war-dialer.db")
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (f"display:{DROP_USER_ID}", json.dumps(display)))
        finally:
            conn.close()
    return {key: value for key, value in extra.items()
            if key not in ("display_style", "display")}


def base_fixture(door_name: str, door: pathlib.Path, root_dir: pathlib.Path,
                 fresh: bool = False) -> pathlib.Path:
    """One career or world per door, created once and copied by every panel.

    Built in a temporary directory and moved into place only once the door has
    exited cleanly, so an interrupted run never leaves a half-made fixture that
    the next run would accept simply because the directory exists.
    """
    fixture = root_dir / "fixtures" / door_name
    if fresh:
        remove(fixture)
    # A directory is not a fixture; a world or a saves directory inside one is.
    # `--fresh` deletes through a tree the door's SQLite handle may still be
    # holding, and on Windows that can take the files and leave the directory
    # behind -- after which a bare existence check accepted a world with no
    # schema in it and failed every panel with `no such table: meta`.
    if any(fixture.glob("*.db")) or any(fixture.glob("saves")):
        return fixture
    remove(fixture)
    staging = pathlib.Path(tempfile.mkdtemp(prefix="gallery-fixture-"))
    try:
        state = staging / "state"
        state.mkdir()
        # First-launch keys are the one place a key may find nothing to answer.
        capture(door, state, ONBOARDING[door_name], 80, 24, {}, expect=False)
        if door_name in SEED:
            SEED[door_name](door, state)
            capture(door, state, RESUME[door_name], 80, 24, {})
        # Move the state *into* the fixture directory entry by entry rather than
        # moving the directory itself: if Windows would not let the old fixture
        # go, `shutil.move` treats it as a destination and nests the staging
        # directory inside it, and every panel then opens a world with no schema.
        fixture.mkdir(parents=True, exist_ok=True)
        for entry in sorted(state.iterdir()):
            shutil.move(str(entry), str(fixture / entry.name))
    finally:
        remove(staging)
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


def painted(screen: str, width: int, height: int) -> str:
    """The panel's text as one line, read back through the emulator that paints it.

    Checking the door's bytes would not do: a heading is styled, so in the byte
    stream its words are separated by the sequences that colour them, and a
    screen drawn with cursor moves says nothing in the order it was written.
    """
    canvas = term.Screen(width, height)
    canvas.feed(screen)
    rows = ["".join(char for char, _ in row) for row in canvas.cells]
    return " ".join(" ".join(rows).split())


def build(door_name: str, widths: list[int], heights: dict[int, int],
          out_dir: pathlib.Path, fresh: bool) -> pathlib.Path:
    door = ROOT / "src/netbbs/doors/bundled" / f"{door_name}.py"
    if not door.exists():
        raise SystemExit(f"no such bundled door: {door}")
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture = base_fixture(door_name, door, out_dir, fresh)

    shows = SHOWS.get(door_name, {})
    unnamed = [label for label, _ in WALKS[door_name] if label not in shows]
    if shows and unnamed:
        raise SystemExit("every walk must say what its screen shows; "
                         f"{door_name} does not for: {', '.join(unnamed)}")

    shots = [(label, keys, preset, extra, width, heights.get(width, 24))
             for label, keys in WALKS[door_name]
             for preset, extra in PRESETS[door_name].items()
             for width in widths]

    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="gallery-run-"))

    def shoot(shot) -> str:
        _, keys, _, extra, width, height = shot
        work = pathlib.Path(tempfile.mkdtemp(dir=run_dir))
        try:
            state = work / "state"
            shutil.copytree(fixture, state)
            info_extra = apply_preset(state, extra)
            return last_screen(capture(door, state, keys, width, height, info_extra))
        finally:
            remove(work)

    # Each panel is its own door in its own copy of the fixture, so they can be
    # taken at once; the page is assembled from them in order afterwards. They
    # all live under one directory, so a build that dies takes them with it
    # rather than leaving a career per panel in the system temp directory.
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            screens = list(pool.map(shoot, shots))
    finally:
        remove(run_dir)

    # A panel that is not the screen its caption names is worse than a missing
    # one: it reads as a review of a screen nobody has looked at.
    wrong = [f"  {label} at {width}x{height} {preset}: no {shows[label]!r} on screen"
             for (label, _, preset, _, width, height), screen in zip(shots, screens)
             if label in shows and shows[label] not in painted(screen, width, height)]
    if wrong:
        raise SystemExit(f"{door_name}: a walk did not end on the screen it names:\n"
                         + "\n".join(wrong))

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
