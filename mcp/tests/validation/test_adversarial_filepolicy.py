"""Adversarial probes for screenshot confinement and output-cap boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import uuid

import pytest

from rustwright_mcp import server
from rustwright_mcp.filepolicy import FilePolicy, FilePolicyError


class ScreenshotMustNotRun:
    def screenshot(self, *, path, full_page) -> None:  # pragma: no cover
        raise AssertionError(f"screenshot unexpectedly reached filesystem path {path}")


class StaticRoots:
    def __init__(self, *roots: Path) -> None:
        self._roots = roots

    def roots(self):
        return self._roots


def test_screenshot_escape_variants_are_structured_and_write_nothing(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "output"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    policy = FilePolicy(output_root=root)
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)
    monkeypatch.setattr(server, "_page", lambda: ScreenshotMustNotRun())

    symlink = root / "linked"
    symlink.symlink_to(outside_dir, target_is_directory=True)
    absolute_tmp = Path("/tmp") / f"rustwright-validation-{uuid.uuid4().hex}.png"
    cases = [
        ("../traversal.png", tmp_path / "traversal.png"),
        ("linked/symlink.png", outside_dir / "symlink.png"),
        (str(absolute_tmp), absolute_tmp),
    ]

    for requested, escaped_path in cases:
        assert not escaped_path.exists()
        with pytest.raises(FilePolicyError) as error:
            server.browser_take_screenshot(filename=requested)
        assert str(error.value) == (
            "screenshot paths are confined to RUSTWRIGHT_MCP_OUTPUT_DIR "
            f"({policy.output_root}); got {requested}"
        )
        assert not escaped_path.exists()


def test_screenshot_symlink_swap_cannot_write_outside_root(monkeypatch) -> None:
    """A swap after exclusive reservation must not turn the write into an escape."""
    with tempfile.TemporaryDirectory(
        prefix="rustwright-mcp-validation-", dir="/tmp"
    ) as temporary:
        temporary_path = Path(temporary)
        policy = FilePolicy(output_root=temporary_path / "output")
        escaped = temporary_path / "escaped.png"
        monkeypatch.setattr(server, "get_file_policy", lambda: policy)

        class RacingPage:
            def screenshot(self, *, path, full_page, type) -> None:
                reserved = Path(path)
                reserved.unlink()
                reserved.symlink_to(escaped)
                reserved.write_bytes(b"escaped screenshot bytes")

        monkeypatch.setattr(server, "_page", lambda: RacingPage())

        with pytest.raises(FilePolicyError, match="escaped the configured root"):
            server.browser_take_screenshot(filename="race.png")
        assert not escaped.exists(), "screenshot bytes escaped through a symlink swap"


def test_cap_boundaries_and_oldest_first_eviction(tmp_path) -> None:
    policy = FilePolicy(
        output_root=tmp_path / "output",
        max_file_bytes=4,
        max_total_bytes=7,
    )
    old = policy.reserve_output("old.bin")
    old.write_bytes(b"old")
    assert policy.finalize_output(old) == "old.bin"
    os.utime(old, ns=(1, 1))

    middle = policy.reserve_output("middle.bin")
    middle.write_bytes(b"mid")
    assert policy.finalize_output(middle) == "middle.bin"
    os.utime(middle, ns=(2, 2))

    boundary = policy.reserve_output("boundary.bin")
    boundary.write_bytes(b"four")
    assert policy.finalize_output(boundary) == "boundary.bin"
    assert not old.exists()
    assert middle.exists()
    assert boundary.exists()

    oversized = policy.reserve_output("oversized.bin")
    oversized.write_bytes(b"12345")
    with pytest.raises(FilePolicyError, match="4-byte per-file cap"):
        policy.finalize_output(oversized)
    assert not oversized.exists()


def test_artifact_links_are_workspace_relative() -> None:
    with tempfile.TemporaryDirectory(
        prefix="rustwright-mcp-validation-", dir="/tmp"
    ) as temporary:
        workspace = Path(temporary) / "workspace"
        output_root = workspace / "artifacts"
        workspace.mkdir()
        policy = FilePolicy(
            output_root=output_root,
            roots_provider=StaticRoots(workspace),
        )
        output = policy.reserve_output("capture.bin")
        output.write_bytes(b"artifact")

        assert policy.finalize_output(output) == "artifacts/capture.bin"
