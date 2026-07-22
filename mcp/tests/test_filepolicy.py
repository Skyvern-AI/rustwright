"""Tests for confined MCP file inputs and outputs."""

import asyncio
import os
from pathlib import Path
import re
import stat

import pytest

from rustwright_mcp.filepolicy import FilePolicy, FilePolicyError
from rustwright_mcp import server
from test_smoke import FIXTURE, _call, _run_session


class StaticRoots:
    def __init__(self, *roots):
        self._roots = roots

    def roots(self):
        return self._roots


def test_output_containment_exclusive_create_and_permissions(tmp_path):
    root = tmp_path / "output"
    policy = FilePolicy(output_root=root)
    outside = tmp_path / "outside.png"

    with pytest.raises(FilePolicyError) as error:
        policy.reserve_output(str(outside), purpose="screenshot")
    assert str(error.value) == (
        f"screenshot paths are confined to RUSTWRIGHT_MCP_OUTPUT_DIR "
        f"({root}); got {outside}"
    )

    output = policy.reserve_output("nested/capture.png", purpose="screenshot")
    assert output == root / "nested" / "capture.png"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FilePolicyError, match="already exists"):
        policy.reserve_output("nested/capture.png", purpose="screenshot")


def test_output_rejects_symlink_parent(tmp_path):
    root = tmp_path / "output"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    policy = FilePolicy(output_root=root)
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FilePolicyError, match="confined"):
        policy.reserve_output("linked/capture.png", purpose="screenshot")
    assert not (outside / "capture.png").exists()


def test_input_requires_allowed_regular_non_symlink_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "upload.txt"
    source.write_text("content")
    policy = FilePolicy(
        output_root=tmp_path / "output",
        roots_provider=StaticRoots(workspace),
    )

    assert policy.validate_input(source) == source
    with pytest.raises(FilePolicyError, match="absolute"):
        policy.validate_input(Path("upload.txt"))

    link = workspace / "link.txt"
    link.symlink_to(source)
    with pytest.raises(FilePolicyError, match="must not be a symlink"):
        policy.validate_input(link)

    outside = tmp_path / "outside.txt"
    outside.write_text("content")
    with pytest.raises(FilePolicyError, match="outside the allowed workspace"):
        policy.validate_input(outside)


def test_per_file_cap_and_oldest_first_total_eviction(tmp_path):
    policy = FilePolicy(
        output_root=tmp_path / "output",
        max_file_bytes=4,
        max_total_bytes=6,
    )
    old = policy.reserve_output("old.bin")
    old.write_bytes(b"1234")
    policy.finalize_output(old)
    os.utime(old, ns=(1, 1))

    new = policy.reserve_output("new.bin")
    new.write_bytes(b"5678")
    assert policy.finalize_output(new) == "new.bin"
    assert not old.exists()
    assert new.exists()

    too_large = policy.reserve_output("large.bin")
    too_large.write_bytes(b"12345")
    with pytest.raises(FilePolicyError, match="per-file cap"):
        policy.finalize_output(too_large)
    assert not too_large.exists()


def test_existing_configured_root_file_is_never_evicted_or_chmodded(
    monkeypatch, tmp_path
):
    root = tmp_path / "existing-output"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    preexisting = root / "unrelated.txt"
    preexisting.write_text("keep this unrelated file")
    monkeypatch.setenv("RUSTWRIGHT_MCP_OUTPUT_DIR", str(root))
    policy = FilePolicy(max_file_bytes=4, max_total_bytes=6)

    old = policy.reserve_output("old.bin")
    old.write_bytes(b"1234")
    policy.finalize_output(old)
    os.utime(old, ns=(1, 1))
    new = policy.reserve_output("new.bin")
    new.write_bytes(b"5678")
    policy.finalize_output(new)

    assert not old.exists()
    assert new.exists()
    assert preexisting.read_text() == "keep this unrelated file"
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_screenshot_policy_error_and_no_path_artifact(monkeypatch, tmp_path):
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)

    outside = tmp_path / "outside.png"
    with pytest.raises(FilePolicyError) as error:
        server.browser_take_screenshot(filename=str(outside))
    assert str(error.value) == (
        f"screenshot paths are confined to RUSTWRIGHT_MCP_OUTPUT_DIR "
        f"({policy.output_root}); got {outside}"
    )

    class FakePage:
        def screenshot(self, *, path, full_page, type, scale):
            Path(path).write_bytes(b"PNG")

    monkeypatch.setattr(server, "_page", lambda: FakePage())
    response = server.browser_take_screenshot()
    artifact = re.search(r"`([^`]+)`", response).group(1)
    assert not Path(artifact).is_absolute()
    assert (policy.output_root / artifact).read_bytes() == b"PNG"


def test_stdio_screenshot_outside_root_is_structured_error(tmp_path):
    output_root = tmp_path / "output"
    outside = tmp_path / "outside.png"

    async def checks(session):
        result = await session.call_tool(
            "browser_take_screenshot", {"path": str(outside)}
        )
        text = "\n".join(item.text for item in result.content if item.type == "text")
        assert result.isError
        assert (
            f"screenshot paths are confined to RUSTWRIGHT_MCP_OUTPUT_DIR "
            f"({output_root}); got {outside}"
        ) in text

    asyncio.run(
        _run_session(
            checks,
            {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(output_root)},
        )
    )


def test_stdio_screenshot_without_path_stays_in_output_root(tmp_path):
    output_root = tmp_path / "output"

    async def checks(session):
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        response = await _call(session, "browser_take_screenshot")
        artifact = re.search(r"`([^`]+)`", response).group(1)
        assert not Path(artifact).is_absolute()
        screenshot = output_root / artifact
        assert screenshot.read_bytes().startswith(b"\x89PNG")
        assert stat.S_IMODE(screenshot.stat().st_mode) == 0o600
        await _call(session, "browser_close")

    asyncio.run(
        _run_session(
            checks,
            {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(output_root)},
        )
    )
