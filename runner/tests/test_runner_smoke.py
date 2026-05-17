"""Runner end-to-end test with a fake harness.

We use a deterministic in-process fake harness so the test is fast and does
not require network or LLM credentials. The smoke verifies:

  - the runner writes one trajectory per task
  - it produces _summary.json with success_rate / mean_steps / total_tokens
  - it produces at least one library snapshot
  - M0 trajectories carry NO library (library_applied=False)
  - M4 trajectories with non-empty library carry library entries
  - resume picks up where it left off and does not re-run completed tasks
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentweave_runner.harness import Harness
from agentweave_runner.methods import M0NoSharing, M4AgentWeaveFull
from agentweave_runner.runner import run_method


class FakeHarness:
    """Always-successful in-process harness producing schema-compatible payloads.

    A single successful tool call is emitted per task so M4 has something to
    extract into its DO library.
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        had_library = bool(library and (library.get("do_entries") or library.get("do_not_entries")))
        self.calls.append((task.get("id", "?"), had_library))
        return {
            "result": {
                "harness": "fake",
                "model": "fake-model",
                "task_id": task.get("id", ""),
                "category": task.get("category", ""),
                "difficulty": task.get("difficulty", ""),
                "seed": seed,
                "success": True,
                "steps": 1,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "duration_sec": 0.001,
                "finish_reason": "assistant_final",
                "library_applied": had_library,
            },
            "verdict": {"success": True, "details": {}},
            "errors": [],
            "recoveries": [],
            "messages": [
                {"role": "system", "content": f"library_applied={had_library}"},
                {"role": "user", "content": task.get("goal", "")},
                {"role": "assistant", "content": "TASK_COMPLETE"},
            ],
            "trajectory": [
                {
                    "step": 0,
                    "role": "tool",
                    "tool": "list_directory",
                    "arguments": {"path": "/"},
                    "result": {"ok": True, "entries": []},
                }
            ],
            "final_state": {},
        }


def _make_task(idx: int) -> dict:
    return {
        "id": f"task_{idx:02d}",
        "goal": f"Test task {idx}.",
        "tools": [
            {"type": "function", "function": {"name": "list_directory"}},
        ],
    }


@pytest.fixture()
def tasks() -> list[dict]:
    return [_make_task(i) for i in range(1, 6)]


@pytest.mark.unit
def test_runner_writes_trajectories_and_summary(tmp_path: Path, tasks):
    out = tmp_path / "run"
    harness = FakeHarness()
    summary = run_method(
        M4AgentWeaveFull(),
        seed=0,
        tasks=tasks,
        harness=harness,
        out_dir=out,
        concurrency=2,
        snapshot_every=2,
        resume=False,
    )
    # 1 trajectory per task
    written = sorted(p.name for p in out.glob("task_*.json"))
    assert written == [f"task_{i:02d}.json" for i in range(1, 6)]
    # Summary file exists and includes the required metrics
    summary_disk = json.loads((out / "_summary.json").read_text(encoding="utf-8"))
    assert summary_disk["method"] == "M4"
    assert summary_disk["metrics"]["n"] == 5
    assert summary_disk["metrics"]["success_rate"] == 1.0
    assert summary_disk["metrics"]["total_tokens"] == 5 * 150
    assert summary == summary_disk
    # At least one library snapshot
    snaps = sorted((out / "_library_snapshots").glob("*.json"))
    assert len(snaps) >= 1


@pytest.mark.unit
def test_m0_never_passes_library_to_harness(tmp_path: Path, tasks):
    out = tmp_path / "run_m0"
    harness = FakeHarness()
    run_method(
        M0NoSharing(),
        seed=0,
        tasks=tasks,
        harness=harness,
        out_dir=out,
        concurrency=2,
        snapshot_every=2,
        resume=False,
    )
    # Every call must have had_library=False
    assert harness.calls
    assert all(not had_lib for _, had_lib in harness.calls)


@pytest.mark.unit
def test_m4_eventually_passes_library_to_harness(tmp_path: Path, tasks):
    """M4 should accumulate DO entries from earlier successful tasks and pass
    them to later tasks. We use concurrency=1 so library state strictly
    streams forward."""
    out = tmp_path / "run_m4"
    harness = FakeHarness()
    run_method(
        M4AgentWeaveFull(),
        seed=0,
        tasks=tasks,
        harness=harness,
        out_dir=out,
        concurrency=1,
        snapshot_every=1,
        resume=False,
    )
    # First call has empty library; later calls should see something.
    assert harness.calls[0][1] is False  # task_01 starts empty
    assert any(had_lib for _, had_lib in harness.calls[1:])


@pytest.mark.unit
def test_resume_skips_completed(tmp_path: Path, tasks):
    out = tmp_path / "run_resume"
    # First pass: only run 3 tasks
    harness = FakeHarness()
    run_method(
        M4AgentWeaveFull(),
        seed=0,
        tasks=tasks[:3],
        harness=harness,
        out_dir=out,
        concurrency=1,
        snapshot_every=1,
        resume=False,
    )
    # Second pass: resume with full task list
    harness2 = FakeHarness()
    summary = run_method(
        M4AgentWeaveFull(),
        seed=0,
        tasks=tasks,
        harness=harness2,
        out_dir=out,
        concurrency=1,
        snapshot_every=1,
        resume=True,
    )
    # Only 2 new tasks should have hit the harness on the second pass
    assert len(harness2.calls) == 2
    assert summary["metrics"]["n"] == 5
