"""Manually copy a repository text configuration with DOS CRLF line endings.

Refuses to overwrite existing files: back up or choose a new destination first.
Does not download, execute a game, or change any NetBBS configuration.
"""
import argparse
from pathlib import Path


def copy_config(source: Path, destination: Path, *, hexadecimal=False):
    text = source.read_text(encoding="utf-8")
    data = bytes.fromhex(text) if hexadecimal else text.replace("\r\n", "\n").replace("\n", "\r\n").encode("cp437")
    with destination.open("xb") as target:
        target.write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--hex", action="store_true", help="Decode a repository .hex binary configuration instead of converting text to CRLF")
    args = parser.parse_args()
    copy_config(args.source, args.destination, hexadecimal=args.hex)
