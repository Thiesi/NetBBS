"""Install a newer NetBBS release from inside the running node (issue #731).

`netbbs.selfupdate` finds out that a newer release exists. This module
installs it: fetch the release's wheel from GitHub, check it against the
SHA-256 digest the release publishes, `pip install` it into the interpreter
the node is running on, and -- where a service manager restarts NetBBS when it
exits -- ask the node to shut down gracefully with `RESTART_EXIT_CODE`, so the
new build is what comes back up. Where nothing restarts the node, it stops
after the install and the SysOp restarts the service.

Trust boundary (design doc §6.7): HTTPS and GitHub. The digest comes from the
same release API over the same TLS, so it proves the downloaded bytes are the
bytes GitHub holds for that asset -- a truncated, corrupted or swapped download
fails -- not that a maintainer signed them. An asset without a published digest
is refused rather than installed on TLS alone.

Pure functions and injectable I/O here; the SysOp screen is
`netbbs.net.admin_flow._install_release_screen`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from netbbs.config import get_config, set_config
from netbbs.storage.database import Database

#: Exit status of a node that shut down to run a newly installed release.
#: Non-zero, so the shipped systemd unit's `Restart=on-failure` restarts it;
#: `examples/netbbs.service` also lists it under `RestartForceExitStatus=` and
#: `SuccessExitStatus=` so it is neither refused nor logged as a failure.
#: 75 is sysexits' EX_TEMPFAIL.
RESTART_EXIT_CODE = 75

MAX_WHEEL_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 300.0
PIP_TIMEOUT_SECONDS = 900.0
PIP_LOG_MAX_CHARS = 16 * 1024
_CHUNK_BYTES = 64 * 1024

_RELEASE_BY_TAG_URL = "https://api.github.com/repos/Thiesi/NetBBS/releases/tags/{tag}"
_TAG_RE = re.compile(r"^v?\d+(\.\d+){1,3}$")
_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
# Where release assets are served from. `browser_download_url` points at
# github.com and redirects to GitHub's object storage.
_ASSET_URL_PREFIX = "https://github.com/Thiesi/NetBBS/releases/download/"

RESTART_MODE_CONFIG_KEY = "update_restart_mode"
RESTART_MODES = ("auto", "yes", "no")
_ATTEMPT_CONFIG_KEY = "update_install_attempt"


class ApplyError(Exception):
    """An install step refused or failed; the message is for the SysOp."""


# -- restart after install ----------------------------------------------------


def detect_supervisor(environ: dict[str, str] | None = None) -> str | None:
    """Name the service manager that restarts this process when it exits, if
    one can be recognised. systemd sets `INVOCATION_ID` for every service it
    starts; NetBSD's rc.d sets nothing and does not restart a stopped node."""
    environ = os.environ if environ is None else environ
    return "systemd" if environ.get("INVOCATION_ID") else None


def get_restart_mode(db: Database) -> str:
    value = get_config(db, RESTART_MODE_CONFIG_KEY)
    return value if value in RESTART_MODES else "auto"


def set_restart_mode(db: Database, mode: str) -> None:
    if mode not in RESTART_MODES:
        raise ValueError(f"unknown restart mode {mode!r}")
    set_config(db, RESTART_MODE_CONFIG_KEY, mode)


def restarts_after_install(db: Database, environ: dict[str, str] | None = None) -> bool:
    """Whether an install should end in a graceful shutdown for the service
    manager to restart. The SysOp's declaration wins; `auto` trusts detection."""
    mode = get_restart_mode(db)
    if mode == "yes":
        return True
    if mode == "no":
        return False
    return detect_supervisor(environ) is not None


_restart_exit_requested = False


def request_restart_exit() -> None:
    global _restart_exit_requested
    _restart_exit_requested = True


def cancel_restart_exit() -> None:
    global _restart_exit_requested
    _restart_exit_requested = False


def restart_exit_requested() -> bool:
    """Read by `netbbs.__main__.main` once the node has stopped: exit with
    `RESTART_EXIT_CODE` instead of 0."""
    return _restart_exit_requested


async def run_restart_shutdown(shutdown: Callable[[], Any]) -> None:
    """Run the node's graceful shutdown for a restart into a new release.

    The exit status is only requested while this sequence is the one in
    charge: a SysOp who cancels it, or a SIGTERM that replaces it (the
    service manager stopping the node, which must stay stopped), withdraws
    the request."""
    request_restart_exit()
    try:
        await shutdown()
    except asyncio.CancelledError:
        cancel_restart_exit()
        raise


# -- the installation this process runs from ---------------------------------


@dataclass(frozen=True)
class InstallEnvironment:
    python: str
    site_packages: Path
    version: str
    extras: tuple[str, ...]


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return match.group(1) if match else ""


def installed_extras(dist: Any) -> tuple[str, ...]:
    """The optional extras this installation has: those whose every
    requirement is installed. `dev` is never carried into a node's install."""
    by_extra: dict[str, list[str]] = {}
    for requirement in dist.requires or []:
        match = re.search(r"""extra\s*==\s*["']([^"']+)["']""", requirement)
        if match:
            by_extra.setdefault(match.group(1), []).append(_requirement_name(requirement))
    present = []
    for extra, names in sorted(by_extra.items()):
        if extra == "dev" or not names:
            continue
        try:
            for name in names:
                metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        present.append(extra)
    return tuple(present)


def _is_editable(dist: Any) -> bool:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return bool((data.get("dir_info") or {}).get("editable")) or "vcs_info" in data


def inspect_install_environment(
    *,
    dist_name: str = "netbbs",
    prefix: str | None = None,
    base_prefix: str | None = None,
    executable: str | None = None,
    distribution: Callable[[str], Any] = metadata.distribution,
    writable: Callable[[Path], bool] = lambda path: os.access(path, os.W_OK),
) -> InstallEnvironment:
    """Where an install would go, or why one must not be attempted."""
    prefix = sys.prefix if prefix is None else prefix
    base_prefix = sys.base_prefix if base_prefix is None else base_prefix
    executable = sys.executable if executable is None else executable
    if prefix == base_prefix:
        raise ApplyError(
            "NetBBS is not running from a virtual environment, so installing here would change "
            "the system's own Python. Upgrade by hand as the handbook describes."
        )
    try:
        dist = distribution(dist_name)
    except metadata.PackageNotFoundError as exc:
        raise ApplyError("This NetBBS was not installed as a package; upgrade it by hand.") from exc
    if _is_editable(dist):
        raise ApplyError(
            "This NetBBS runs from a development checkout (an editable install); update the checkout instead."
        )
    site_packages = Path(dist.locate_file(""))
    for path in (site_packages, Path(dist.locate_file(dist_name))):
        if not writable(path):
            raise ApplyError(
                f"The service account cannot write {path}, so pip could not replace NetBBS there. "
                "Upgrade by hand as the account that owns the environment."
            )
    return InstallEnvironment(
        python=executable, site_packages=site_packages, version=dist.version, extras=installed_extras(dist)
    )


# -- the release wheel ---------------------------------------------------------


@dataclass(frozen=True)
class ReleaseWheel:
    tag: str
    version: str
    name: str
    url: str
    size: int
    sha256: str


def _default_fetch_json(url: str, token: str | None) -> bytes:
    headers = {"User-Agent": "netbbs-selfupdate", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(1024 * 1024)


def release_wheel_from_json(tag: str, raw: bytes) -> ReleaseWheel:
    """Pick the release's own wheel out of a release API response and check
    that everything needed to verify it is there."""
    try:
        release = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApplyError(f"the release API returned unreadable JSON: {exc}") from exc
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        raise ApplyError(f"the release API did not return release {tag}")
    if release.get("draft") or release.get("prerelease"):
        raise ApplyError(f"{tag} is a draft or pre-release; only published releases are installed")
    version = tag.lstrip("vV")
    wanted = f"netbbs-{version}-py3-none-any.whl"
    assets = [a for a in release.get("assets") or [] if isinstance(a, dict) and a.get("name") == wanted]
    if len(assets) != 1:
        raise ApplyError(f"release {tag} has no {wanted} to install")
    asset = assets[0]
    digest = _DIGEST_RE.match(str(asset.get("digest") or ""))
    if digest is None:
        raise ApplyError(
            f"release {tag} publishes no SHA-256 digest for {wanted}, so its download cannot be checked. "
            "Install it by hand."
        )
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith(_ASSET_URL_PREFIX + tag + "/"):
        raise ApplyError(f"release {tag} points its wheel at an unexpected address: {url[:200]}")
    size = asset.get("size")
    if not isinstance(size, int) or size <= 0 or size > MAX_WHEEL_BYTES:
        raise ApplyError(f"release {tag}'s wheel has an implausible size: {size!r}")
    return ReleaseWheel(tag=tag, version=version, name=wanted, url=url, size=size, sha256=digest.group(1))


def fetch_release_wheel(
    tag: str, *, token: str | None = None, fetch: Callable[[str, str | None], bytes] = _default_fetch_json
) -> ReleaseWheel:
    if not _TAG_RE.match(tag):
        raise ApplyError(f"not a release tag this node installs: {tag!r}")
    try:
        raw = fetch(_RELEASE_BY_TAG_URL.format(tag=tag), token)
    except OSError as exc:
        raise ApplyError(f"could not reach the release API: {exc}") from exc
    return release_wheel_from_json(tag, raw)


def download_wheel(
    wheel: ReleaseWheel,
    destination_dir: Path,
    *,
    open_url: Callable[..., Any] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> Path:
    """Download `wheel` into `destination_dir` and verify it; returns its path.

    Bounded by size and wall time, HTTPS end to end (redirects included), and
    written under a temporary name that becomes the real one only once its
    SHA-256 matches the release's digest."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    final = destination_dir / wheel.name
    partial = destination_dir / (wheel.name + ".part")
    deadline = clock() + timeout_seconds
    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(wheel.url, headers={"User-Agent": "netbbs-selfupdate"})
    try:
        with open_url(request, timeout=30) as response:
            landed = response.geturl()
            if not str(landed).startswith("https://"):
                raise ApplyError(f"the download was redirected off HTTPS: {str(landed)[:200]}")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "wb") as out:
                while True:
                    if clock() > deadline:
                        raise ApplyError(f"the download took longer than {int(timeout_seconds)} seconds")
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > wheel.size or received > MAX_WHEEL_BYTES:
                        raise ApplyError("the download is larger than the release says the wheel is")
                    digest.update(chunk)
                    out.write(chunk)
    except ApplyError:
        partial.unlink(missing_ok=True)
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ApplyError(f"the download failed: {exc}") from exc
    if received != wheel.size:
        partial.unlink(missing_ok=True)
        raise ApplyError(f"the download ended after {received} of {wheel.size} bytes")
    if digest.hexdigest() != wheel.sha256:
        partial.unlink(missing_ok=True)
        raise ApplyError("the downloaded wheel does not match the release's SHA-256 digest; nothing was installed")
    partial.replace(final)
    return final


def updates_directory(db_path: Path) -> Path:
    """Where downloaded wheels are kept, beside the database."""
    return Path(db_path).parent / f"{Path(db_path).stem}_updates"


# -- pip -------------------------------------------------------------------------


def pip_command(environment: InstallEnvironment, wheel_path: Path) -> list[str]:
    target = str(wheel_path)
    if environment.extras:
        target += "[" + ",".join(environment.extras) + "]"
    return [
        environment.python, "-m", "pip", "install",
        "--no-input", "--disable-pip-version-check", "--no-cache-dir",
        target,
    ]


def version_query_command(python: str) -> list[str]:
    return [python, "-c", "import importlib.metadata as m; print(m.version('netbbs'))"]


async def run_bounded(
    command: list[str],
    *,
    timeout_seconds: float,
    max_chars: int = PIP_LOG_MAX_CHARS,
    spawn: Callable[..., Any] = asyncio.create_subprocess_exec,
) -> tuple[int, str]:
    """Run `command`, returning its exit status and the tail of its combined
    output. The process is owned: a timeout or a cancelled caller kills it and
    waits for it before this returns or re-raises."""
    process = await spawn(
        *command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    tail: deque[str] = deque()
    kept = 0

    async def _drain() -> int:
        nonlocal kept
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            tail.append(text)
            kept += len(text) + 1
            while kept > max_chars and tail:
                kept -= len(tail.popleft()) + 1
        return await process.wait()

    try:
        status = await asyncio.wait_for(_drain(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _kill(process)
        raise ApplyError(
            f"{Path(command[0]).name} {' '.join(command[1:3])} did not finish within {int(timeout_seconds)} seconds"
        ) from None
    except BaseException:
        await _kill(process)
        raise
    return status, "\n".join(tail)


async def _kill(process: Any) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


# -- what happened across the restart -----------------------------------------


def record_install(db: Database, *, from_version: str, to_version: str, restarting: bool) -> None:
    """Remember an install before the node goes down, for the next start to
    report against the version it actually runs."""
    set_config(db, _ATTEMPT_CONFIG_KEY, json.dumps(
        {"from": from_version, "to": to_version, "restarting": restarting}
    ))


def get_recorded_install(db: Database) -> dict | None:
    raw = get_config(db, _ATTEMPT_CONFIG_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("to"), str):
        return None
    return data


def reconcile_install_at_startup(db: Database, current_version: str) -> str | None:
    """Called once when a node starts: turn a recorded install into an
    outcome the Update screen shows, and forget it. `None` when there was
    none."""
    from netbbs.selfupdate import _normalize_version, record_check_outcome

    data = get_recorded_install(db)
    db.connection.execute("DELETE FROM node_config WHERE key = ?", (_ATTEMPT_CONFIG_KEY,))
    db.connection.commit()
    if data is None:
        return None
    target = data["to"]
    if _normalize_version(target) == _normalize_version(current_version):
        outcome = f"installed {target} and restarted into it"
    else:
        outcome = (
            f"installed {target}, but this node started as {current_version} -- "
            "check the service's interpreter and the install"
        )
    record_check_outcome(db, outcome)
    return outcome
