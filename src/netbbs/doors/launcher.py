"""Private exec helper: resource/TTY setup after exec, never Python preexec_fn.

Invoked by absolute script path with an isolated interpreter. No NetBBS imports.
The parent starts a new session and passes only explicitly owned descriptors.
"""
import fcntl
import json
import os
import resource
import sys
import termios


def main():
    setup = json.loads(sys.argv[1])
    if setup.get("pty"):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    for name, value in setup["limits"].items():
        kind = getattr(resource, name)
        _, hard = resource.getrlimit(kind)
        value = min(value, hard) if hard != resource.RLIM_INFINITY else value
        resource.setrlimit(kind, (value, value))
    argv = sys.argv[2:]
    os.execve(argv[0], argv, os.environ)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Door launcher: {exc}", file=sys.stderr, flush=True)
        sys.exit(126)
