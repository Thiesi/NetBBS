"""Classic metadata files (CRLF); private session data is never an input.

Format references: Synchronet ref:door.sys, WWIV chains/doors, DOOR32 v1.0.
Unknown personal/statistical fields are empty or zero, never invented identities.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from netbbs.doors.profiles import DoorProfile


def _text(value, maximum=80):
    return "".join(c for c in str(value or "") if c >= " " and c != "\x7f")[:maximum]


def drop_file_bytes(profile: DoorProfile, info: dict, node: int, *, descriptor=0,
                    now: datetime | None = None) -> dict[str, bytes]:
    profile.validate()
    now = now or datetime.now()
    date = now.strftime("%m/%d/%y")
    clock = now.strftime("%H:%M")
    handle = _text(info.get("handle"), 35)
    system = _text(info.get("node_name"), 35)
    uid = int(info.get("user_id", 0))
    width = profile.width or int(info.get("terminal_width", 80))
    height = profile.height or int(info.get("terminal_height", 24))
    seconds, minutes = profile.time_limit, profile.time_limit // 60
    security, baud = profile.security_level, profile.baud
    serial = profile.adapter == "dosbox"
    com = 1 if serial else 0
    # These are door-visible node paths, never the NetBBS database directory.
    node_path = "D:\\" + (profile.drop_subdir + "\\" if profile.drop_subdir else "") if serial else ""
    door_sys = [f"COM{com}:", baud, 8, node, baud, "Y", "N", "N", "N", handle,
                "", "", "", "", security, 0, date, seconds, minutes, "GR", height,
                "N", "", 0, "12/31/99", uid, "N", 0, 0, 0, 0,
                "", node_path, node_path, "SysOp", handle, "00:00", "Y", "N", "Y", 7,
                0, date, clock, clock, 0, 0, 0, 0, "", 0, 0]
    first, _, last = handle.upper().partition(" ")
    dorinfo = [system.upper(), "SYSOP", "", f"COM{com}", f"{baud} BAUD,N,8,1", 0,
               first, last, "", 1, security, minutes, -1]
    chain = [uid, handle, handle, "", 0, "", "0.00", date, width, height, security,
             0, 0, 1, int(serial), f"{seconds}.00", node_path, node_path, "", baud, com,
             system, "SysOp", now.hour * 3600 + now.minute * 60 + now.second, 0, 0, 0, 0, 0,
             "8N1", baud, 0]
    door32 = [2 if descriptor else 0, descriptor, baud, system, uid, handle, handle,
              security, minutes, 1, node]
    variants = {"DOOR.SYS": door_sys, "DORINFO1.DEF": dorinfo, "DORINFOx.DEF": dorinfo,
                "CHAIN.TXT": chain, "DOOR32.SYS": door32}
    result = {}
    encoding = "cp437" if profile.encoding in ("cp437", "raw") else "utf-8"
    for name in profile.drop_files:
        lines = variants[name]
        if name == "DORINFOx.DEF":
            suffix = str(node) if node < 10 else "0" if node == 10 else chr(ord("a") + node - 11)
            name = f"DORINFO{suffix}.DEF"
        name = name.lower() if profile.filename_case == "lower" else name.upper()
        result[name] = ("\r\n".join(str(x) for x in lines) + "\r\n").encode(encoding, errors="replace")
    return result


def write_drop_files(directory: Path, profile: DoorProfile, info: dict, node: int, *, descriptor=0):
    target = directory / profile.drop_subdir
    target.mkdir(exist_ok=True, mode=0o700)
    for name, data in drop_file_bytes(profile, info, node, descriptor=descriptor).items():
        (target / name).write_bytes(data)
    return target
