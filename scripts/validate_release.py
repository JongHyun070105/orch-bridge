#!/usr/bin/env python3
"""Validate that the checked-out source version matches a release tag."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def validate(tag: str, root: Path = ROOT) -> str:
    match = VERSION_RE.fullmatch(tag)
    if not match:
        raise ValueError(f"release tag must use vMAJOR.MINOR.PATCH: {tag}")
    version = ".".join(match.groups())
    data = json.loads((root / "VERSION.json").read_text(encoding="utf-8"))
    if str(data.get("release") or "") != version:
        raise ValueError(f"tag {tag} does not match VERSION.json release {data.get('release')!r}")
    manifest = json.loads((root / ".release-please-manifest.json").read_text(encoding="utf-8"))
    if str(manifest.get(".") or "") != version:
        raise ValueError(
            f"tag {tag} does not match .release-please-manifest.json version {manifest.get('.')!r}"
        )
    return version


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: validate_release.py vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    try:
        version = validate(args[0])
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    print(f"release tag validated: v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
