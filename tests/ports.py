"""
Fixed loopback ports for tests that need a known port up front (a node
started from config, then dialled), made safe under `pytest -n auto`.

Each xdist worker shifts every port by its own offset, so two workers can
never bind the same one; within a worker tests run one at a time, so a
port reused across tests there is as safe as it was serially. Without
xdist the offset is zero and every port is the one written in the test.
"""

from __future__ import annotations

import os

# Wider than the span of ports any one file uses, so worker N's range never
# reaches worker N+1's.
_WORKER_STRIDE = 100


def port(base: int) -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    index = int(worker[2:]) if worker.startswith("gw") and worker[2:].isdigit() else 0
    return base + index * _WORKER_STRIDE
