"""
Connectivity layer: transport implementations (Telnet now; SSH and a
web-based terminal emulator later) landing on the shared `Session`
abstraction.
"""

from netbbs.net.session import Session, SessionClosedError
from netbbs.net.telnet import TelnetServer, TelnetSession

__all__ = ["Session", "SessionClosedError", "TelnetServer", "TelnetSession"]
