"""Compatibility checks for the clean-room default tool contract."""

import asyncio
import json
from pathlib import Path

from rustwright_mcp.server import mcp

from contract.contract_schema import (
    ParamContract,
    ToolContract,
    compare_tool_schema,
    dump_contract,
    load_contract,
)


FIXTURE = Path(__file__).parent / "fixtures" / "default_toolset.json"


def test_default_toolset_contract():
    contract = load_contract(FIXTURE)
    advertised = {
        tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())
    }

    for name, tool_contract in contract.items():
        assert name in advertised
        assert compare_tool_schema(advertised[name], tool_contract) == []


def test_contract_fixture_round_trip():
    contract = load_contract(FIXTURE)
    assert dump_contract(contract) == json.loads(FIXTURE.read_text())


def test_comparator_allows_required_subset_and_accepted_superset():
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

    assert compare_tool_schema(advertised, contract) == []


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

    assert compare_tool_schema(advertised, contract) == [
        "unexpected required params: ['extra']",
        "type mismatch for action: expected string",
        "enum mismatch for action",
        "default mismatch for action",
    ]
