"""Loader and comparator for the neutral tool-schema contract fixture."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


_MISSING = object()


@dataclass(frozen=True)
class ParamContract:
    name: str
    type: str
    required: bool
    enum: tuple[Any, ...] | None = None
    default: Any = _MISSING

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ParamContract":
        return cls(
            name=raw["name"],
            type=raw["type"],
            required=raw["required"],
            enum=tuple(raw["enum"]) if "enum" in raw else None,
            default=raw.get("default", _MISSING),
        )

    def to_mapping(self) -> dict[str, Any]:
        raw = {"name": self.name, "type": self.type, "required": self.required}
        if self.enum is not None:
            raw["enum"] = list(self.enum)
        if self.default is not _MISSING:
            raw["default"] = self.default
        return raw


@dataclass(frozen=True)
class ToolContract:
    name: str
    params: tuple[ParamContract, ...]

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, Any]) -> "ToolContract":
        return cls(
            name=name,
            params=tuple(ParamContract.from_mapping(item) for item in raw["params"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"params": [param.to_mapping() for param in self.params]}


def load_contract(path: Path) -> dict[str, ToolContract]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("contract fixture must contain a tool object")
    return {
        name: ToolContract.from_mapping(name, tool)
        for name, tool in raw.items()
    }


def dump_contract(contract: Mapping[str, ToolContract]) -> dict[str, Any]:
    return {name: tool.to_mapping() for name, tool in contract.items()}


def _advertised_types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    alternatives = schema.get("anyOf", ())
    return {
        item["type"]
        for item in alternatives
        if isinstance(item, Mapping) and isinstance(item.get("type"), str)
    }


def compare_tool_schema(
    advertised: Mapping[str, Any], contract: ToolContract
) -> list[str]:
    """Return compatibility mismatches for one advertised input schema."""
    errors: list[str] = []
    properties = advertised.get("properties", {})
    advertised_required = set(advertised.get("required", ()))
    fixture_required = {param.name for param in contract.params if param.required}
    extra_required = advertised_required - fixture_required
    if extra_required:
        errors.append(f"unexpected required params: {sorted(extra_required)}")

    for param in contract.params:
        if param.name not in properties:
            errors.append(f"missing accepted param: {param.name}")
            continue
        property_schema = properties[param.name]
        if param.type not in _advertised_types(property_schema):
            errors.append(f"type mismatch for {param.name}: expected {param.type}")
        if param.enum is not None and property_schema.get("enum") != list(param.enum):
            errors.append(f"enum mismatch for {param.name}")
        if (
            param.default is not _MISSING
            and property_schema.get("default", _MISSING) != param.default
        ):
            errors.append(f"default mismatch for {param.name}")
    return errors
