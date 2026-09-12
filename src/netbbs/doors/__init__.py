"""
Native door-game support (issue #172, Phase 7's first vertical).

`netbbs.doors.registry` is the SysOp-facing catalogue -- a door is
registered once (an executable path, optional fixed args, a permission
gate, optional Community attachment), mirroring `netbbs.files.areas`'
shape closely since both are "a thing a caller can be granted access to,
scoped to a Community" (see that module's own docstring). `netbbs.doors.
runtime` is the actual per-launch execution engine -- see its own
docstring for the locked sandbox design (issue #63/#167: same-OS-user
subprocess isolation, no containers, a drop-file-shaped v1 API surface,
trusted/unsanitized door output).
"""

from netbbs.doors.registry import (
    Door,
    DoorError,
    create_door,
    custom_doors_dir,
    delete_door,
    get_door,
    get_door_by_name,
    list_doors,
    update_door,
)

__all__ = [
    "Door",
    "DoorError",
    "create_door",
    "custom_doors_dir",
    "delete_door",
    "get_door",
    "get_door_by_name",
    "list_doors",
    "update_door",
]
