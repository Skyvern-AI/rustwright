from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


import_mind2web = load_tool("import_mind2web", "tools/import_mind2web.py")
run_mind2web_benchmark = load_tool("run_mind2web_benchmark", "tools/run_mind2web_benchmark.py")


def test_manifest_task_can_include_executable_action_fixture(tmp_path):
    source = tmp_path / "mind2web.json"
    source.write_text("[]", encoding="utf-8")
    record = {
        "annotation_id": "task-1",
        "confirmed_task": "Click submit",
        "domain": "example.com",
        "actions": [
            {
                "operation": {"op": "CLICK"},
                "cleaned_html": "<button id='submit'>Submit</button>",
                "pos_candidates": [
                    {
                        "tag": "button",
                        "attributes": {"id": "submit", "aria-label": "Submit"},
                        "text": "Submit",
                    }
                ],
            }
        ],
    }

    task = import_mind2web.manifest_task(tmp_path, source, 0, record, include_action_fixtures=True)

    assert task["task_id"] == "task-1"
    assert task["executable_action_count"] == 1
    fixture = task["action_fixtures"][0]
    assert fixture["operation"] == "CLICK"
    assert fixture["html"] == "<button id='submit'>Submit</button>"
    assert fixture["candidates"][0]["id"] == "submit"


def test_percentage_sampling_is_deterministic_and_stratified():
    manifest = {
        "tasks": [
            {"task_id": "a1", "domain": "a.test"},
            {"task_id": "a2", "domain": "a.test"},
            {"task_id": "b1", "domain": "b.test"},
            {"task_id": "b2", "domain": "b.test"},
            {"task_id": "c1", "domain": "c.test"},
            {"task_id": "c2", "domain": "c.test"},
        ]
    }

    first = run_mind2web_benchmark.selected_tasks(manifest, percentage=50, seed=7, max_tasks=None)
    second = run_mind2web_benchmark.selected_tasks(manifest, percentage=50, seed=7, max_tasks=None)

    assert [task["task_id"] for task in first] == [task["task_id"] for task in second]
    assert len(first) == 3
    assert len({task["domain"] for task in first}) == 3


def test_importer_writes_fixture_manifest_when_requested(tmp_path):
    source = tmp_path / "data.json"
    output = tmp_path / "manifest.json"
    source.write_text(
        json.dumps(
            [
                {
                    "annotation_id": "task-1",
                    "confirmed_task": "Type a search",
                    "domain": "example.com",
                    "actions": [
                        {
                            "operation": {"op": "TYPE", "value": "weather"},
                            "cleaned_html": "<input id='q'>",
                            "pos_candidates": [{"attributes": {"id": "q"}, "tag": "input"}],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    tasks = [
        import_mind2web.manifest_task(
            tmp_path,
            source,
            index,
            record,
            include_action_fixtures=True,
        )
        for index, (_, record) in enumerate(import_mind2web.iter_records(source))
    ]
    manifest = {
        "schema_version": 1,
        "source": "mind2web",
        "action_fixtures_included": True,
        "summary": import_mind2web.build_summary(tasks),
        "tasks": tasks,
    }
    output.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["action_fixtures_included"]
    assert loaded["summary"]["task_count"] == 1
    assert loaded["tasks"][0]["action_fixtures"][0]["value"] == "weather"
