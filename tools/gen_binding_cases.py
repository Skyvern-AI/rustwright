#!/usr/bin/env python3
"""Generate the cross-language binding benchmark manifest (bindings/cases/full.json).

This is a deterministic 150-case suite so the full cross-language benchmark is
runnable today and the harness is proven at scale. It is deterministic and
hermetic (every case navigates to inline HTML via `useCaseHtml`), and every
capture/assertion is JSON-structural so all six language runners must agree
structurally (see tools/compare_binding_results.py). Keep the schema + runner
CLI unchanged when adding or curating cases.

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

# --- 10 deep DOM cases: nested textContent targets + exact/contains assertions ---
for i in range(10):
    depth = 5 + (i % 5)
    expected = f"level-{i:02d} payload-{i:02d} end"
    nested = (
        f'<span id="leaf-{i:02d}"><strong>level-{i:02d}</strong>'
        f' payload-{i:02d} <em>end</em></span>'
    )
    for level in range(depth):
        nested = f'<div data-level="{level}">{nested}</div>'
    selector = f'#root-{i:02d} ' + " > ".join(["div"] * depth + [f"#leaf-{i:02d}"])
    cases.append(case(
        f"dom-{i:02d}",
        "Read a deeply nested DOM target and mix exact and contains assertions.",
        doc(f"Deep DOM {i:02d}", f'<main id="root-{i:02d}">{nested}</main>'),
        [
            {"op": "goto", "useCaseHtml": True},
            {"op": "textContent", "selector": selector, "capture": "deepText"},
            {"op": "assertText", "selector": selector, "equals": expected},
            {"op": "assertText", "selector": f"#root-{i:02d}", "contains": f"payload-{i:02d}"},
        ],
    ))

# --- 10 sequence cases: repeated clicks mutate and expose cumulative state ---
for i in range(10):
    delta = 2 + (i % 4)
    selectors = ["#one", "#delta", "#one", "#delta"]
    if i % 2:
        selectors.append("#one")
    total = 2 + (2 * delta) + (i % 2)
    clicks = len(selectors)
    body = (
        '<output id="state" data-value="0" data-clicks="0">value=0; clicks=0</output>'
        '<button id="one" onclick="bump(1)">One</button>'
        f'<button id="delta" onclick="bump({delta})">Delta</button>'
        '<script>function bump(n){const s=document.querySelector(\'#state\');'
        'const v=Number(s.dataset.value)+n;const c=Number(s.dataset.clicks)+1;'
        "s.dataset.value=String(v);s.dataset.clicks=String(c);"
        "s.textContent='value='+v+'; clicks='+c}</script>"
    )
    steps = [
        {"op": "goto", "useCaseHtml": True},
        {"op": "assertText", "selector": "#state", "equals": "value=0; clicks=0"},
        {"op": "click", "selector": selectors[0]},
        {"op": "textContent", "selector": "#state", "capture": "afterFirst"},
        {"op": "assertText", "selector": "#state", "contains": "clicks=1"},
    ]
    steps.extend({"op": "click", "selector": selector} for selector in selectors[1:])
    steps.extend([
        {"op": "textContent", "selector": "#state", "capture": "finalText"},
        {
            "op": "evaluate",
            "expression": "() => { const s = document.querySelector('#state'); return { value: Number(s.dataset.value), clicks: Number(s.dataset.clicks) }; }",
            "capture": "finalState",
        },
        {"op": "assertText", "selector": "#state", "equals": f"value={total}; clicks={clicks}"},
        {
            "op": "assertEval",
            "expression": "() => { const s = document.querySelector('#state'); return [Number(s.dataset.value), Number(s.dataset.clicks)]; }",
            "equals": [total, clicks],
        },
    ])
    cases.append(case(
        f"seq-{i:02d}",
        "Click controls repeatedly and verify cumulative DOM and structured state.",
        doc(f"Sequence {i:02d}", body),
        steps,
    ))

# --- 10 Unicode/long fill cases: exact UTF-8 input round trips ---
unicode_values = [
    "café déjà vu — crème brûlée",
    "東京から京都へ、こんにちは世界",
    "مرحبا بالعالم — اختبار رسترايت",
    "Здравствуй, мир — проверка браузера",
    "नमस्ते दुनिया — ब्राउज़र परीक्षण",
    "emoji 🚀✨🧪 and flags 🇨🇦🇯🇵",
    "combining e\u0301 and precomposed é stay distinct",
    "quotes: \"double\", 'single', ampersand &, angle <tag>",
    "line one\nline two\tTabbed\nline three",
    "Rustwright-long-" + ("0123456789abcdef" * 64) + "-終",
]
for i, value in enumerate(unicode_values):
    contains = value[0:24]
    body = (
        '<textarea id="name"></textarea><button id="copy" '
        'onclick="document.querySelector(\'#out\').textContent='
        'document.querySelector(\'#name\').value">Copy</button><pre id="out">empty</pre>'
    )
    cases.append(case(
        f"uni-{i:02d}",
        "Fill Unicode or long text and verify its exact DOM round trip.",
        doc(f"Unicode Fill {i:02d}", body),
        [
            {"op": "goto", "useCaseHtml": True},
            {"op": "fill", "selector": "#name", "value": value},
            {"op": "click", "selector": "#copy"},
            {"op": "textContent", "selector": "#out", "capture": "copied"},
            {"op": "evaluate", "expression": "document.querySelector('#name').value", "capture": "inputValue"},
            {"op": "assertText", "selector": "#out", "contains": contains},
            {"op": "assertText", "selector": "#out", "equals": value},
        ],
    ))

# --- 10 extended evaluate cases: nested JSON + safe numeric edges ---
numeric_edges = [
    0,
    0.000001,
    -0.000001,
    2147483647,
    -2147483648,
    4294967295,
    9007199254740991,
    -9007199254740991,
    123456789.125,
    -987654321.875,
]
for i, edge in enumerate(numeric_edges):
    payload = {
        "index": i,
        "enabled": i % 2 == 0,
        "nothing": None,
        "edge": edge,
        "nested": {
            "values": [i, i + 1, i + 2],
            "flags": {"yes": True, "no": False},
            "matrix": [[i, edge], [None, -i]],
        },
    }
    cases.append(case(
        f"evalx-{i:02d}",
        "Capture nested JSON values with booleans, null, arrays, objects, and safe numeric edges.",
        doc(f"Extended Evaluate {i:02d}", ""),
        [
            {"op": "goto", "useCaseHtml": True},
            {
                "op": "evaluate",
                "expression": "v => ({ input: v, summary: { count: v.nested.values.length, active: v.enabled, edge: v.edge }, mirror: [v.nothing, v.enabled, v.edge, [...v.nested.values]] })",
                "arg": payload,
                "capture": "complex",
            },
            {
                "op": "assertEval",
                "expression": f"[true, false, null, {{ edge: {json.dumps(edge)} }}, [[{i}, {i + 1}], []]]",
                "equals": [True, False, None, {"edge": edge}, [[i, i + 1], []]],
            },
        ],
    ))

# --- 10 extended screenshot cases: fixed palettes and geometric layouts ---
extended_palettes = [
    ("#0f172a", "#38bdf8", "#f8fafc"),
    ("#1f2937", "#f59e0b", "#fef3c7"),
    ("#312e81", "#a78bfa", "#ede9fe"),
    ("#3f0d1e", "#fb7185", "#fff1f2"),
    ("#052e16", "#4ade80", "#dcfce7"),
    ("#164e63", "#22d3ee", "#cffafe"),
    ("#422006", "#facc15", "#fef9c3"),
    ("#450a0a", "#f87171", "#fee2e2"),
    ("#2e1065", "#c084fc", "#f3e8ff"),
    ("#111827", "#94a3b8", "#f1f5f9"),
]
for i, (background, accent, light) in enumerate(extended_palettes):
    style = (
        f"html,body{{margin:0;width:100%;height:100%;background:{background}}}"
        "main{width:640px;height:360px;padding:40px;box-sizing:border-box;"
        "display:grid;grid-template-columns:repeat(4,1fr);gap:16px}"
        f".tile{{background:{accent};border:8px solid {light};box-sizing:border-box}}"
        f".tile:nth-child(3n){{background:{light};border-color:{accent};border-radius:50%}}"
    )
    body = '<main id="canvas">' + ('<div class="tile"></div>' * 12) + "</main>"
    cases.append(case(
        f"shotx-{i:02d}",
        "Record PNG bytes for a fixed palette and geometric CSS layout.",
        doc(f"Extended Shot {i:02d}", body, style),
        [
            {"op": "goto", "useCaseHtml": True},
            {"op": "screenshot", "capture": "pngBytes"},
            {"op": "evaluate", "expression": "document.querySelectorAll('.tile').length", "capture": "tileCount"},
            {"op": "assertEval", "expression": "document.querySelectorAll('.tile').length", "equals": 12},
            {"op": "assertTitle", "contains": f"Shot {i:02d}"},
        ],
    ))

manifest = {"version": 1, "cases": cases}
assert len(cases) == 150, len(cases)
ids = [c["id"] for c in cases]
assert len(ids) == len(set(ids)), "duplicate ids"
OUT.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"wrote {OUT} with {len(cases)} cases")
