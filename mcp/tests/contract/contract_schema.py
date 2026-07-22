"""Loader and comparator for the neutral tool-schema contract fixture."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


_MISSING = object()


@dataclass(frozen=True)
class SchemaContract:
    type: str
    enum: tuple[Any, ...] | None = None
    default: Any = _MISSING
    params: tuple["ParamContract", ...] = ()
    items: "SchemaContract | None" = None
    additional_properties: "SchemaContract | None" = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SchemaContract":
        return cls(
            type=raw["type"],
            enum=tuple(raw["enum"]) if "enum" in raw else None,
            default=raw.get("default", _MISSING),
            params=tuple(
                ParamContract.from_mapping(item) for item in raw.get("params", ())
            ),
            items=(
                cls.from_mapping(raw["items"])
                if "items" in raw
                else None
            ),
            additional_properties=(
                cls.from_mapping(raw["additionalProperties"])
                if isinstance(raw.get("additionalProperties"), Mapping)
                else None
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"type": self.type}
        if self.enum is not None:
            raw["enum"] = list(self.enum)
        if self.default is not _MISSING:
            raw["default"] = self.default
        if self.params:
            raw["params"] = [param.to_mapping() for param in self.params]
        if self.items is not None:
            raw["items"] = self.items.to_mapping()
        if self.additional_properties is not None:
            raw["additionalProperties"] = self.additional_properties.to_mapping()
        return raw


@dataclass(frozen=True)
class ParamContract(SchemaContract):
    name: str = ""
    required: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ParamContract":
        shape = SchemaContract.from_mapping(raw)
        return cls(
            name=raw["name"],
            required=raw["required"],
            type=shape.type,
            enum=shape.enum,
            default=shape.default,
            params=shape.params,
            items=shape.items,
            additional_properties=shape.additional_properties,
        )

    def to_mapping(self) -> dict[str, Any]:
        raw = super().to_mapping()
        return {"name": self.name, **raw, "required": self.required}


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


def _advertised_enum(schema: Mapping[str, Any]) -> list[Any] | None:
    candidates = [schema, *schema.get("anyOf", ())]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        enum = candidate.get("enum")
        if isinstance(enum, list):
            return enum
    return None


def _resolve_ref(
    schema: Mapping[str, Any], definitions: Mapping[str, Any]
) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return schema
    name = reference.removeprefix("#/$defs/")
    resolved = definitions.get(name)
    return resolved if isinstance(resolved, Mapping) else schema


def _schema_for_type(
    schema: Mapping[str, Any], expected_type: str, definitions: Mapping[str, Any]
) -> Mapping[str, Any]:
    resolved = _resolve_ref(schema, definitions)
    if resolved.get("type") == expected_type:
        return resolved
    for candidate in resolved.get("anyOf", ()):
        if not isinstance(candidate, Mapping):
            continue
        candidate = _resolve_ref(candidate, definitions)
        if candidate.get("type") == expected_type:
            return candidate
    return resolved


def _compare_params(
    properties: Mapping[str, Any],
    advertised_required: set[str],
    expected_params: tuple[ParamContract, ...],
    definitions: Mapping[str, Any],
    path: str,
    errors: list[str],
) -> None:
    expected_by_name = {param.name: param for param in expected_params}
    advertised_names = set(properties)
    expected_names = set(expected_by_name)
    missing = expected_names - advertised_names
    unexpected = advertised_names - expected_names
    if missing:
        errors.append(f"{path}missing params: {sorted(missing)}")
    if unexpected:
        errors.append(f"{path}unexpected params: {sorted(unexpected)}")

    expected_required = {
        param.name for param in expected_params if param.required
    }
    if advertised_required != expected_required:
        errors.append(
            f"{path}required params mismatch: expected "
            f"{sorted(expected_required)}, got {sorted(advertised_required)}"
        )

    for name in sorted(advertised_names & expected_names):
        property_schema = properties[name]
        if not isinstance(property_schema, Mapping):
            errors.append(f"{path}{name} schema is not an object")
            continue
        _compare_shape(
            property_schema,
            expected_by_name[name],
            definitions,
            f"{path}{name}",
            errors,
        )


def _compare_shape(
    advertised: Mapping[str, Any],
    expected: SchemaContract,
    definitions: Mapping[str, Any],
    path: str,
    errors: list[str],
) -> None:
    resolved_advertised = _resolve_ref(advertised, definitions)
    advertised_types = _advertised_types(resolved_advertised) - {"null"}
    if advertised_types != {expected.type}:
        errors.append(
            f"type mismatch for {path}: expected {expected.type}, "
            f"got {sorted(advertised_types)}"
        )

    advertised_enum = _advertised_enum(resolved_advertised)
    expected_enum = None if expected.enum is None else list(expected.enum)
    if advertised_enum != expected_enum:
        errors.append(
            f"enum mismatch for {path}: expected {expected_enum}, "
            f"got {advertised_enum}"
        )

    advertised_default = advertised.get(
        "default", resolved_advertised.get("default", _MISSING)
    )
    if advertised_default is None and expected.default is _MISSING:
        advertised_default = _MISSING
    if advertised_default != expected.default:
        shown_expected = "<missing>" if expected.default is _MISSING else expected.default
        shown_actual = "<missing>" if advertised_default is _MISSING else advertised_default
        errors.append(
            f"default mismatch for {path}: expected {shown_expected!r}, "
            f"got {shown_actual!r}"
        )

    typed_schema = _schema_for_type(
        resolved_advertised, expected.type, definitions
    )
    if expected.type == "array":
        advertised_items = typed_schema.get("items")
        if expected.items is None:
            if isinstance(advertised_items, Mapping):
                errors.append(f"unexpected items schema for {path}")
        elif not isinstance(advertised_items, Mapping):
            errors.append(f"missing items schema for {path}")
        else:
            _compare_shape(
                advertised_items,
                expected.items,
                definitions,
                f"{path}[]",
                errors,
            )

    if expected.type == "object":
        advertised_properties = typed_schema.get("properties", {})
        if not isinstance(advertised_properties, Mapping):
            advertised_properties = {}
        _compare_params(
            advertised_properties,
            set(typed_schema.get("required", ())),
            expected.params,
            definitions,
            f"{path}.",
            errors,
        )
        advertised_additional = typed_schema.get("additionalProperties")
        if expected.additional_properties is None:
            if isinstance(advertised_additional, Mapping):
                errors.append(f"unexpected additionalProperties schema for {path}")
        elif not isinstance(advertised_additional, Mapping):
            errors.append(f"missing additionalProperties schema for {path}")
        else:
            _compare_shape(
                advertised_additional,
                expected.additional_properties,
                definitions,
                f"{path}.*",
                errors,
            )


def compare_tool_schema(
    advertised: Mapping[str, Any], contract: ToolContract
) -> list[str]:
    """Return compatibility mismatches for one advertised input schema."""
    errors: list[str] = []
    properties = advertised.get("properties", {})
    if not isinstance(properties, Mapping):
        return ["tool properties schema is not an object"]
    definitions = advertised.get("$defs", {})
    if not isinstance(definitions, Mapping):
        definitions = {}
    _compare_params(
        properties,
        set(advertised.get("required", ())),
        contract.params,
        definitions,
        "",
        errors,
    )
    return errors


def compare_tool_inventory(
    advertised: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, ToolContract],
    *,
    allowed_additions: frozenset[str] = frozenset(),
) -> list[str]:
    """Compare the exact tool inventory and every normalized input schema."""
    errors: list[str] = []
    advertised_names = set(advertised)
    contract_names = set(contract)
    missing = contract_names - advertised_names
    unexpected = advertised_names - contract_names - set(allowed_additions)
    if missing:
        errors.append(f"missing tools: {sorted(missing)}")
    if unexpected:
        errors.append(f"unpinned tools: {sorted(unexpected)}")
    for name in sorted(contract_names & advertised_names):
        for mismatch in compare_tool_schema(advertised[name], contract[name]):
            errors.append(f"{name}: {mismatch}")
    return errors
