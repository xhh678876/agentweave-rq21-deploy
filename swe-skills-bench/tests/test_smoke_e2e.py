"""End-to-end smoke for the SWE-Skills-Bench bridge.

Two layers:

1. ``test_task_index_has_49_entries`` (offline, no Docker, no LLM): asserts
   :mod:`swe_skills_bridge.task_adapter` produces a 49-task INDEX with valid
   tool schemas matching the LangGraph harness's TOOL_TABLE.

2. ``test_e2e_run_one_smoke`` (gated on ``RUN_E2E_SMOKE=1``): drives the real
   LangGraph adapter on one task and runs the Docker eval. Requires DeepSeek
   creds + a running Docker daemon — skipped by default in CI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


BRIDGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_ROOT / "src"))

from swe_skills_bridge.task_adapter import (  # noqa: E402
    build_all_tasks,
    build_task_json,
    load_upstream_skills,
    _FUNCTION_TOOLS,
)


_TOOL_NAMES = {entry["function"]["name"] for entry in _FUNCTION_TOOLS}


@pytest.mark.unit
def test_function_tools_subset_of_harness_table() -> None:
    """Adapter must only emit tools the LangGraph harness can dispatch."""
    expected_subset = {
        "list_directory",
        "read_file",
        "write_file",
        "delete_file",
        "copy_file",
    }
    assert _TOOL_NAMES == expected_subset, (
        f"tool schemas drifted: {_TOOL_NAMES} != {expected_subset}"
    )


@pytest.mark.unit
def test_task_index_has_49_entries(tmp_path: Path) -> None:
    """The bridge contract: exactly 49 task JSONs after build_all_tasks."""
    summary = build_all_tasks(root=BRIDGE_ROOT, output_dir=tmp_path)
    assert summary["total"] == 49, f"expected 49 tasks, got {summary['total']}"
    assert summary["skipped"] == [], f"unexpected skips: {summary['skipped']}"

    # Every emitted task JSON must parse and carry the bridge's contract fields.
    files = sorted(tmp_path.glob("task_*.json"))
    assert len(files) == 49
    for fp in files:
        payload = json.loads(fp.read_text(encoding="utf-8"))
        assert payload["category"] == "swe_skills_bench"
        assert payload["verification"]["type"] == "swe_skills_docker_test"
        assert payload["verification"]["docker_image"]
        # All tools must be in the harness-known set.
        for tool in payload["tools"]:
            assert tool["function"]["name"] in _TOOL_NAMES


@pytest.mark.unit
def test_single_task_json_shape() -> None:
    """Spot-check a single, easy-to-reason task (add-uint-support)."""
    skills = load_upstream_skills(BRIDGE_ROOT)
    target = next((s for s in skills if s.skill_id == "add-uint-support"), None)
    assert target is not None
    payload = build_task_json(target, sequence=1)

    assert payload["id"] == "swe_skills_add_uint_support"
    assert "/workspace/TASK.md" in payload["initial_state"]["files"]
    assert payload["verification"]["docker_image"].startswith("zhangyiiiiii/")
    assert payload["verification"]["evaluation"]["method"] in {"unit_test", "build_check"}


@pytest.mark.integration
def test_e2e_run_one_smoke(tmp_path: Path) -> None:
    """End-to-end: drive the LangGraph harness + Docker eval on one task.

    Gated on env so CI doesn't fan out on every commit. Run manually::

        RUN_E2E_SMOKE=1 pytest -k test_e2e_run_one_smoke -s
    """
    if os.environ.get("RUN_E2E_SMOKE") != "1":
        pytest.skip("RUN_E2E_SMOKE!=1; skipping live LangGraph + Docker smoke")

    sys.path.insert(0, str(Path.home() / "agentweave" / "harness-exp-langgraph" / "src"))
    from langgraph_harness.adapter import run_task  # type: ignore

    from swe_skills_bridge.eval_adapter import docker_evaluate_trajectory

    task_path = BRIDGE_ROOT / "tasks" / "task_001.json"
    assert task_path.exists(), "tasks not built; run build_all_tasks first"
    task = json.loads(task_path.read_text(encoding="utf-8"))

    payload = run_task(task, library=None, seed=0, max_steps=8)
    out_path = tmp_path / "trajectory.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    verdict = docker_evaluate_trajectory(out_path)
    print(json.dumps(verdict.to_dict(), indent=2))
    assert verdict.method in {"unit_test", "build_check", "skipped"}
