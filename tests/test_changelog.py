from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
REQUIRED_VERSIONS = (
    "0.1.1",
    "0.1.0",
    "0.1.0-alpha.4",
    "0.1.0-alpha.3",
)
PRERELEASE_IDENTIFIER = r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
VERSION_RE = re.compile(
    rf"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({PRERELEASE_IDENTIFIER}(?:\.{PRERELEASE_IDENTIFIER})*))?"
    rf"(?:\+({BUILD_IDENTIFIER}(?:\.{BUILD_IDENTIFIER})*))?"
)
SECTION_RE = re.compile(
    r"^## \[([^\]]+)\](?: - ([^\r\n]+))?\s*$",
    re.MULTILINE,
)
CATEGORY_RE = re.compile(
    r"^### (?:Added|Changed|Fixed|Deprecated|Removed|Security)\s*$",
    re.MULTILINE,
)
NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
NEXT_H3_RE = re.compile(r"^### ", re.MULTILINE)
BULLET_RE = re.compile(r"^- .+", re.MULTILINE)


def _version_key(
    version: str,
) -> tuple[int, int, int, tuple[int, tuple[tuple[int, int | str], ...]]]:
    match = VERSION_RE.fullmatch(version)
    assert match is not None, f"unsupported changelog version: {version}"
    major, minor, patch, prerelease, _build = match.groups()
    prerelease_key = (
        (1, ())
        if prerelease is None
        else (
            0,
            tuple(
                (0, int(identifier)) if identifier.isdigit() else (1, identifier)
                for identifier in prerelease.split(".")
            ),
        )
    )
    return int(major), int(minor), int(patch), prerelease_key


def _version_sections(text: str) -> list[re.Match[str]]:
    sections = [
        section
        for section in SECTION_RE.finditer(text)
        if section.group(1) != "Unreleased"
    ]
    unsupported = [
        section.group(1)
        for section in sections
        if VERSION_RE.fullmatch(section.group(1)) is None
    ]
    assert not unsupported, (
        "unsupported changelog version heading: " + ", ".join(unsupported)
    )
    return sections


def _assert_version_sections_strictly_descending(text: str) -> None:
    versions = [section.group(1) for section in _version_sections(text)]
    duplicates = sorted({version for version in versions if versions.count(version) > 1})
    assert not duplicates, "version sections must be unique: " + ", ".join(duplicates)

    for newer, older in zip(versions, versions[1:]):
        assert _version_key(newer) > _version_key(older), (
            "version sections must appear newest first: "
            f"{newer} must be newer than {older}"
        )


def _has_category_with_bullet(section_body: str) -> bool:
    for category in CATEGORY_RE.finditer(section_body):
        next_category = NEXT_H3_RE.search(section_body, category.end())
        category_end = next_category.start() if next_category else len(section_body)
        if BULLET_RE.search(section_body, category.end(), category_end):
            return True
    return False


def test_changelog_has_required_structure() -> None:
    assert CHANGELOG.is_file(), "CHANGELOG.md must exist at the repository root"
    text = CHANGELOG.read_text(encoding="utf-8")

    headings = re.findall(r"^#{1,6} .+$", text, re.MULTILINE)
    assert headings, "CHANGELOG.md must contain Markdown headings"
    assert headings[0] == "# Changelog", "the first heading must be '# Changelog'"

    sections = list(SECTION_RE.finditer(text))
    unreleased = [section for section in sections if section.group(1) == "Unreleased"]
    assert unreleased, "CHANGELOG.md must contain an exact '## [Unreleased]' section"
    assert unreleased[0].group(2) is None, "the Unreleased section must not have a date"

    version_sections = _version_sections(text)
    assert version_sections, "CHANGELOG.md must contain version sections"
    assert unreleased[0].start() < version_sections[0].start(), (
        "the Unreleased section must appear before every version section"
    )

    version_names = [section.group(1) for section in version_sections]
    missing = [version for version in REQUIRED_VERSIONS if version not in version_names]
    assert not missing, f"CHANGELOG.md is missing published versions: {', '.join(missing)}"
    _assert_version_sections_strictly_descending(text)

    for section in version_sections:
        version, released_on = section.groups()
        assert released_on is not None, f"version {version} must have a YYYY-MM-DD date"
        try:
            parsed_date = date.fromisoformat(released_on)
        except ValueError:
            parsed_date = None
        assert parsed_date is not None and parsed_date.isoformat() == released_on, (
            f"version {version} must have a valid YYYY-MM-DD date"
        )

        next_section = NEXT_H2_RE.search(text, section.end())
        section_end = next_section.start() if next_section else len(text)
        section_body = text[section.end() : section_end]
        assert _has_category_with_bullet(section_body), (
            f"version {version} must contain a recognized category with a '- ' bullet"
        )


def test_version_order_rejects_out_of_order_fixture() -> None:
    fixture = """\
## [0.1.1] - 2026-07-15
## [9.0.0] - 2026-07-16
## [0.1.0] - 2026-07-14
"""

    with pytest.raises(AssertionError, match="newest first"):
        _assert_version_sections_strictly_descending(fixture)


def test_version_order_rejects_duplicate_fixture() -> None:
    fixture = """\
## [1.0.0] - 2026-07-16
## [1.0.0-alpha.1] - 2026-07-15
## [1.0.0-alpha.1] - 2026-07-14
"""

    with pytest.raises(AssertionError, match="must be unique"):
        _assert_version_sections_strictly_descending(fixture)


def test_version_order_rejects_misordered_beta_fixture() -> None:
    fixture = """\
## [1.0.0-beta.2] - 2026-07-16
## [1.0.0-beta.10] - 2026-07-15
## [1.0.0-alpha.1] - 2026-07-14
"""

    with pytest.raises(AssertionError, match="newest first"):
        _assert_version_sections_strictly_descending(fixture)


def test_version_order_rejects_unrecognized_heading_fixture() -> None:
    fixture = """\
## [1.0.0] - 2026-07-16
## [release-next] - 2026-07-15
"""

    with pytest.raises(AssertionError, match="unsupported changelog version heading"):
        _assert_version_sections_strictly_descending(fixture)
