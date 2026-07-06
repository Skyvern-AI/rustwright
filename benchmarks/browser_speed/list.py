#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    rows = []
    for descriptor_path in sorted((ROOT / "benchmarks").glob("*/benchmark.json")):
        data = json.loads(descriptor_path.read_text(encoding="utf-8"))
        rows.append(
            [
                data.get("id", descriptor_path.parent.name),
                data.get("category", ""),
                data.get("primary_use", ""),
                data.get("default_command", ""),
            ]
        )

    print("| Benchmark | Category | Primary use | Default command |")
    print("| --- | --- | --- | --- |")
    for benchmark_id, category, primary_use, command in rows:
        command = f"`{command}`" if command else ""
        print(f"| {benchmark_id} | {category} | {primary_use} | {command} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
