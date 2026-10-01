"""Wait for a condition instead of a fixed time (issue #999).

A test that sleeps a fixed 0.1 s and then asserts that something happened
fails whenever the machine is busy -- a full `pytest -n auto` run competes
with every other worker for the CPU, and other sessions' suites often run
alongside. Poll for the condition with a generous bound instead: the loop
leaves as soon as the condition holds, so the bound costs nothing on an idle
machine.

Negative checks ("nothing more arrives") still need a short settle sleep;
this is for positive ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def eventually(predicate: Callable[[], object], *, timeout: float = 30.0, interval: float = 0.01) -> bool:
    """True once `predicate()` is truthy; False if `timeout` seconds pass first."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
    return True
