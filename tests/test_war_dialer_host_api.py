"""Pin the names NetBBS reaches into the vendored War Dialer for (issues #1227, #1228).

The game lives in https://github.com/Thiesi/WarDialer. A release that renamed
one of these would break NetBBS's backup or SysOp console rather than the game,
so the game's own suite cannot notice."""
import ast
from pathlib import Path

from netbbs.doors.bundled import war_dialer as wd


def test_public_host_api_exists():
    for name in ("administer_world", "inspect_world", "world_session", "_event_plain"):
        assert callable(getattr(wd, name))
    assert issubclass(wd.WorldStateError, Exception)


def test_admin_and_backup_use_only_the_pinned_boundary():
    root = Path(__file__).resolve().parents[1] / "src/netbbs"
    expected = {
        "doors/war_dialer_admin.py": {"administer_world", "WorldStateError"},
        "backup.py": {"inspect_world", "world_session", "WorldStateError"},
        "net/admin_flow.py": {"_event_plain", "WorldStateError"},
    }
    for path, names in expected.items():
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
        actual = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                  and isinstance(node.value, ast.Name) and node.value.id == "wd"}
        assert actual == names
