#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.txt for GitHub Release notes.

Used by .github/workflows/release.yml when a v* tag is pushed:

    python scripts/release_notes.py 0.7.0 > notes.md

Prints the block between "Version 0.7.0 - ..." and the next "Version " header
(without the header line itself). Exits non-zero if the version is missing so
a tag without a changelog entry fails the release loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.txt"


def extract_section(text: str, version: str) -> str | None:
    lines = text.splitlines()
    header = f"Version {version} - "
    start = None
    for index, line in enumerate(lines):
        if line.startswith(header):
            start = index + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("Version "):
            break
        body.append(line)
    return "\n".join(body).strip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_notes.py <version, e.g. 0.7.0>", file=sys.stderr)
        return 2
    version = sys.argv[1].lstrip("v")
    section = extract_section(CHANGELOG.read_text(encoding="utf-8"), version)
    if section is None:
        print(f"ERROR: no 'Version {version}' section in {CHANGELOG.name}", file=sys.stderr)
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
