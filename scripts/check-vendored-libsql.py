#!/usr/bin/env python3
"""Verify the reviewed libSQL source and reject silent vendor drift."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def verify(root: Path) -> None:
    """Raise ValueError if the vendored tree differs from its reviewed manifest."""
    manifest = root / "TEMPER-SHA256SUMS"
    expected: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        digest, name = line.split("  ", 1)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid vendor checksum")
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or name in expected:
            raise ValueError("invalid vendor manifest path")
        expected[name] = digest
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path != manifest
    }
    if actual.keys() != expected.keys():
        raise ValueError("vendored libSQL file set differs from its reviewed manifest")
    for name, path in actual.items():
        if (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]
        ):
            raise ValueError(f"vendored libSQL content differs: {name}")


if __name__ == "__main__":
    vendor = (
        Path(sys.argv[1])
        if len(sys.argv) == 2
        else Path(__file__).resolve().parents[1] / "vendor/libsql"
    )
    try:
        verify(vendor)
    except (OSError, ValueError) as error:
        sys.exit(str(error))
