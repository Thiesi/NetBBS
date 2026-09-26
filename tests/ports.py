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

# A Link listener configured without an explicit `realtime_port` binds a
# second one at `port + 1000`, so a worker's range is its written ports
# (12391-12444 today) *and* those plus 1000. The stride has to clear both,
# or worker N's real-time listeners land on worker N+10's base ports.
_WORKER_STRIDE = 2000
# 12444 + 1000 + 25 * 2000 stays below 65536. More workers than that reuse
# a range, which is only a risk on a machine with more than 26 of them.
_WORKER_RANGES = 26


def port(base: int) -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    index = int(worker[2:]) if worker.startswith("gw") and worker[2:].isdigit() else 0
    return base + (index % _WORKER_RANGES) * _WORKER_STRIDE
