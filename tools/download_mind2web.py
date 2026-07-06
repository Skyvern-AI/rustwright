#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".benchmark-data" / "raw" / "mind2web"
DATASET_API = "https://huggingface.co/api/datasets/osunlp/Mind2Web/tree/main?recursive=1"
RESOLVE_BASE = "https://huggingface.co/datasets/osunlp/Mind2Web/resolve/main"


def dataset_files() -> list[dict[str, Any]]:
    with urllib.request.urlopen(DATASET_API, timeout=30) as response:
        values = json.loads(response.read().decode("utf-8"))
    return [item for item in values if item.get("type") == "file"]


def selected_paths(files: list[dict[str, Any]], args: argparse.Namespace) -> list[str]:
    available = {item["path"] for item in files}
    if args.file:
        missing = [path for path in args.file if path not in available]
        if missing:
            raise SystemExit(f"unknown Mind2Web file(s): {', '.join(missing)}")
        return list(args.file)
    if args.train_shard is not None:
        path = f"data/train/train_{args.train_shard}.json"
        if path not in available:
            raise SystemExit(f"unknown Mind2Web train shard: {path}")
        return [path]
    if args.test_zip:
        return ["test.zip"]
    if args.all_train:
        return sorted(path for path in available if path.startswith("data/train/") and path.endswith(".json"))
    return ["data/train/train_10.json"]


def download_file(path: str, output_root: Path, *, force: bool = False) -> Path:
    target = output_root / path
    if target.is_file() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{RESOLVE_BASE}/{path}?download=true"
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Mind2Web files from Hugging Face into ignored benchmark data.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--file", action="append", help="Exact Hugging Face repo path to download. Repeatable.")
    parser.add_argument("--train-shard", type=int, help="Download data/train/train_<N>.json.")
    parser.add_argument("--test-zip", action="store_true", help="Download test.zip.")
    parser.add_argument("--all-train", action="store_true", help="Download all train shards. This is several GB.")
    parser.add_argument("--list", action="store_true", help="List available files and sizes without downloading.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    files = dataset_files()
    if args.list:
        rows = [{"path": item["path"], "size": item.get("size")} for item in files]
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            for item in rows:
                print(f"{item['path']}\t{item.get('size')}")
        return 0

    paths = selected_paths(files, args)
    downloaded = [download_file(path, args.output, force=args.force) for path in paths]
    result = {
        "output": str(args.output),
        "downloaded": [str(path) for path in downloaded],
        "count": len(downloaded),
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for path in downloaded:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
