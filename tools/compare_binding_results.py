#!/usr/bin/env python3
"""Assert every language's benchmark runner produced equivalent results.

Given a directory of per-language result files (`<lang>.json`, the output of
`runner --out`), verify that:
  1. every language ran the same set of case ids,
  2. every case is ok=true in every language,
  3. for each case, the captured values are IDENTICAL across all languages
     (structural JSON equality — including screenshot byte lengths).

Different FFI stacks driving the same engine must agree exactly; a mismatch is
a correctness failure, not timing variance. Exits non-zero on any discrepancy.

Usage: python3 tools/compare_binding_results.py <results-dir>
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def load(results_dir: Path) -> dict[str, dict]:
    langs = {}
    for p in sorted(results_dir.glob("*.json")):
        data = json.loads(p.read_text())
        lang = data.get("lang") or p.stem
        langs[lang] = {r["id"]: r for r in data["results"]}
    return langs


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: compare_binding_results.py <results-dir>", file=sys.stderr)
        return 2
    results_dir = Path(sys.argv[1])
    langs = load(results_dir)
    if len(langs) < 2:
        print(f"ERROR: need >=2 language result files, found {sorted(langs)}", file=sys.stderr)
        return 1

    problems: list[str] = []

    # 1. same set of case ids
    id_sets = {lang: set(cases) for lang, cases in langs.items()}
    all_ids = set().union(*id_sets.values())
    for lang, ids in id_sets.items():
        missing = all_ids - ids
        if missing:
            problems.append(f"{lang}: missing cases {sorted(missing)}")

    # 2. every case ok in every language
    for lang, cases in langs.items():
        failed = [cid for cid, r in cases.items() if not r.get("ok")]
        if failed:
            problems.append(f"{lang}: failing cases {sorted(failed)}")

    # 3. identical captures per case across languages
    reference_lang = sorted(langs)[0]
    for cid in sorted(all_ids):
        baseline = langs.get(reference_lang, {}).get(cid, {}).get("captures")
        for lang, cases in langs.items():
            if cid not in cases:
                continue
            caps = cases[cid].get("captures")
            if caps != baseline:
                problems.append(
                    f"capture mismatch '{cid}': {reference_lang}={json.dumps(baseline, sort_keys=True)} "
                    f"vs {lang}={json.dumps(caps, sort_keys=True)}"
                )

    if problems:
        print("CROSS-LANGUAGE EQUIVALENCE FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    n_cases = len(all_ids)
    print(f"OK: {len(langs)} languages ({', '.join(sorted(langs))}) agree on all {n_cases} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
