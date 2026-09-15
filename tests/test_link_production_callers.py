"""A ratchet against Link code that is implemented, tested, and unreachable.

Issue #464 found one queue helper whose only callers were under ``tests/``.
Issue #584 found the same shape in two whole subsystems the design document
treats as load-bearing: the trust wire could verify, store, re-serve and
enforce on a peer's signed trust objects but never issue one, and remote
identity attestation was unwired in *both* directions while the Profile screen
offered a caller a switch that said their verified age or name was shared
across the Link.

Neither was caught by the suite, because both subsystems test the same way:
mint the object by calling the builder directly, then drive the receiving
half.  That is a green end-to-end test of one direction which reads like a
round trip, and it stays green forever while production never calls the
builder at all.

This test is the cheap check that would have caught all three: a public
function in ``src/netbbs/link/`` that nothing in ``src/`` references is, at
best, not yet wired up.  It is deliberately *weak* -- it does not attempt
reachability from an entry point, which would flag every helper legitimately
called only from the sync loop.  One reference anywhere in ``src/`` is enough.

``ALLOWED`` carries the names that were already in this state when the check
landed, each pointing at the issue that owns it, so a new instance fails the
build while the existing ones are worked through.  ``test_allowlist_has_no_
stale_entries`` forces an entry back out as soon as it is wired up, so the
list can only shrink.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
LINK = SRC / "netbbs" / "link"


# Every name here is implemented and tested but referenced nowhere in `src/`
# outside its own definition.  The value is the issue that owns wiring it up
# (or deciding it should go).  Add nothing to this list: a new entry means a
# subsystem shipped with no production caller, which is the thing the check
# exists to stop.
ALLOWED: dict[str, str] = {
    # Issue #589 -- the trust wire is receive-only.  A node can verify, store,
    # re-serve and enforce on a peer's signed trust objects; it can never
    # issue one, so `link_trust_wire_objects` is empty on every node in
    # production and §12.7's pull protocol correctly returns nothing.
    "build_trust_signal": "#589",
    "build_trust_vouch": "#589",
    "build_trust_revocation": "#589",
    "activate_reproduced_digest_signal": "#589",
    "fetch_trust_evidence": "#589",
    "record_activity": "#589",
    "clear_local_observation": "#589",
    "recompute_all_trust_states": "#589",
    # Issue #584 -- named by the same scan, not yet triaged to a subsystem.
    "enqueue_work_item": "#584",
    "frame_is_relay_traffic": "#584",
    "get_remote_file": "#584",
    "rotate_realtime_transport_key": "#584",
}


def _public_definitions() -> dict[str, list[tuple[pathlib.Path, ast.stmt]]]:
    """Every module-level public function defined under `src/netbbs/link/`."""
    found: dict[str, list[tuple[pathlib.Path, ast.stmt]]] = {}
    for path in sorted(LINK.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                found.setdefault(node.name, []).append((path, node))
    return found


def _references() -> dict[str, set[tuple[pathlib.Path, int]]]:
    """Every name and attribute reference anywhere in `src/`, with location.

    Collection is AST-based rather than a textual search for ``name(`` because
    this codebase dispatches callables by reference -- ``await lane.run(
    link_board, db, ...)`` -- and a call-site grep reports most of ``link/``
    as uncalled.

    ``ast.alias`` is deliberately *not* collected, so an import is not itself
    a reference.  Without that, a name a module imports and never uses, or one
    a package re-exports and nobody calls, would look wired up: `get_remote_
    file` is exactly that case, imported at `transport.py` and used nowhere.
    """
    found: dict[str, set[tuple[pathlib.Path, int]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.setdefault(node.id, set()).add((path, node.lineno))
            elif isinstance(node, ast.Attribute):
                found.setdefault(node.attr, set()).add((path, node.lineno))
    return found


def _unreferenced() -> dict[str, list[str]]:
    """Public `link/` functions with no reference outside their own body."""
    definitions = _public_definitions()
    references = _references()
    result: dict[str, list[str]] = {}
    for name, sites in sorted(definitions.items()):
        spans = [(path, node.lineno, node.end_lineno) for path, node in sites]
        outside = {
            (path, line)
            for path, line in references.get(name, set())
            if not any(
                path == own_path and own_start <= line <= own_end
                for own_path, own_start, own_end in spans
            )
        }
        if not outside:
            result[name] = [
                f"{path.relative_to(SRC).as_posix()}:{node.lineno}" for path, node in sites
            ]
    return result


def test_every_public_link_function_has_a_production_caller() -> None:
    unreferenced = _unreferenced()
    new = {name: where for name, where in unreferenced.items() if name not in ALLOWED}
    if new:
        listing = "\n".join(f"  {name} ({', '.join(where)})" for name, where in sorted(new.items()))
        pytest.fail(
            "public function(s) in src/netbbs/link/ referenced nowhere in src/ outside "
            "their own definition:\n"
            f"{listing}\n\n"
            "Implemented, tested, and unreachable is the shape of issues #464 and #584. "
            "Wire it to a production caller, delete it, or -- only if an issue owns "
            "wiring it up -- add it to ALLOWED in this file with that issue's number."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted name that got wired up has to come back off the list.

    This is the half that makes the list a ratchet rather than a graveyard:
    without it, a name could be wired up years ago and still sit here implying
    its issue is open.
    """
    unreferenced = _unreferenced()
    wired = sorted(name for name in ALLOWED if name not in unreferenced)
    assert not wired, (
        "ALLOWED names that now have a production caller -- remove them from "
        f"ALLOWED (and close the issue if it is done): {wired}"
    )


def test_allowlisted_names_still_exist() -> None:
    """A name deleted outright also has to come off the list."""
    definitions = _public_definitions()
    missing = sorted(name for name in ALLOWED if name not in definitions)
    assert not missing, (
        f"ALLOWED names no longer defined in src/netbbs/link/: {missing}"
    )
