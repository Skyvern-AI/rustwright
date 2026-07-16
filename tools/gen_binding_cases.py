#!/usr/bin/env python3
"""Generate the cross-language binding benchmark manifest (bindings/cases/full.json).

This is a PLACEHOLDER 100-case suite so the full cross-language benchmark is
runnable today and the harness is proven at scale. It is deterministic and
hermetic (every case navigates to inline HTML via `useCaseHtml`), and every
capture/assertion is JSON-structural so all six language runners must agree
byte-for-byte (see tools/compare_binding_results.py). Replace with the
canonical curated suite when it lands; keep the schema + runner CLI unchanged.

Regenerate:  python3 tools/gen_binding_cases.py
Validate:    python3 -c "import json,jsonschema; jsonschema.validate(json.load(open('bindings/cases/full.json')), json.load(open('bindings/cases/manifest.schema.json')))"
"""
from __future__ import annotations
import html as _html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "bindings" / "cases" / "full.json"


def doc(title: str, body: str, style: str = "") -> str:
    style_tag = f"<style>{style}</style>" if style else ""
    return (
        f"<!doctype html><html><head><title>{_html.escape(title)}</title>"
        f"{style_tag}</head><body>{body}</body></html>"
    )


def case(cid, description, html, steps):
    return {"id": cid, "description": description, "html": html, "steps": steps}


cases = []

# --- 30 title cases: goto -> title capture -> exact + contains assertions ---
for i in range(30):
    title = f"Rustwright Case {i:02d}"
    cases.append(case(
        f"title-{i:02d}",
        "Capture the document title and assert exact + substring equality.",
        doc(title, f"<h1>heading {i}</h1>"),
        [
            {"op": "goto", "useCaseHtml": True},
            {"op": "title", "capture": "title"},
            {"op": "assertTitle", "equals": title},
            {"op": "assertTitle", "contains": f"Case {i:02d}"},
        ],
    ))

# --- 25 form cases: textContent + fill + click DOM mutation + assertText ---
for i in range(25):
    value = f"value-{i:03d}"
    body = (
        '<p id="message">ready</p>'
        '<input id="name">'
        '<button id="go" onclick="document.querySelector(\'#message\')'
        ".textContent=document.querySelector('#name').value\">Go</button>"
    )
    cases.append(case(
        f"form-{i:02d}",
        "Read initial text, fill an input, click, and assert the mutated text.",
        doc(f"Form {i:02d}", body),
        [
            {"op": "goto", "useCaseHtml": True, "waitUntil": "load"},
            {"op": "textContent", "selector": "#message", "capture": "before"},
            {"op": "assertText", "selector": "#message", "equals": "ready"},
            {"op": "fill", "selector": "#name", "value": value},
            {"op": "click", "selector": "#go"},
            {"op": "textContent", "selector": "#message", "capture": "after"},
            {"op": "assertText", "selector": "#message", "equals": value},
        ],
    ))

# --- 20 evaluate cases: JSON arg in, structural JSON out, primitive assertEval ---
for i in range(20):
    cases.append(case(
        f"eval-{i:02d}",
        "Pass a JSON argument, capture a decoded object, assert a primitive result.",
        doc(f"Evaluate {i:02d}", ""),
        [
            {"op": "goto", "useCaseHtml": True},
            {
                "op": "evaluate",
                "expression": "v => ({ n: v.n, doubled: v.n * 2, tag: v.tag, items: [v.n, v.n + 1] })",
                "arg": {"n": i, "tag": f"t-{i:02d}"},
                "capture": "computed",
            },
            {"op": "assertEval", "expression": f"{i} + {i}", "equals": i + i},
        ],
    ))

# --- 15 text cases: contains + exact textContent assertions ---
for i in range(15):
    text = f"lorem {i:02d} ipsum dolor"
    cases.append(case(
        f"text-{i:02d}",
        "Capture element text and assert contains + exact semantics.",
        doc(f"Text {i:02d}", f'<article id="content">{text}</article>'),
        [
            {"op": "goto", "useCaseHtml": True},
            {"op": "textContent", "selector": "#content", "capture": "content"},
            {"op": "assertText", "selector": "#content", "contains": f"{i:02d}"},
            {"op": "assertText", "selector": "#content", "equals": text},
        ],
    ))

# --- 10 screenshot cases: deterministic render -> byte length must match across langs ---
palette = ["#2457d6", "#0b7d3e", "#8a1f5c", "#b45309", "#334155",
           "#7c3aed", "#0e7490", "#be123c", "#15803d", "#a16207"]
for i in range(10):
    style = f"body{{margin:0;background:{palette[i]};color:#fff;font:32px sans-serif}}main{{padding:48px}}"
    cases.append(case(
        f"shot-{i:02d}",
        "Record the default PNG screenshot byte length for a deterministic render.",
        doc(f"Shot {i:02d}", f"<main>Rustwright shot {i:02d}</main>", style),
        [
            {"op": "goto", "useCaseHtml": True},
            {"op": "screenshot", "capture": "pngBytes"},
            {"op": "evaluate", "expression": "document.body.textContent.trim()", "capture": "bodyText"},
            {"op": "assertTitle", "equals": f"Shot {i:02d}"},
        ],
    ))

manifest = {"version": 1, "cases": cases}
assert len(cases) == 100, len(cases)
ids = [c["id"] for c in cases]
assert len(ids) == len(set(ids)), "duplicate ids"
OUT.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"wrote {OUT} with {len(cases)} cases")
