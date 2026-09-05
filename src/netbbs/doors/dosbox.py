"""DOSBox-X COM1 adapter. Emulator and optional FOSSIL are installed by the SysOp."""
from pathlib import Path


def prepare_dosbox(door, directory: Path, descriptor: int, node: int):
    profile = door.profile.validate()
    install = Path(profile.install_dir).resolve()
    substitutions = {"node": str(node), "node_dir": "D:\\" +
                     (profile.drop_subdir + "\\" if profile.drop_subdir else "")}
    command = profile.options["command"].format_map(substitutions)
    fossil = profile.options.get("fossil", "").format_map(substitutions)
    success = set(profile.options.get("success_exit_codes", [0]))
    # A separate batch file permits exact status branches without IF's eager
    # redirection side effect. LORD uses 255 for a successful normal return.
    branches = []
    for code in range(255, -1, -1):
        if code in success or code - 1 in success:
            branches.append(f"if errorlevel {code} goto {'ok' if code in success else 'failed'}")
    batch = ["@echo off", fossil, command, *branches, ":failed", "echo failed > D:\\EXIT.ERR", "goto drain",
             ":ok", "echo returned > D:\\RETURN.OK", ":drain", "D:\\DRAIN.COM"]
    (directory / "RUN.BAT").write_bytes(("\r\n".join(batch) + "\r\n").encode("cp437"))
    # Let the emulator finish its serial transmit events after the game exits.
    # mov ax,8600h; xor cx,cx; mov dx,50000; int 15h; mov ax,4c00h; int 21h
    (directory / "DRAIN.COM").write_bytes(bytes.fromhex("b8008631c9ba50c3cd15b8004ccd21"))
    # Fixed mounts, no host configuration discovery or operator's ambient autoexec.
    # Secure mode is enabled AFTER mounting both narrow directories.
    config = f'''[sdl]
fullscreen=false
output=surface
autolock=false
waitonerror=false
showmenu=false
mapperfile=mapper.map
[dosbox]
machine=svga_s3
memsize=16
[render]
frameskip=10
scaler=none
[cpu]
core=normal
cputype=auto
cycles=fixed 10000
[mixer]
nosound=true
[midi]
mpu401=none
mididevice=none
[sblaster]
sbtype=none
oplmode=none
[gus]
gus=false
[speaker]
pcspeaker=false
tandy=off
disney=false
[joystick]
joysticktype=none
[serial]
serial1=nullmodem inhsocket:1 transparent:1 telnet:0 rxdelay:1000 txdelay:0
serial2=disabled
serial3=disabled
serial4=disabled
[parallel]
parallel1=disabled
parallel2=disabled
parallel3=disabled
[ipx]
ipx=false
[ne2000]
ne2000=false
[autoexec]
@echo off
mount c "{install}"
mount d "{directory}"
c:
config -securemode
call D:\\RUN.BAT
exit
'''
    path = directory / "dosbox.conf"
    path.write_text(config, encoding="utf-8")
    return ([door.executable_path, "-conf", str(path), "-socket", str(descriptor), "-fastlaunch", "-nogui"],
            {"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy", "HOME": str(directory),
             "XDG_CONFIG_HOME": str(directory), "XDG_DATA_HOME": str(directory)})
