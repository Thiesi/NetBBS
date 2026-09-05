# Door games: setup and compatibility

NetBBS supplies the integration, not third-party games or their execution
environments. The bundled games remain available without a legacy profile.
Existing registrations keep their JSON metadata and UTF-8 stdio API.

**MANUAL — outside NetBBS** labels below identify work the SysOp must do on
the host or in another program. NetBBS never installs packages, downloads
games/drivers, creates tunnel accounts, or edits host configuration.

## What is supported, and what has been verified?

| Profile | Execution environment | Current verification |
| --- | --- | --- |
| Native stdio | Matching host binary/interpreter | Real processes, persistent scores, metadata and cleanup on NetBSD 11 and Debian 13 amd64; Windows development smoke |
| Native PTY | POSIX host build | NetBSD and Debian controlling terminal, `isatty`, TERM, 80x25 and process-group tests |
| Native DOOR32 socket | POSIX socket-aware host build | NetBSD and Debian inherited private descriptor and persistence tests |
| DOS UART/FOSSIL | External DOSBox-X | NetBSD 11 and Debian 13 amd64, upstream `2025.02.01 (SDL2)` with the repository socket/libpng patches; real COM1 input/output, CP437 block characters, BNU 1.70 |
| LORD 4.07 DOS demo | DOSBox-X + BNU 1.70 | NetBSD player creation; NetBSD/Debian persistent town-menu re-entry and normal quit |
| Global War 2.7 DOS demo | DOSBox-X built-in UART | NetBSD game creation; NetBSD/Debian saved waiting-game re-entry and normal quit; a full three-player match is not certified |
| TradeWars 2002 3.09 DOS demo | DOSBox-X + BNU 1.70 | NetBSD player/ship/planet creation; NetBSD/Debian persistent universe re-entry and normal quit |
| Remote RFC 1282 | Operator-run SSH/TLS tunnel, provider access | Real loopback handshake tests; live third-party accounts not certified |

On both NetBSD and Debian, native and DOS serial fixtures pass over real Telnet, SSH and
WebSocket connections, including return to the menu. The DOS fixture also
verifies two simultaneous nodes and scores persisting across two launches;
this does not certify a proprietary game's shared-data locking. Bundled doors
pass over real WebSockets, and the browser shim is executed by
Node.js for streaming decoder/mode tests. These are automated protocol checks,
not screenshots of every game on every terminal client.
Real-emulator crash, timeout, caller disconnect and node-shutdown tests also
verify reaping, released node leases and removal of scratch files without
deleting persistent installation data.
The three real-game saved-state/normal-quit checks pass with the patched
2025.02.01 runtime on both hosts. Initial player/game creation and setup recipes
were first verified on NetBSD with pkgsrc `dosbox-x-0.84.3nb10`
(`2022.09.0 SDL2`); the saved data remains usable with the patched replacement
on NetBSD and with disposable copies transferred to Debian.

**DOSBox-X build warning:** Debian 13's unmodified `2025.02.01+dfsg-3`
package aborts during inherited-socket cleanup. The upstream `0.84.3` source
has the same double-free despite the NetBSD launch/quit checks above passing.
Use a socket-ownership-fixed build, not merely a version which appears to
quit normally. See [the manual source fix](#dosbox-x-inherited-socket-source-fix).
The patched build passes the complete door/web certification suite on both
NetBSD 11 and Debian 13 amd64.
Run the tests below on the actual distribution/emulator before offering
it to callers. Windows is a development target for stdio/browser tests, not
a supported DOS/PTY/socket host. Templates are starting configurations, not
a promise that arbitrary versions or all historical doors work.

All legacy templates default to one session. Do not enable multiple sessions
until you have tested the specific game's licensing and shared-file locking.
Use a single registry entry per installation, on a local filesystem; multiple
NetBBS state directories do not share node leases. Back up persistent game
files while the game is stopped, not its disposable node/drop directory.

## Trust and filesystem layout

Native doors run as the NetBBS service account. They can access everything
that account can access, including NetBBS keys, configuration and database.
The environment and drop files do not disclose these secrets, but that is
not a security sandbox. Only install code you trust. Never accept executable
doors uploaded by callers. CPU/memory/time/process limits are resource
controls, not filesystem or network isolation.

For DOS, only the game installation is mounted as `C:` and the private launch
directory as `D:`. Secure mode follows those mounts; audio and network devices
are disabled. This is a useful restriction on DOS programs, not a guarantee
against emulator vulnerabilities. Do not put BBS secrets or symlinks to other
host data in the game installation. Run NetBBS unprivileged, never as root.

- Installation directory: persistent, operator-owned game executables,
  configuration, player databases and scores. Example `/var/games/netbbs/lord`.
- Node directory: private temporary directory allocated by NetBBS, with JSON
  metadata and selected classic drop files. Removed after every launch.
- Lease directory: `door-nodes` beside the NetBBS database; small advisory-lock
  files persist, but the OS releases locks at process exit/reboot. Do not
  remove lock files while NetBBS is running.

**MANUAL — outside NetBBS:** create the installation directories and give the
actual service account read/write/search access. For a service user/group both
named `netbbs`, for example (substitute your real account names):

```sh
sudo install -d -m 750 -o netbbs -g netbbs /var/games/netbbs
sudo install -d -m 750 -o netbbs -g netbbs /var/games/netbbs/lord
```

Install each game in its own directory. Do not recursively change ownership
of your home, NetBBS state directory or a shared game collection.
Run game extraction, setup and configuration-copy commands as that installation's
owner, normally the NetBBS service account. For example, prefix a command with
`sudo -u netbbs` when administering a separate `netbbs` account, using a checkout
and interpreter it can read and execute. Package installation may need root;
the emulator, game and NetBBS itself must not run as root.

## Register and test inside NetBBS

1. Open **SysOp → Doors**, register a door using the existing draft editor,
   and initially set its minimum play level to SysOp (255).
2. Open that door's **Compatibility** screen. Select a setup template or
   import an edited repository JSON file. Templates live in
   [`src/netbbs/doors/presets`](../src/netbbs/doors/presets) and are installed
   with the Python package. The import file may contain the full template
   (`executable_path`, `args`, `profile`) or just the profile object.
3. For local profiles, edit the executable and persistent installation paths. Native argv is a
   list, never a shell command. Supported substitutions are `{node_dir}`,
   `{install_dir}`, `{node}`, `{door_sys}`, `{door32}`. Quote argv entries
   with spaces; use forward slashes in Windows development paths.
   Remote presets use the placeholder executable `remote`; no executable is
   launched. Configure their destination and credentials in adapter options.
4. Set the drop formats, casing, endpoint, encoding and geometry required by
   the game. The DOS templates use CP437, COM1, 38400 baud, 80x25 and 1 GiB
   address-space ceiling. This ceiling includes the emulator's host shared
   libraries, not just its 16 MiB emulated RAM. Native default: 256 MiB.
5. **Check setup** reports static problems. For DOS, run **Emulator capability
   probe** to verify headless startup, inherited COM1, CP437 echo and optional
   FOSSIL using NetBBS's own fixture, without launching the game. It requires
   confirmation and uses a temporary installation. **Test as SysOp** asks for one
   explicit confirmation, then starts the actual game/service. A test can
   modify game data even if you later leave the draft without saving.
6. Read its test result/exit code and diagnostic excerpt. Save explicitly.
   Back discards configuration edits. The door detail's **Last diagnostic**
   retains at most 8 KiB from the latest run; callers do not receive stderr.
7. Test each enabled caller transport, including return to the door picker,
   and only then lower the minimum play level.

Also check existing caller handles against the particular game's own name
length and character rules. NetBBS supplies the caller ID and handle but does
not migrate, rename or reconcile an existing third-party player database.

Classic metadata formats are `DOOR.SYS` (52 lines), `DORINFO1.DEF` or numbered
`DORINFOx.DEF`, `CHAIN.TXT` and `DOOR32.SYS`. Output is CRLF, in the configured
encoding, without passwords or session credentials. For node 10, the
DORINFO suffix is `0`; nodes 11–36 use `a`–`z` subject to filename casing.
Use a single DOS-safe `drop_subdir` only if your game needs it; update the
game's configured `D:\` path accordingly. Never copy live drop files into
a shared persistent directory.

NetBBS terminals speak UTF-8: the adapter converts CP437 game output and
keyboard input. Configure native Telnet/SSH clients accordingly. `raw` means
no codec conversion and is not a browser profile. Web door mode preserves
escape sequences and uses an incremental UTF-8 decoder; classic fixed-size
screens return to browser-fit geometry after play. Telnet/SSH terminals must
already be at least the configured size; NetBBS does not resize their windows.
PTY geometry is set at launch; dynamic terminal resizing inside local games
is not currently forwarded.

## Native doors

**MANUAL — outside NetBBS:** obtain an appropriate host build from its author,
verify its provenance/license, install it and any interpreter/runtime, and
read its communications-mode instructions. A Linux ELF executable is not a
NetBSD executable. Build from source on NetBSD or obtain a NetBSD build;
NetBSD's optional Linux emulation is not a supported backend here.

Choose the matching native template:

- `native-stdio.json`: redirected input/output; executable plus drop-file argv.
- `native-pty.json`: programs requiring a controlling terminal and `TERM=ansi`.
- `native-door32.json`: POSIX socket-mode DOOR32; lowercase `door32.sys`, with
  a private inherited descriptor, not the caller's actual socket. The program
  must support POSIX descriptors, not Windows Winsock handles.

For an interpreter, use its absolute executable and put the absolute script
path first in argv. Java example: executable `/usr/pkg/java/openjdk17/bin/java`,
argv `-jar /var/games/netbbs/game/game.jar {node_dir}`; adjust memory after
checking the JVM's reservation needs. Do not assume that JVM path/package
exists on your host. Install the runtime recommended by the game's author.

An optional `runner` is a fixed argv prefix, e.g. an operator-authored
`["/usr/local/libexec/netbbs-door-wrapper"]` which finally execs its argv.
**MANUAL — outside NetBBS:** write/audit that wrapper and configure any
container/chroot/dedicated-account/VM it uses. It must preserve required
descriptors, path mappings and process ownership; validate disconnects.
No privileged helper, containment tool or universal container recipe is
installed by NetBBS. Wine/Win32 remains experimental and untested.

## DOS prerequisites

**MANUAL — outside NetBBS, NetBSD:** install DOSBox-X from pkgsrc (binary
package if available for your architecture/repository):

```sh
sudo pkgin install dosbox-x unzip unarj
command -v dosbox-x
/usr/pkg/bin/dosbox-x -version
```

If unavailable, use the normal pkgsrc build procedure for
[`emulators/dosbox-x`](https://cdn.netbsd.org/pub/pkgsrc/current/pkgsrc/emulators/dosbox-x/README.html).
Do not install NetBBS from pkgsrc; its official distribution remains GitHub.

**MANUAL — outside NetBBS, Linux:** obtain DOSBox-X using its
[official platform installation instructions](https://dosbox-x.com/wiki/Guide%3ALinux-installation),
and set the profile executable to `command -v dosbox-x`. Do not substitute
plain DOSBox or DOSBox Staging without certifying the inherited-socket
interface. The adapter requires `-socket FD`, `nullmodem inhsocket:1`,
transparent non-Telnet serial, dummy SDL video/audio and secure mode.
Debian 13 supplies a [DOSBox-X package](https://packages.debian.org/trixie/dosbox-x):
install it manually with `sudo apt-get install dosbox-x`. Other distributions
and sandboxed application packages may differ; run the capability probe.
The unmodified Debian package is not sufficient for inherited COM1: apply
the source fix below or obtain a downstream build containing an equivalent fix.

### DOSBox-X inherited-socket source fix

**MANUAL — outside NetBBS:** the repository includes
[`dosbox-x-inherited-socket.patch`](../examples/doors/dosbox-x-inherited-socket.patch)
for upstream `dosbox-x-v2025.02.01`. It makes SDL_net the sole owner of the
inherited socket structure, using its matching allocator, and retains cleanup
ownership when socket initialization fails. The original destructor deletes
the structure before passing it to
[`SDLNet_TCP_Close`](https://wiki.libsdl.org/SDL2_net/SDLNet_TCP_Close), which
also frees it. Do not disable allocator checks or turn emulator crashes into
successful game results. A passing capability probe cannot exclude an
allocator-dependent memory error on every platform.

This is an external emulator source change, not a NetBBS installation action.
The two external patch files are provided under GPL-2.0-or-later, matching
the DOSBox-X source they modify; NetBBS's own license is unchanged.
Review the patches and the upstream license before building. Keep the packaged
emulator installed until the replacement passes the checks below; give the
new binary its own installation prefix and select its absolute path in the
NetBBS profile. Do not overwrite the package-managed executable.

The runtime-only build is verified on NetBSD 11 and Debian 13 amd64.
The patch alone is not a certified executable.

1. **MANUAL — install build prerequisites.** On Debian 13:

   ```sh
   sudo apt-get install curl patch nasm g++ make autoconf automake libtool pkg-config libsdl2-dev libsdl2-net-dev zlib1g-dev libpng-dev
   ```

   On NetBSD, use the base compiler plus pkgsrc tools/libraries:

   ```sh
   sudo pkgin install curl bash nasm autoconf automake libtool gmake pkgconf SDL2 SDL2_net png
   export PATH=/usr/pkg/bin:$PATH
   ```

2. **MANUAL — download and review source.** Set `netbbs_checkout` to your
   repository checkout, not the installed Python package. Use a fresh build
   directory and keep its printed path until validation is complete:

   ```sh
   netbbs_checkout=/absolute/path/to/NetBBS
   door_build=$(mktemp -d)
   echo "$door_build"
   cd "$door_build"
   curl -fL https://codeload.github.com/joncampbell123/dosbox-x/tar.gz/refs/tags/dosbox-x-v2025.02.01 -o dosbox-x.tar.gz
   ```

   Check with `sha256 dosbox-x.tar.gz` (NetBSD) or
   `sha256sum dosbox-x.tar.gz` (Linux). The tested upstream archive is
   `3a6fdfd659bb05db82bf2d850af806f666562cce9a37609fd33b59f7e4bd8fa4`.
   Stop if it differs; do not apply this recipe to an unidentified revision.

   ```sh
   tar -xzf dosbox-x.tar.gz
   cd dosbox-x-dosbox-x-v2025.02.01
   patch -p1 < "$netbbs_checkout/examples/doors/dosbox-x-inherited-socket.patch"
   patch -p1 < "$netbbs_checkout/examples/doors/dosbox-x-png-configure.patch"
   bash ./autogen.sh
   ```

   The second patch permits pkgsrc's `libpng16` name without changing any
   system library links. Keep upstream's `vs/sdl` sources even for an SDL2
   build: they contain required CD-ROM compatibility headers.

3. **MANUAL — configure and compile unprivileged.** On NetBSD first set:

   ```sh
   export CPPFLAGS=-I/usr/pkg/include
   export LDFLAGS=-Wl,-rpath,/usr/pkg/lib
   export LIBS="-lcompat -lrt"
   ```

   Then, on either platform:

   ```sh
   ./configure --prefix=/opt/netbbs-dosbox-x --enable-sdl2 --disable-optimize \
     --disable-x11 --disable-opengl --disable-freetype --disable-printer \
     --disable-xbrz --disable-mt32 --disable-dynamic-core --disable-screenshots \
     --disable-libslirp --disable-libfluidsynth --disable-avcodec --disable-alsa-midi
   ```

   Build with `gmake -j1 res_DATA=` on NetBSD or `make -j1 res_DATA=` on
   Linux. `res_DATA=` selects the runtime-only build without optional desktop
   font assets, matching the tested headless configuration. Do not continue
   after a build error. Test the resulting absolute `src/dosbox-x` path
   before installation, using the capability probe and tests below.

4. **MANUAL — install only after verification.** For the English serial-door
   runtime, install the standalone binary into the separate prefix:

   ```sh
   sudo install -d /opt/netbbs-dosbox-x/bin
   sudo install -m 755 src/dosbox-x /opt/netbbs-dosbox-x/bin/dosbox-x
   ```

   This is a runtime-only installation, not a complete translated desktop
   DOSBox-X installation. The checked-in NetBBS configuration uses dummy
   video/audio and built-in VGA fonts, not the optional desktop font files.
   Set the door profile's executable to `/opt/netbbs-dosbox-x/bin/dosbox-x`,
   run the capability probe again as the actual NetBBS service account, then
   test the game. Keep the patches/build version with your operational notes;
   updating or rebuilding the emulator is also manual.

### One-time DOS game setup

Runtime needs no X display. One-time game setup utilities usually do need a
local DOS screen. **MANUAL — outside NetBBS:** copy and edit
[`dosbox-setup.conf`](../examples/doors/dosbox-setup.conf), replacing its one
mount path, then run on a graphical desktop:

```sh
dosbox-x -conf /absolute/path/to/your-setup.conf
```

On a headless server, perform setup on a trusted workstation using a copy of
the game directory, then copy the configured installation back while no game
is running. Keep game drop paths DOS-visible (`D:\`), not workstation host
paths. This GUI setup is separate from the generated headless runtime config.

### Optional FOSSIL driver (required by LORD/TradeWars templates)

**MANUAL — outside NetBBS:** obtain BNU 1.70 or another driver your game
supports, read its license, and install it in that game's directory. The
provided templates use `BNU.COM /L0=38400` (driver port 0 is COM1). The
[UUPC distributor archive](https://www.uupc.net/pub/uupc/tools/bnu170.zip)
contains BNU and its documentation. BNU permits noncommercial/nonprofit use
under its stated conditions; commercial use/distribution may need separate
permission. NetBBS does not ship the driver. Do not assume another driver's
switches match BNU's.

On NetBSD use **pkgsrc** `/usr/pkg/bin/unzip`, not `/usr/bin/unzip` or tar:
the BNU archive uses legacy ZIP Implode compression unsupported by those
base tools in the tested environment. Inspect the listing before extraction:

```sh
/usr/pkg/bin/unzip -l /path/to/bnu170.zip
/usr/pkg/bin/unzip /path/to/bnu170.zip -d /path/to/bnu-staging
```

Read `BNU.DOC`, then copy only the licensed runtime files required by the
game into its installation. Never put a downloaded emulator/driver in a
caller-writable upload directory and execute it from there.

## LORD 4.07 DOS

**MANUAL — outside NetBBS:** obtain the DOS demo from the
[publisher's LORD page](https://www.gameport.com/bbs/lord.html),
[lord407.zip](https://www.gameport.com/demos/lord407.zip). Respect evaluation
and registration terms. This is not LORD II or a Windows-native release.

1. Inspect and extract `lord407.zip`, then extract its inner `LORD.ZIP` into
   the installation directory. Install BNU.COM there as described above.
2. Run `LORDCFG.EXE` using the local setup configuration. On a fresh game,
   create the default LORD.DAT when asked; never reset an existing game.
   Quit/save. The current 4.07 utility's menu can differ from the old manual.
3. Install [`NODE1.DAT`](../examples/doors/lord/NODE1.DAT) with **CRLF**, not
   LF. A safe manual helper refuses to overwrite an existing file:

   ```sh
   .venv/bin/python scripts/copy_dos_config.py examples/doors/lord/NODE1.DAT /var/games/netbbs/lord/NODE1.DAT
   ```

   If NODE1.DAT already exists, back it up first. Do not overwrite a running
   game's node configuration. LORD can silently fall back to local/default
   settings when the file uses wrong line endings.
4. Verify in LORDCFG: node 1, BBS NetBBS, DOORSYS, drop path `D:\`, FOSSIL,
   COM1, locked speed 38400, no direct screen, no open/reset port commands.
   Save. Runtime mounts the drop directory as D:; D: need not exist during
   this local setup step.
5. Inside NetBBS select `dos-lord`, correct the executable/install paths,
   and test. The command is **`CALL START.BAT {node}`**, not LORD.EXE alone.
   Keep the publisher's START.BAT IGM/re-entry handling; do not add host `CD`
   paths. Its normal final return can be 255, which this template accepts.

Keep one session until you have independently certified a licensed multi-node
installation. Existing player data is persistent in the game directory.

## Global War 2.7 DOS

The tested product is **Global War** (singular), originally Joel Bergen's
game, now distributed by John Dailey Software. If you meant a different
game called Global Wars, this template does not certify that product.

**MANUAL — outside NetBBS:** download the evaluation from the
[publisher's Global War page](https://www.johndaileysoftware.com/products/bbsdoors/globalwar/).
`gwarv27.exe` is a self-extracting ARJ; inspect/extract it in a staging
directory with `unarj l gwarv27.exe` then `unarj e gwarv27.exe`. The NetBSD
extractor creates lowercase names; DOSBox resolves these case-insensitively.
Move the extracted game to its dedicated installation and read `gwar.doc`.

Install the repository's [WAR.CFG](../examples/doors/global-war/WAR.CFG) using
`scripts/copy_dos_config.py` for CRLF, backing up the publisher's file first.
After moving the old `war.cfg` aside while the game is stopped, run as the
installation owner from the NetBBS checkout:

```sh
.venv/bin/python scripts/copy_dos_config.py examples/doors/global-war/WAR.CFG /var/games/netbbs/globalwar/war.cfg
```

For a licensed installation, edit a staging copy with your existing registration
and registered BBS name on lines 1–2, and use that as the source instead.
It selects direct UART (`U`), direct local-screen writes, ANSI and
evaluation-compatible game limits. Its internal node-aware setting is `Y`;
NetBBS independently limits it to **one caller**. Keep the space-delimited
comments after values: the tested executable stalls with a bare-value-only
configuration. Bulletin entries also need their `key:description` suffix.
Preserve your own registration number if licensed; never change line ordering. For other versions use that
version's supplied configuration and documented line meanings instead. The
`dos-global-war` template generates DOOR.SYS and launches
`WAR.EXE /D D:\DOOR.SYS`. No FOSSIL is needed for this direct-UART template.
Do not enable GWTerm/RIP or graphics-only mode; the supported path is ANSI
serial output. Review game limits and licensing in WAR.CFG before publishing.
Test a new player, quit, reconnect, and confirm its persistent game state.
The normal game's default minimum is three players. Different NetBBS callers
can join and take turns on successive visits; the default **one concurrent
session** does not limit the total participants in a persistent game. Keep
sequential turns enabled until you have separately tested other modes.

## TradeWars 2002 3.09 DOS

**MANUAL — outside NetBBS:** obtain the DOS release, not the current Windows
TradeWars Game Server. The [WWIV project's setup documentation](https://docs.wwivbbs.org/en/wwiv53/chains/tradewars2002/)
links the [ClassicTW DOS 3.09 archive](https://wiki.classictw.com/filearchive/apps/2002V309.ZIP).
Read the included license and TWSYSOP.DOC. Do not bypass registration limits;
the evaluation's node limits apply independently of NetBBS.

1. Extract the outer ZIP in a dedicated installation. Run `INSTALL.BAT` in
   the local setup emulator to expand the program/support archives and
   initialize a fresh universe with BIGBANG. **This is new-game setup; do
   not run BIGBANG against an existing universe unless you intend a reset.**
   In BIGBANG's menu choose **Z — Begin Universe Creation**, Enter, then
   confirm with Y and Enter;
   merely opening BIGBANG does not initialize a playable universe. Decline
   registration if you do not own a license. Use the evaluation's limits.
2. After initialization, back up the installation's existing TWNODE.DAT.
   For **DOS 3.09 only**, install the supplied binary configuration (stored
   as reviewable hex text in the repo):

   ```sh
   .venv/bin/python scripts/copy_dos_config.py --hex examples/doors/tradewars/TWNODE.DAT.hex /var/games/netbbs/tw2002/TWNODE.DAT
   ```

   This provides local node 0 and remote node 1: default persistent data
   directory, drop directory `D:\`, WWIV/CHAIN.TXT, active node, COM1,
   hardware handshaking and FOSSIL. It contains no registration information.
   The helper refuses to overwrite; never replace another version's file or
   a configured multi-node installation with this two-record template.
   Verify in `TEDIT.EXE`: `O`, node `1`, Enter; `B` is `D:\`, `C` is
   `2` (WWIV), `E` is Yes, `F` is 1, `I` is `2` (FOSSIL). Exit with `X`,
   then quit TEDIT with `Q`. These letter actions are single keys; values
   such as the node number and I/O choice require Enter.
3. Install BNU.COM and select the `dos-tradewars-2002` NetBBS template. It
   creates CHAIN.TXT and launches `TW2002.EXE TWNODE={node}` at 38400 baud.
4. Test universe entry, quit, reconnection and daily maintenance before
   offering it to callers. Schedule the publisher's EXTERN maintenance
   **manually outside NetBBS**, according to TWSYSOP.DOC, while preventing
   conflicts with live games. NetBBS does not schedule game maintenance.

In DOS 3.09, `Q` at the universe command prompt and `Y` at **Confirmed?**
return to TradeWars' own title menu. Choose `X`, then Enter, there to return
to NetBBS. This final step is necessary; leaving the title menu open is still
a running door. The verified row above covers a single-player evaluation
smoke, not EXTERN scheduling, a licensed multi-node game or a long campaign.

## Remote services: tunnel first

A remote operator controls availability, resets, game data, privacy and terms.
NetBBS displays that service's identity in the door picker even when menu
descriptions are hidden. The operator receives the configured caller identity.
Never reuse a caller's NetBBS password as an RLogin credential.

**MANUAL — outside NetBBS:** obtain a provider account and written connection
parameters: tunnel host/port, account/key, remote RLogin destination, exact
local-user and remote-user field format, and service name. There is no
universal DoorParty/BBSLink credential convention; use the provider's current
instructions. A provider which requires a different protocol needs its own
adapter, not guessed credentials in this one.

1. Copy [`remote/ssh_config`](../examples/doors/remote/ssh_config). Replace
   placeholders, including the destination in `LocalForward`. Obtain and
   independently verify the provider's SSH host key; add it to the tunnel
   account's known_hosts. Set the private key permissions to 600. Do not
   disable host-key checking or forward the NetBBS account's agent.
2. Start the tunnel as an unprivileged account:

   ```sh
   ssh -F /absolute/path/to/ssh_config -N netbbs-door-tunnel
   ```

   Arrange supervision/restart with your host's existing service manager
   and test a reboot. OpenSSH is in NetBSD base; install the OpenSSH client
   package manually on hosts which do not supply it. NetBBS does not start
   or supervise the tunnel.
3. Alternative only for a TLS-capable provider: install `stunnel` manually
   (`sudo pkgin install stunnel` on NetBSD), copy
   [`stunnel.conf`](../examples/doors/remote/stunnel.conf), set its actual
   host/port/CA path/name, and run `stunnel /path/to/stunnel.conf`. Use your
   host's actual CA bundle; certificate verification is mandatory. Do not
   run SSH and stunnel on the same loopback port.
4. Select `remote-tunnel` in NetBBS: host `127.0.0.1`, port `1513`, allowlist
   `["127.0.0.1:1513"]`, real `service_name`. Use `local_user`/`remote_user`
   templates with `{user_id}` and/or `{handle}` exactly as the provider requires.
5. For secret provider fields, manually copy
   [`credentials.example.json`](../examples/doors/remote/credentials.example.json)
   **outside the repository**, fill in the private values, chmod 600, and
   set `options.credential_file` to its absolute path. NetBBS reads only a
   regular file of at most 4 KiB; keep it out of game mounts and backups
   exposed to callers. Do not put secrets in a shared exported profile.

RLogin is plaintext. A loopback destination is only a configuration guard;
the SysOp must actually provide the secure tunnel. Direct non-loopback access
requires `"insecure_acknowledged": true` and an exact destination allowlist.
Only use it on a consciously trusted network after reviewing credential and
caller privacy. No arbitrary-host proxy or privileged source port is offered;
traditional servers insisting on ports 512–1023 are incompatible.

RFC 1282 urgent window-size requests are answered on the RLogin socket.
Ordinary SSH port forwarding and TLS byte tunnels do not generally preserve
TCP urgent data. Use the provider-agreed fixed geometry (normally 80x25) for
those tunnels; do not assume live resize negotiation reaches the remote host.

### DoorParty provider template

The `remote-doorparty` preset uses the provider's documented RLogin identity
mapping: local-user is a stable door-only password, remote-user is
`[assigned-system-tag]handle`. This is a configuration template, **not a
live-account certification**. See the provider connector author's
[protocol and account instructions](https://github.com/echicken/dpc2#usage).

**MANUAL — outside NetBBS:** obtain provider access first, confirm the current
SSH/RLogin endpoints, and edit
[`doorparty_ssh_config`](../examples/doors/remote/doorparty_ssh_config).
Start `ssh -F /path/to/doorparty_ssh_config -N netbbs-doorparty` from a private
operator terminal and enter the provider SSH password if requested. Leave
that tunnel running; a password-prompt tunnel does not automatically survive
logout/reboot. For unattended operation, arrange provider-approved key
authentication and host service supervision. Do not use `sshpass` or store
the provider's SSH password in NetBBS.

Copy [`doorparty.credentials.example.json`](../examples/doors/remote/doorparty.credentials.example.json)
to the preset's private credential path, replace the tag (do not double its
brackets), and generate a long random door-only secret prefix. Keep that
secret stable and backed up; changing it can break existing provider accounts.
It is **not** the provider SSH password or a caller's NetBBS password.
Use chmod 600. Do not grant guest accounts access; initially restrict the
door to SysOp, then regular approved callers after a successful test.

## Verification and troubleshooting

**MANUAL — outside NetBBS:** test on a disposable game copy; smoke tests may
create players, advance turns or run maintenance. The repository's opt-in
DOS fixture is our own serial/FOSSIL test program, not a third-party game.
Install `nasm` manually for it (`sudo pkgin install nasm` on NetBSD), then:

```sh
PATH=/absolute/directory/containing/patched/dosbox-x:$PATH \
PYTHONPATH=src NETBBS_TEST_FOSSIL=/absolute/path/to/BNU.COM \
  .venv/bin/python -m pytest tests/test_doors_runtime.py \
  tests/test_doors_compatibility.py tests/test_door_transports.py \
  tests/test_door_web_protocol.py tests/test_door_profile_flow.py tests/test_web.py -q
```

Set that directory to the build tree's `src` before installation, or the
verified private prefix's `bin` afterwards. The tests discover `dosbox-x`
through `PATH`; changing a saved NetBBS profile does not select their emulator.
Unset `NETBBS_TEST_FOSSIL` to skip the optional licensed driver check.
Without dosbox-x/nasm the DOS tests skip; a skipped test is not certification.
The test environment also needs NetBBS's `dev` extra (including the SSH/web
dependencies): install it manually with `.venv/bin/python -m pip install -e '.[dev]'`
in a development checkout. Follow the operator guide's NetBSD SSH build
prerequisites first. The browser JavaScript fixture needs an operator-installed
Node.js (`sudo pkgin install nodejs` on NetBSD, `sudo apt-get install nodejs`
on Debian); without it that fixture skips. Node.js and NASM are test tools,
not NetBBS door-runtime requirements.
The game smoke harness installs nothing and uses a temporary NetBBS database:

```sh
PYTHONPATH=src .venv/bin/python scripts/door_compat_smoke.py \
  src/netbbs/doors/presets/dos-lord.json /path/to/disposable/lord \
  --emulator /absolute/path/to/patched/dosbox-x \
  --seconds 65 --auto-page --input-file examples/doors/lord-smoke.json
```

Inputs are `[delay_seconds, text]` pairs, or `["expected output", text]` to wait
for a prompt before typing (ANSI styling is ignored). The LORD example is
for an existing `DoorTester` character; use `lord-new-player-smoke.json`
on a fresh disposable game first. TradeWars equivalents are
[`tradewars-new-player-smoke.json`](../examples/doors/tradewars-new-player-smoke.json)
and [`tradewars-smoke.json`](../examples/doors/tradewars-smoke.json); substitute
the TradeWars preset and installation path in the command above. These assume
the documented default new universe, including a starting planet. A partially
created character or different universe settings need adjusted inputs.
Global War's [`global-war-new-game-smoke.json`](../examples/doors/global-war-new-game-smoke.json)
creates one waiting game on a fresh disposable installation; use
[`global-war-smoke.json`](../examples/doors/global-war-smoke.json) to reconnect
and quit. Do not repeatedly run the new-game recipe against an account already
at the evaluation's one-game limit.
`--auto-page` dismisses known art pauses,
including LORD's unlabelled initial title pause; it does not answer gameplay
questions. Adjust scripts for your game version. A clean exit with uncompleted
scripted steps is a smoke-test failure, not certification.
A timeout can mean incorrect inputs, not an emulator failure. Record game,
emulator and host versions; test normal quit, crash, timeout, caller disconnect,
node shutdown, CP437 ANSI and 80x25 over Telnet, SSH and web, and reconnect
to verify scores. Multi-node certification additionally needs two live
callers and a game demonstrably safe for shared-file access.

| Symptom | Check/action |
| --- | --- |
| Missing executable/driver/directory | Install the named dependency manually; correct the absolute path and service-account permissions |
| Loader says a library is missing although installed | Check the emulator address-space ceiling; the tested NetBSD DOSBox needs 1 GiB rather than native 256 MiB |
| LORD shows nothing, local node/defaults in LORDCFG | Install NODE1.DAT with CRLF, create LORD.DAT, confirm DOORSYS/FOSSIL/COM1 and D:\ |
| ANSI art but no response | Match UART vs FOSSIL, baud, drop-file path and game input conventions; some prompts require Enter |
| DOS exits but NetBBS reports crash | Read last diagnostic; game exit status and emulator status are separate; check command spelling and START.BAT |
| Door is busy | Default one-session policy; wait rather than deleting lease files |
| Web rejects raw mode / screen too small | Use utf-8 or cp437 and enlarge the terminal to the configured dimensions |
| Remote connection refused/handshake rejected | Check tunnel, exact provider field convention, credentials permissions and allowlist; never disable verification to hide a TLS/SSH error |
| Native wrapper leaves children behind | Keep descendants in the owned group; daemonization, setsid, detached containers and untrusted code are outside the supervision guarantee |

No automatic host repairs are performed. A failing/unverified setup should
remain SysOp-only until its complete caller experience has been demonstrated.
