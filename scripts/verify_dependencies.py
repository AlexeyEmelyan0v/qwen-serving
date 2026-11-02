"""Verify a complete requirements lock against an immutable base image."""

from importlib import metadata
from pathlib import Path
import re
import sys


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(path):
    locked = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)", line)
        if not match:
            raise ValueError("Every requirement must have an exact version")
        name, version = match.groups()
        name = normalize(name)
        if name in locked:
            raise ValueError("Duplicate package in dependency lock")
        locked[name] = version
    if not locked:
        raise ValueError("Dependency lock is empty")
    return locked


def main():
    expected = locked_versions(sys.argv[1])
    installed = {
        normalize(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions()
    }
    differences = sorted(
        name for name in expected.keys() | installed.keys()
        if expected.get(name) != installed.get(name)
    )
    if differences:
        raise SystemExit("Dependency lock mismatch: " + ", ".join(differences))
    print(f"Verified all {len(expected)} Python package versions.")


if __name__ == "__main__":
    main()
