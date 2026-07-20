"""Independent adversarial validation for PR-4 browser_drop."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
import sys

import pytest

from rustwright_mcp import server

sys.path.insert(0, str(Path(__file__).parents[1]))
from contract.contract_schema import ToolContract, compare_tool_schema
from test_pr2_compatibility import _stdio_tools
from test_smoke import _call, _result_section, _run_session


# Independently transcribed from the audited compatibility surface, not loaded
# from the repository contract fixture under test.
AUDITED_BROWSER_DROP = {
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


ADVERSARIAL_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Adversarial Drop Validation</title></head>
<body>
  <div id="standard">standard</div>
  <div id="requires-over">requires dragover cancellation</div>
  <div id="reject-untrusted">reject untrusted</div>
  <div id="security">security</div>
  <pre id="standard-result"></pre>
  <pre id="over-result">idle</pre>
  <pre id="reject-result">idle</pre>
  <pre id="security-result">[]</pre>
  <pre id="frame-result">idle</pre>
  <iframe id="embedded"></iframe>
  <script>
    const standard = {
      events: [],
      files: [],
      data: {},
      types: [],
      items: [],
      isTrusted: {},
      status: "idle",
    };
    const standardResult = document.querySelector("#standard-result");
    const renderStandard = () => {
      standardResult.textContent = JSON.stringify(standard);
    };
    const fileAsBase64 = (file) => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error);
      reader.onload = () => {
        const bytes = new Uint8Array(reader.result);
        let binary = "";
        for (const byte of bytes) binary += String.fromCharCode(byte);
        resolve(btoa(binary));
      };
      reader.readAsArrayBuffer(file);
    });

    const standardZone = document.querySelector("#standard");
    for (const type of ["dragenter", "dragover"]) {
      standardZone.addEventListener(type, (event) => {
        if (type === "dragover") event.preventDefault();
        standard.events.push(type);
        standard.isTrusted[type] = event.isTrusted;
        renderStandard();
      });
    }
    standardZone.addEventListener("drop", async (event) => {
      event.preventDefault();
      standard.events.push("drop");
      standard.isTrusted.drop = event.isTrusted;
      standard.types = Array.from(event.dataTransfer.types);
      standard.items = Array.from(event.dataTransfer.items, (item) => ({
        kind: item.kind,
        type: item.type,
      }));
      standard.data = {
        text: event.dataTransfer.getData("text/plain"),
        custom: event.dataTransfer.getData("application/x-adversarial"),
      };
      standard.files = await Promise.all(
        Array.from(event.dataTransfer.files, async (file) => ({
          name: file.name,
          size: file.size,
          type: file.type,
          base64: await fileAsBase64(file),
        }))
      );
      standard.status = "done";
      renderStandard();
    });
    renderStandard();

    let overArmed = false;
    const overZone = document.querySelector("#requires-over");
    overZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      overArmed = event.defaultPrevented;
    });
    overZone.addEventListener("drop", (event) => {
      document.querySelector("#over-result").textContent = JSON.stringify({
        accepted: overArmed,
        isTrusted: event.isTrusted,
        value: overArmed ? event.dataTransfer.getData("text/plain") : null,
      });
    });

    document.querySelector("#reject-untrusted").addEventListener("dragover", (event) => {
      event.preventDefault();
    });
    document.querySelector("#reject-untrusted").addEventListener("drop", (event) => {
      document.querySelector("#reject-result").textContent =
        event.isTrusted ? "accepted" : "rejected-untrusted";
    });

    const securityEvents = [];
    const securityZone = document.querySelector("#security");
    const renderSecurity = () => {
      document.querySelector("#security-result").textContent =
        JSON.stringify(securityEvents);
    };
    securityZone.addEventListener("dragover", (event) => event.preventDefault());
    securityZone.addEventListener("drop", async (event) => {
      const record = {
        files: [],
        items: Array.from(event.dataTransfer.items, (item) => ({
          kind: item.kind,
          type: item.type,
        })),
        plain: event.dataTransfer.getData("text/plain"),
        uri: event.dataTransfer.getData("text/uri-list"),
      };
      record.files = await Promise.all(
        Array.from(event.dataTransfer.files, async (file) => ({
          name: file.name,
          base64: await fileAsBase64(file),
        }))
      );
      securityEvents.push(record);
      renderSecurity();
    });

    window.addEventListener("message", (event) => {
      if (event.data === "frame-drop") {
        document.querySelector("#frame-result").textContent = "dropped";
      }
    });
    document.querySelector("#embedded").srcdoc = `
      <div id="frame-zone">frame dropzone</div>
      <script>
        const zone = document.querySelector("#frame-zone");
        zone.addEventListener("dragover", (event) => event.preventDefault());
        zone.addEventListener("drop", () => parent.postMessage("frame-drop", "*"));
      <\\/script>`;
  </script>
</body>
</html>
"""


def _error_text(result) -> str:
    return "\n".join(item.text for item in result.content if item.type == "text")


def _write_page(tmp_path: Path) -> Path:
    page = tmp_path / "adversarial_drop.html"
    page.write_text(ADVERSARIAL_PAGE)
    return page


def test_schema_fixture_comparator_description_and_mirror_registration() -> None:
    listed = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    tool = listed["browser_drop"]
    schema = tool.inputSchema
    assert set(schema["properties"]) == {"element", "target", "paths", "data"}
    assert schema["required"] == ["target"]
    assert schema["additionalProperties"] is False
    assert "synthetic DataTransfer semantics" in tool.description
    assert "sites checking event trust or native drag sessions may behave differently" in (
        tool.description
    )

    fixture_path = (
        Path(__file__).parents[1] / "contract" / "fixtures" / "default_toolset.json"
    )
    fixture = json.loads(fixture_path.read_text())
    assert fixture["browser_drop"] == AUDITED_BROWSER_DROP
    contract = ToolContract.from_mapping("browser_drop", AUDITED_BROWSER_DROP)
    assert compare_tool_schema(schema, contract) == []
    incompatible = copy.deepcopy(schema)
    incompatible["properties"]["paths"] = {"type": "object"}
    assert compare_tool_schema(incompatible, contract) == [
        "type mismatch for paths: expected array, got ['object']",
        "missing items schema for paths",
    ]

    mirror, _ = asyncio.run(
        _stdio_tools(env_overrides={"RUSTWRIGHT_MCP_ALLOW_EVAL": "0"})
    )
    lean, _ = asyncio.run(
        _stdio_tools(
            env_overrides={
                "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
                "RUSTWRIGHT_MCP_TOOLSET": "lean",
            }
        )
    )
    assert "browser_drop" in mirror
    assert "browser_drop" not in lean


def test_real_browser_files_data_order_trust_dragover_iframe_and_rejection(
    tmp_path,
) -> None:
    page = _write_page(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_bytes = b"\x00adversarial-drop\xff\n"
    source = workspace / "payload.bin"
    source.write_bytes(source_bytes)

    async def checks(session) -> None:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        description = tools["browser_drop"].description
        assert "synthetic DataTransfer semantics" in description
        assert "event trust or native drag sessions may behave differently" in description

        await _call(session, "browser_navigate", url=page.as_uri())
        response = await _call(
            session,
            "browser_drop",
            target="#standard",
            paths=[str(source)],
            data={
                "text/plain": "mixed text",
                "application/x-adversarial": "custom value",
            },
        )
        assert "Synthesized a drop of 1 file(s) and 2 data item(s)" in response
        await _call(session, "browser_wait_for", time=0.1)
        observed = json.loads(
            _result_section(
                await _call(session, "browser_get_text", selector="#standard-result")
            )
        )
        assert observed == {
            "events": ["dragenter", "dragover", "drop"],
            "files": [
                {
                    "name": "payload.bin",
                    "size": len(source_bytes),
                    "type": "application/octet-stream",
                    "base64": base64.b64encode(source_bytes).decode("ascii"),
                }
            ],
            "data": {"text": "mixed text", "custom": "custom value"},
            "types": ["text/plain", "application/x-adversarial", "Files"],
            "items": [
                {"kind": "file", "type": "application/octet-stream"},
                {"kind": "string", "type": "text/plain"},
                {"kind": "string", "type": "application/x-adversarial"},
            ],
            "isTrusted": {"dragenter": False, "dragover": False, "drop": False},
            "status": "done",
        }

        await _call(
            session,
            "browser_drop",
            target="#requires-over",
            data={"text/plain": "dragover-gated"},
        )
        over = json.loads(
            _result_section(
                await _call(session, "browser_get_text", selector="#over-result")
            )
        )
        assert over == {
            "accepted": True,
            "isTrusted": False,
            "value": "dragover-gated",
        }

        frame = await session.call_tool(
            "browser_drop",
            {"target": "#frame-zone", "data": {"text/plain": "frame"}},
        )
        assert frame.isError
        assert "matched no elements" in _error_text(frame)
        assert _result_section(
            await _call(session, "browser_get_text", selector="#frame-result")
        ) == "idle"

        rejected = await _call(
            session,
            "browser_drop",
            target="#reject-untrusted",
            data={"text/plain": "untrusted"},
        )
        assert "Synthesized a drop" in rejected
        assert _result_section(
            await _call(session, "browser_get_text", selector="#reject-result")
        ) == "rejected-untrusted"
        assert "Adversarial Drop Validation" in await _call(
            session, "browser_snapshot"
        )

    asyncio.run(
        _run_session(
            checks,
            {
                "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
                "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
            },
        )
    )


def test_rejected_paths_never_reach_page_and_data_paths_remain_strings(
    tmp_path,
) -> None:
    page = _write_page(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    escape_marker = "ESCAPE-CONTENT-MUST-NOT-REACH-PAGE"
    outside.write_text(escape_marker)
    symlink_target = workspace / "symlink-target.txt"
    symlink_marker = "SYMLINK-CONTENT-MUST-NOT-REACH-PAGE"
    symlink_target.write_text(symlink_marker)
    symlink = workspace / "symlink.txt"
    try:
        symlink.symlink_to(symlink_target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    oversized = workspace / "oversized.bin"
    oversize_marker = "OVERSIZE-CONTENT-MUST-NOT-REACH-PAGE"
    oversized.write_text(oversize_marker)

    async def checks(session) -> None:
        await _call(session, "browser_navigate", url=page.as_uri())
        rejected = [
            (
                {"target": "#security", "paths": [str(outside)]},
                "outside the allowed workspace",
            ),
            (
                {"target": "#security", "paths": [str(symlink)]},
                "must not be a symlink",
            ),
            (
                {"target": "#security", "paths": [str(oversized)]},
                "8-byte per-file cap",
            ),
        ]
        for arguments, expected_error in rejected:
            result = await session.call_tool("browser_drop", arguments)
            assert result.isError
            assert expected_error in _error_text(result)
            page_state = _result_section(
                await _call(session, "browser_get_text", selector="#security-result")
            )
            assert page_state == "[]"
            assert escape_marker not in page_state
            assert symlink_marker not in page_state
            assert oversize_marker not in page_state

        await _call(
            session,
            "browser_drop",
            target="#security",
            data={
                "text/plain": str(outside),
                "text/uri-list": outside.as_uri(),
            },
        )
        data_only = json.loads(
            _result_section(
                await _call(session, "browser_get_text", selector="#security-result")
            )
        )
        assert data_only == [
            {
                "files": [],
                "items": [
                    {"kind": "string", "type": "text/plain"},
                    {"kind": "string", "type": "text/uri-list"},
                ],
                "plain": str(outside),
                "uri": outside.as_uri(),
            }
        ]
        assert escape_marker not in json.dumps(data_only)

    asyncio.run(
        _run_session(
            checks,
            {
                "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
                "RUSTWRIGHT_MCP_OUTPUT_MAX_FILE_BYTES": "8",
                "RUSTWRIGHT_MCP_OUTPUT_MAX_TOTAL_BYTES": "16",
                "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
            },
        )
    )
