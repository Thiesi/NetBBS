"""
Waiting out a `netbbs.link.sync.run_link_sync` pass in tests that drive a
real loopback transport -- shared by `test_link_sync` and
`test_link_end_to_end`, which each carried their own copy before.
"""

from __future__ import annotations

import asyncio


def parked_between_passes(task: asyncio.Task) -> bool:
    """Whether `run_link_sync` has finished a pass and is waiting out its
    interval: its own coroutine is directly awaiting `asyncio.sleep`, or
    `asyncio.wait_for` on its `stop_event`. Anything else it awaits is
    part of a pass."""
    awaited = getattr(task.get_coro(), "cr_await", None)
    code = getattr(awaited, "cr_code", None)
    return code is not None and code.co_name in ("sleep", "wait_for")


async def run_sync_briefly(
    coro_task: asyncio.Task, *, settle: float = 0.2, pass_timeout: float = 60.0
) -> None:
    """Lets a run_link_sync task run for at least `settle` seconds, then
    until the pass in progress has finished, then cancels it cleanly --
    mirrors how netbbs.__main__ cancels this same task on node shutdown.

    Waiting for the pass rather than for a fixed time is what makes the
    assertions after it deterministic: on a loaded machine (a parallel
    pytest-xdist run) one pass over a real loopback transport can take
    longer than any fixed `settle`, and cancelling mid-pass left the test
    asserting on half-synced state. `settle` stays the minimum, so a test
    that relies on several short-interval passes still gets them."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(settle)
    deadline = loop.time() + pass_timeout
    while not coro_task.done() and not parked_between_passes(coro_task):
        if loop.time() >= deadline:
            coro_task.cancel()
            await asyncio.gather(coro_task, return_exceptions=True)
            raise AssertionError(
                f"run_link_sync did not finish its pass within {pass_timeout}s -- "
                "a hung pass, not a slow machine"
            )
        await asyncio.sleep(0.01)
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass
