#!/usr/bin/env python3
"""Update and verify Rustwright's shared package version."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple


ROOT = Path(__file__).resolve().parents[4]
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:alpha|beta|rc)\.(?:0|[1-9]\d*)))?$"
)
SOURCE_FILES = {
    "pyproject.toml": (ROOT / "pyproject.toml", "project"),
    "Cargo.toml": (ROOT / "Cargo.toml", "package"),
    "node/Cargo.toml": (ROOT / "node/Cargo.toml", "package"),
}
RUNTIME_VERSION_FIELDS = (
    (
        "python/rustwright/sync_api.py HAR creator",
        ROOT / "python/rustwright/sync_api.py",
        re.compile(
            r'("creator"\s*:\s*\{\s*"name"\s*:\s*"Rustwright"\s*,\s*'
            r'"version"\s*:\s*")([^"]+)(")'
        ),
    ),
    (
        "python/rustwright/sync_api.py trace metadata",
        ROOT / "python/rustwright/sync_api.py",
        re.compile(r'("playwrightVersion"\s*:\s*"rustwright-)([^"]+)(")'),
    ),
    (
        "python/rustwright/cli.py source fallback",
        ROOT / "python/rustwright/cli.py",
        re.compile(
            r'(except metadata\.PackageNotFoundError:\s*\n\s*return ")([^"]+)(")'
        ),
    ),
    (
        "python/rustwright/_backend.py source fallback",
        ROOT / "python/rustwright/_backend.py",
        re.compile(
            r'(except metadata\.PackageNotFoundError:\s*\n\s*return ")([^"]+)(\+local")'
        ),
    ),
)


def parse_version(value: str) -> Tuple[int, int, int, Tuple[str, ...]]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError(
            f"{value!r} is not a supported release version; expected "
            "MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-(alpha|beta|rc).N"
        )
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    for identifier in prerelease:
        if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
            raise ValueError(f"numeric prerelease identifier has a leading zero: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease


def compare_versions(left: str, right: str) -> int:
    left_version = parse_version(left)
    right_version = parse_version(right)
    left_core, right_core = left_version[:3], right_version[:3]
    if left_core != right_core:
        return (left_core > right_core) - (left_core < right_core)

    left_pre, right_pre = left_version[3], right_version[3]
    if not left_pre or not right_pre:
        return (not left_pre) - (not right_pre)
    for left_id, right_id in zip(left_pre, right_pre):
        if left_id == right_id:
            continue
        left_numeric, right_numeric = left_id.isdigit(), right_id.isdigit()
        if left_numeric and right_numeric:
            return (int(left_id) > int(right_id)) - (int(left_id) < int(right_id))
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_id > right_id) - (left_id < right_id)
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def next_version(current: str) -> str:
    major, minor, patch, prerelease = parse_version(current)
    if not prerelease:
        return f"{major}.{minor}.{patch + 1}"
    if prerelease[-1].isdigit():
        bumped = (*prerelease[:-1], str(int(prerelease[-1]) + 1))
    else:
        bumped = (*prerelease, "1")
    return f"{major}.{minor}.{patch}-{'.'.join(bumped)}"


def toml_version(path: Path, section: str) -> str:
    current_section = None  # type: Optional[str]
    versions = []
    for line in path.read_text().splitlines():
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            current_section = section_match.group(1)
            continue
        if current_section != section:
            continue
        version_match = re.match(
            r'^\s*version\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?$', line
        )
        if version_match:
            versions.append(version_match.group(1))
    if len(versions) != 1:
        raise RuntimeError(
            f"expected one version in [{section}] of {path.relative_to(ROOT)}, "
            f"found {len(versions)}"
        )
    return versions[0]


def source_versions() -> Dict[str, str]:
    versions = {
        label: toml_version(path, section)
        for label, (path, section) in SOURCE_FILES.items()
    }
    versions["node/package.json"] = json.loads(
        (ROOT / "node/package.json").read_text()
    )["version"]
    for label, path, pattern in RUNTIME_VERSION_FIELDS:
        runtime_versions = pattern.findall(path.read_text())
        if len(runtime_versions) != 1:
            raise RuntimeError(
                f"expected one {label}, found {len(runtime_versions)}"
            )
        versions[label] = runtime_versions[0][1]
    return versions


def replace_toml_version(path: Path, section: str, version: str) -> None:
    lines = path.read_text().splitlines(keepends=True)
    current_section = None  # type: Optional[str]
    replacements = 0
    for index, line in enumerate(lines):
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            current_section = section_match.group(1)
            continue
        if current_section != section:
            continue
        version_match = re.match(
            r'^(\s*version\s*=\s*)["\'][^"\']+["\'](\s*(?:#.*)?(?:\n)?)$', line
        )
        if version_match:
            lines[index] = f'{version_match.group(1)}"{version}"{version_match.group(2)}'
            replacements += 1
    if replacements != 1:
        raise RuntimeError(
            f"expected one version in [{section}] of {path.relative_to(ROOT)}, "
            f"found {replacements}"
        )
    path.write_text("".join(lines))


def replace_json_version(path: Path, version: str) -> None:
    data = json.loads(path.read_text())
    data["version"] = version
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def replace_runtime_version(version: str) -> None:
    updated_files = {}  # type: Dict[Path, str]
    for label, path, pattern in RUNTIME_VERSION_FIELDS:
        text = updated_files.get(path, path.read_text())
        updated, replacements = pattern.subn(
            lambda match: f"{match.group(1)}{version}{match.group(3)}", text
        )
        if replacements != 1:
            raise RuntimeError(f"expected one {label}, found {replacements}")
        updated_files[path] = updated
    for path, text in updated_files.items():
        path.write_text(text)


def lock_versions() -> Dict[str, str]:
    selected = {}  # type: Dict[str, str]
    package = {}  # type: Dict[str, str]
    for line in (ROOT / "Cargo.lock").read_text().splitlines():
        if line.strip() == "[[package]]":
            if package.get("name") in {"rustwright-core", "rustwright-node"}:
                selected[package["name"]] = package.get("version", "")
            package = {}
            continue
        field_match = re.match(r'^(name|version) = "([^"]+)"$', line)
        if field_match:
            package[field_match.group(1)] = field_match.group(2)
    if package.get("name") in {"rustwright-core", "rustwright-node"}:
        selected[package["name"]] = package.get("version", "")
    if set(selected) != {"rustwright-core", "rustwright-node"}:
        raise RuntimeError(f"missing Rustwright packages in Cargo.lock: {selected}")

    node_lock = json.loads((ROOT / "node/package-lock.json").read_text())
    return {
        "Cargo.lock rustwright-core": selected["rustwright-core"],
        "Cargo.lock rustwright-node": selected["rustwright-node"],
        "node/package-lock.json": node_lock["version"],
        'node/package-lock.json packages[""]': node_lock["packages"][""]["version"],
    }


def require_one_version(
    versions: Dict[str, str], expected: Optional[str] = None
) -> str:
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise RuntimeError(f"package versions do not match: {details}")
    actual = next(iter(unique))
    parse_version(actual)
    if expected is not None and actual != expected:
        raise RuntimeError(f"expected version {expected}, found {actual}")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="target SemVer; defaults to next version")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify manifests and lockfiles without changing files",
    )
    args = parser.parse_args()

    try:
        current = require_one_version(source_versions())
        if args.check:
            target = args.version or current
            parse_version(target)
            require_one_version({**source_versions(), **lock_versions()}, target)
            print(f"Validated Rustwright version {target} in all manifests and lockfiles")
            return 0

        target = args.version or next_version(current)
        parse_version(target)
        if compare_versions(target, current) <= 0:
            raise ValueError(f"target version {target} must be newer than {current}")

        for path, section in SOURCE_FILES.values():
            replace_toml_version(path, section, target)
        replace_json_version(ROOT / "node/package.json", target)
        replace_runtime_version(target)
        require_one_version(source_versions(), target)
        print(f"Updated Rustwright source manifests from {current} to {target}")
        print("Regenerate Cargo.lock and node/package-lock.json before --check")
        return 0
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
