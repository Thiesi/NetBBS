"""
SQLite storage layer for NetBBS nodes.

Design doc §3: SQLite (WAL mode), per node — no separate DB server process.
"""

from netbbs.storage.database import Database

__all__ = ["Database"]
