#!/usr/bin/env python3
"""Check that rustwright-core and rustwright can publish to crates.io.

The test workflow runs this on every pull request, and the release-crates
workflow runs it before it packages the crates. A manifest change that blocks
the crates.io release therefore fails before a release tag exists. Set
RELEASE_TAG (for example v0.4.0) to also compare the crate version with a tag.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_MANIFEST = "Cargo.toml"
FACADE_MANIFEST = "rust-native/Cargo.toml"


def main() -> int:
    manifests = {
        path: tomllib.loads((ROOT / path).read_text(encoding="utf-8"))
        for path in (CORE_MANIFEST, FACADE_MANIFEST)
    }
    errors = []
    for path, manifest in manifests.items():
        package = manifest["package"]
        if package.get("publish", True) is not True:
            errors.append(f"{path} must not restrict [package].publish")
        # crates.io rejects an upload without these fields.
        for field in ("description", "license"):
            if not package.get(field):
                errors.append(f"{path} is missing [package].{field}")

    version = manifests[CORE_MANIFEST]["package"]["version"]
    facade = manifests[FACADE_MANIFEST]
    facade_versions = {
        f"{FACADE_MANIFEST} [package].version": facade["package"]["version"],
        f"{FACADE_MANIFEST} rustwright_core requirement": (
            facade.get("dependencies", {}).get("rustwright_core", {}).get("version")
        ),
    }
    for label, found in facade_versions.items():
        if found != version:
            errors.append(f"{label} is {found!r}, but {CORE_MANIFEST} is {version!r}")

    tag = os.environ.get("RELEASE_TAG", "")
    if tag and tag.removeprefix("v") != version:
        errors.append(f"tag {tag!r} does not match crate version {version!r}")

    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"crates.io release metadata is valid for version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
