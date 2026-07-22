"""PR-4 schema, policy, synthesis, and conformance coverage for browser_drop."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

from rustwright_mcp import server

sys.path.insert(0, str(Path(__file__).parent))
from contract.contract_schema import ToolContract, compare_tool_schema
from test_pr2_compatibility import _stdio_tools
from test_smoke import _call, _result_section, _run_session


FIXTURE = Path(__file__).parent / "fixtures" / "drop_conformance.html"
CONTRACT_FIXTURE = (
    Path(__file__).parent / "contract" / "fixtures" / "default_toolset.json"
)
CONFORMANCE_OUTPUT = Path(__file__).parent / "output" / "browser_drop_conformance.json"


def _error_text(result) -> str:
    return "\n".join(item.text for item in result.content if item.type == "text")


def _schema_branch(schema: dict, expected_type: str) -> dict:
    if schema.get("type") == expected_type:
        return schema
    return next(
        branch
        for branch in schema.get("anyOf", [])
        if branch.get("type") == expected_type
    )


def test_drop_schema_contract_description_and_profiles() -> None:
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    advertised = tools["browser_drop"].inputSchema
    assert advertised["additionalProperties"] is False
    assert set(advertised["properties"]) == {"element", "target", "paths", "data"}
    assert set(advertised["required"]) == {"target"}
    assert _schema_branch(advertised["properties"]["paths"], "array")[
        "maxItems"
    ] == 50
    data_schema = _schema_branch(advertised["properties"]["data"], "object")
    assert data_schema["additionalProperties"] == {"type": "string"}
    assert (
        "synthesized drop events; sites checking event trust or native drag "
        "sessions may behave differently"
        in tools["browser_drop"].description
    )

    raw_contract = json.loads(CONTRACT_FIXTURE.read_text())
    expected = {
        "params": [
            {"name": "element", "type": "string", "required": False},
            {"name": "target", "type": "string", "required": True},
            {
                "name": "paths",
                "type": "array",
                "required": False,
                "items": {"type": "string"},
            },
            {
                "name": "data",
                "type": "object",
                "required": False,
                "additionalProperties": {"type": "string"},
            },
        ]
    }
    assert raw_contract["browser_drop"] == expected
    contract = ToolContract.from_mapping("browser_drop", expected)
    assert compare_tool_schema(advertised, contract) == []
    mismatched = copy.deepcopy(advertised)
    mismatched["properties"]["data"] = {"type": "array"}
    assert compare_tool_schema(mismatched, contract) == [
        "type mismatch for data: expected object, got ['array']",
        "missing additionalProperties schema for data",
    ]

    mirror, _ = asyncio.run(
        _stdio_tools(env_overrides={"RUSTWRIGHT_MCP_ALLOW_EVAL": "0"})
    )
    lean, _ = asyncio.run(
        _stdio_tools(
            env_overrides={
                "RUSTWRIGHT_MCP_TOOLSET": "lean",
                "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
            }
        )
    )
    assert "browser_drop" in mirror
    assert "browser_drop" not in lean


def test_drop_synthetic_conformance_matrix_and_record(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    text_file = workspace / "note.txt"
    text_file.write_text("plain file content\n")
    json_file = workspace / "record.json"
    json_file.write_text('{"answer":42}\n')

    async def checks(session) -> None:
        listed = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert "browser_drop" in listed
        assert "browser_evaluate" not in listed
        assert "synthetic DataTransfer semantics" in listed["browser_drop"].description

        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        standard = await _call(
            session,
            "browser_drop",
            target="#standard",
            element="Standard zone description",
            paths=[str(text_file), str(json_file)],
            data={
                "text/plain": "plain string",
                "application/x-conformance": "custom string",
            },
        )
        assert "Standard zone description" in standard
        assert "2 file(s) and 2 data item(s)" in standard
        assert "### Snapshot" in standard

        trust = await _call(
            session,
            "browser_drop",
            target="#trust",
            data={"text/plain": "trust probe"},
        )
        assert "### Snapshot" in trust
        items = await _call(
            session,
            "browser_drop",
            target="#items",
            paths=[str(text_file)],
            data={
                "text/plain": "ordered first",
                "application/x-conformance": "ordered second",
            },
        )
        assert "### Snapshot" in items
        framework = await _call(
            session,
            "browser_drop",
            target="#framework",
            data={"text/plain": "framework value"},
        )
        assert "### Snapshot" in framework

        await _call(session, "browser_wait_for", time=0.1)
        result = await _call(session, "browser_get_text", selector="#results")
        observed = json.loads(_result_section(result))

        assert observed["a"] == {
            "events": ["dragenter", "dragover", "drop"],
            "files": [
                {
                    "name": "note.txt",
                    "size": 19,
                    "type": "text/plain",
                    "content": "plain file content\n",
                },
                {
                    "name": "record.json",
                    "size": 14,
                    "type": "application/json",
                    "content": '{"answer":42}\n',
                },
            ],
            "data": {
                "text/plain": "plain string",
                "application/x-conformance": "custom string",
            },
            "status": "done",
        }
        assert observed["b"] == {
            "isTrusted": {"dragenter": False, "dragover": False, "drop": False}
        }
        assert observed["c"] == {
            "types": ["text/plain", "application/x-conformance", "Files"],
            "items": [
                {"kind": "file", "type": "text/plain"},
                {"kind": "string", "type": "text/plain"},
                {"kind": "string", "type": "application/x-conformance"},
            ],
        }
        assert observed["d"] == {
            "events": ["dragenter", "dragover", "drop"],
            "dragoverPrevented": True,
            "accepted": True,
            "value": "framework value",
        }

        record = {
            "implementation": "best-effort-synthetic-data-transfer",
            "cases": observed,
        }
        CONFORMANCE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        CONFORMANCE_OUTPUT.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
        print("browser_drop_conformance=" + json.dumps(record, sort_keys=True))

    asyncio.run(
        _run_session(
            checks,
            {
                "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
                "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
            },
        )
    )


def test_drop_runtime_rejections_are_structured(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    valid = workspace / "valid.txt"
    valid.write_text("valid")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    link = workspace / "link.txt"
    try:
        link.symlink_to(valid)
    except OSError:
        pytest.skip("symlinks are unavailable")

    async def checks(session) -> None:
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        cases = [
            (
                {"target": "#standard"},
                "at least one non-empty paths or data source",
            ),
            (
                {"target": "#standard", "paths": [], "data": {}},
                "at least one non-empty paths or data source",
            ),
            (
                {"target": "#standard", "paths": [str(outside)]},
                "outside the allowed workspace",
            ),
            (
                {"target": "#standard", "paths": [str(link)]},
                "must not be a symlink",
            ),
            (
                {"target": ".dropzone", "data": {"text/plain": "value"}},
                "matched 4 elements",
            ),
            (
                {"target": "#standard", "data": {"": "value"}},
                "MIME keys must be non-empty",
            ),
            (
                {
                    "target": "#standard",
                    "data": {"text/plain": "one", "TEXT/PLAIN": "two"},
                },
                "unique ignoring case",
            ),
            (
                {
                    "target": "#standard",
                    "data": {"text/plain": "value"},
                    "filePaths": [str(valid)],
                },
                "Extra inputs are not permitted",
            ),
        ]
        for arguments, expected in cases:
            result = await session.call_tool("browser_drop", arguments)
            assert result.isError, arguments
            assert expected in _error_text(result), _error_text(result)

    asyncio.run(
        _run_session(checks, {"RUSTWRIGHT_MCP_WORKSPACE": str(workspace)})
    )


def test_drop_oversized_file_rejection_is_structured(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    oversized = workspace / "oversized.bin"
    oversized.write_bytes(b"12345")

    async def checks(session) -> None:
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        result = await session.call_tool(
            "browser_drop",
            {"target": "#standard", "paths": [str(oversized)]},
        )
        assert result.isError
        assert "4-byte per-file cap" in _error_text(result)

    asyncio.run(
        _run_session(
            checks,
            {
                "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
                "RUSTWRIGHT_MCP_OUTPUT_MAX_FILE_BYTES": "4",
            },
        )
    )


def test_drop_total_input_cap_rejection_is_structured(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.bin"
    second = workspace / "second.bin"
    first.write_bytes(b"123")
    second.write_bytes(b"456")

    async def checks(session) -> None:
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        result = await session.call_tool(
            "browser_drop",
            {"target": "#standard", "paths": [str(first), str(second)]},
        )
        assert result.isError
        assert "5-byte total cap" in _error_text(result)

    asyncio.run(
        _run_session(
            checks,
            {
                "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
                "RUSTWRIGHT_MCP_OUTPUT_MAX_FILE_BYTES": "4",
                "RUSTWRIGHT_MCP_OUTPUT_MAX_TOTAL_BYTES": "5",
            },
        )
    )
