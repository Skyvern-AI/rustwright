"""Compatibility checks for the clean-room default tool contract."""

import asyncio
import json
from pathlib import Path

from rustwright_mcp.server import mcp

from contract.contract_schema import (
    ParamContract,
    SchemaContract,
    ToolContract,
    compare_tool_inventory,
    compare_tool_schema,
    dump_contract,
    load_contract,
)


FIXTURE = Path(__file__).parent / "fixtures" / "default_toolset.json"
INTENTIONAL_TOOL_ADDITIONS: frozenset[str] = frozenset()


def test_default_toolset_contract():
    contract = load_contract(FIXTURE)
    advertised = {
        tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())
    }

    assert compare_tool_inventory(
        advertised,
        contract,
        allowed_additions=INTENTIONAL_TOOL_ADDITIONS,
    ) == []


def test_contract_fixture_round_trip():
    contract = load_contract(FIXTURE)
    assert dump_contract(contract) == json.loads(FIXTURE.read_text())


def test_comparator_rejects_optional_superset_and_required_drift():
    contract = ToolContract(
        name="example_tool",
        params=(ParamContract(name="target", type="string", required=True),),
    )
    advertised = {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "extension": {"type": "boolean", "default": False},
        },
        "required": [],
    }

    assert compare_tool_schema(advertised, contract) == [
        "unexpected params: ['extension']",
        "required params mismatch: expected ['target'], got []",
    ]


def test_inventory_rejects_unpinned_tool():
    contract = {
        "pinned": ToolContract(name="pinned", params=()),
    }
    empty_schema = {"type": "object", "properties": {}}

    assert compare_tool_inventory(
        {"pinned": empty_schema, "silent_addition": empty_schema}, contract
    ) == ["unpinned tools: ['silent_addition']"]


def test_comparator_rejects_nested_schema_change():
    contract = ToolContract(
        name="nested",
        params=(
            ParamContract(
                name="items",
                type="array",
                required=True,
                items=SchemaContract(type="string"),
            ),
        ),
    )
    advertised = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["items"],
    }

    assert compare_tool_schema(advertised, contract) == [
        "type mismatch for items[]: expected string, got ['integer']"
    ]


def test_deliberate_contract_mismatch_is_reported():
    contract = ToolContract(
        name="example_tool",
        params=(
            ParamContract(
                name="action",
                type="string",
                required=False,
                enum=("one", "two"),
                default="one",
            ),
        ),
    )
    advertised = {
        "type": "object",
        "properties": {
            "action": {"type": "integer", "enum": ["one"], "default": "two"},
            "extra": {"type": "string"},
        },
        "required": ["extra"],
    }

    errors = compare_tool_schema(advertised, contract)
    assert "unexpected params: ['extra']" in errors
    assert (
        "required params mismatch: expected [], got ['extra']" in errors
    )
    assert "type mismatch for action: expected string, got ['integer']" in errors
    assert "enum mismatch for action: expected ['one', 'two'], got ['one']" in errors
    assert "default mismatch for action: expected 'one', got 'two'" in errors
