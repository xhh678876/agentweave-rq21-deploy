"""Unit tests for M0..M4 method implementations.

These tests are pure (no network, no harness): they feed canned trajectories
into each method and assert on library state + injection shape. The goal is to
nail down the M0/M1/M2/M3/M4 *behavioral* contract so a future refactor cannot
silently break the experimental design.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentweave_runner.library import Library
from agentweave_runner.methods import (
    METHOD_REGISTRY,
    M0NoSharing,
    M1AWM,
    M2ReasoningBank,
    M3AgentWeaveNoL5,
    M4AgentWeaveFull,
    get_method,
)


# --- Fixtures --------------------------------------------------------------


@pytest.fixture()
def task_archive() -> dict:
    return {
        "id": "task_01",
        "goal": "Archive /inbox/report.txt and verify.",
        "tools": [
            {"type": "function", "function": {"name": "list_directory"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "delete_file"}},
            {"type": "function", "function": {"name": "copy_file"}},
        ],
    }


@pytest.fixture()
def task_http() -> dict:
    return {
        "id": "task_13",
        "goal": "GET /orders/100 then POST /audit/order_item.",
        "tools": [
            {"type": "function", "function": {"name": "http_get"}},
            {"type": "function", "function": {"name": "http_post"}},
            {"type": "function", "function": {"name": "parse_json"}},
            {"type": "function", "function": {"name": "extract_field"}},
        ],
    }


@pytest.fixture()
def trajectory_success(task_archive: dict) -> dict:
    return {
        "result": {
            "task_id": task_archive["id"],
            "success": True,
            "steps": 3,
            "finish_reason": "assistant_final",
        },
        "verdict": {"success": True, "details": {}},
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": task_archive["goal"]},
            {"role": "assistant", "content": "TASK_COMPLETE"},
        ],
        "trajectory": [
            {
                "step": 0,
                "role": "tool",
                "tool": "copy_file",
                "arguments": {"source": "/inbox/report.txt", "destination": "/archive/report.txt"},
                "result": {"ok": True},
            },
            {
                "step": 1,
                "role": "tool",
                "tool": "read_file",
                "arguments": {"path": "/archive/report.txt"},
                "result": {"ok": True, "content": "Q2 revenue draft"},
            },
            {
                "step": 2,
                "role": "tool",
                "tool": "delete_file",
                "arguments": {"path": "/inbox/report.txt"},
                "result": {"ok": True},
            },
        ],
    }


@pytest.fixture()
def trajectory_failure(task_archive: dict) -> dict:
    return {
        "result": {
            "task_id": task_archive["id"],
            "success": False,
            "steps": 2,
            "finish_reason": "assistant_stalled",
        },
        "verdict": {"success": False, "details": {"failures": ["missing file"]}},
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": task_archive["goal"]},
        ],
        "trajectory": [
            {
                "step": 0,
                "role": "tool",
                "tool": "read_file",
                "arguments": {"path": "/inbox/missing.txt"},
                "result": {"ok": False, "error": "file not found"},
            },
            {
                "step": 1,
                "role": "tool",
                "tool": "copy_file",
                "arguments": {"source": "/inbox/missing.txt", "destination": "/archive/x.txt"},
                "result": {"ok": False, "error": "source missing"},
            },
        ],
    }


# --- Registry --------------------------------------------------------------


@pytest.mark.unit
def test_registry_completeness():
    assert set(METHOD_REGISTRY) == {"M0", "M1", "M2", "M3", "M4"}


@pytest.mark.unit
def test_get_method_case_insensitive():
    assert get_method("m4").name == "M4"
    assert get_method("M0").name == "M0"


@pytest.mark.unit
def test_get_method_unknown():
    with pytest.raises(ValueError):
        get_method("M99")


# --- M0 --------------------------------------------------------------------


@pytest.mark.unit
def test_m0_never_injects(task_archive, trajectory_success):
    m = M0NoSharing()
    lib = m.make_library()
    assert m.inject_library(lib, task_archive) is None
    lib2 = m.update_library(lib, trajectory_success, task_archive)
    # Library stays empty after any number of updates
    assert lib2.size == 0
    assert m.inject_library(lib2, task_archive) is None


# --- M1 (AWM) --------------------------------------------------------------


@pytest.mark.unit
def test_m1_only_keeps_success_workflows(task_archive, trajectory_success, trajectory_failure):
    m = M1AWM()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_failure, task_archive)
    assert lib.workflows == []  # failure should not be stored
    lib = m.update_library(lib, trajectory_success, task_archive)
    assert len(lib.workflows) == 1
    assert lib.workflows[0]["steps"] == ["copy_file", "read_file", "delete_file"]


@pytest.mark.unit
def test_m1_inject_uses_do_axis_only(task_archive, trajectory_success):
    m = M1AWM()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    out = m.inject_library(lib, task_archive)
    assert out is not None
    assert out["do_not_entries"] == []
    assert len(out["do_entries"]) == 1
    # Workflow line should mention the tool chain
    assert "copy_file" in out["do_entries"][0]


@pytest.mark.unit
def test_m1_dedup(task_archive, trajectory_success):
    m = M1AWM()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    lib = m.update_library(lib, trajectory_success, task_archive)
    assert len(lib.workflows) == 1  # same signature, not duplicated


# --- M2 (ReasoningBank) ----------------------------------------------------


@pytest.mark.unit
def test_m2_stores_success_and_failure(task_archive, trajectory_success, trajectory_failure):
    m = M2ReasoningBank()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    lib = m.update_library(lib, trajectory_failure, task_archive)
    assert len(lib.reasoning_chunks) == 2
    successes = [c for c in lib.reasoning_chunks if c["success"]]
    failures = [c for c in lib.reasoning_chunks if not c["success"]]
    assert len(successes) == 1
    assert len(failures) == 1


@pytest.mark.unit
def test_m2_inject_uses_do_axis_only(task_archive, trajectory_success):
    m = M2ReasoningBank()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    out = m.inject_library(lib, task_archive)
    assert out is not None
    assert out["do_not_entries"] == []
    assert len(out["do_entries"]) >= 1


# --- M3 (w/o L5) -----------------------------------------------------------


@pytest.mark.unit
def test_m3_emits_both_axes_in_library(task_archive, trajectory_success, trajectory_failure):
    m = M3AgentWeaveNoL5()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    lib = m.update_library(lib, trajectory_failure, task_archive)
    assert len(lib.do_entries) >= 1
    assert len(lib.do_not_entries) >= 1


@pytest.mark.unit
def test_m3_inject_collapses_to_do_axis_only(task_archive, trajectory_success, trajectory_failure):
    """The L5 ablation: DO NOT entries are present in the library but NOT
    foregrounded in the prompt. We surface them under DO with a 'Note:' prefix
    so the harness does not render them as hard constraints."""
    m = M3AgentWeaveNoL5()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    lib = m.update_library(lib, trajectory_failure, task_archive)
    out = m.inject_library(lib, task_archive)
    assert out is not None
    assert out["do_not_entries"] == []
    assert any("Note:" in entry for entry in out["do_entries"])


# --- M4 (Full) -------------------------------------------------------------


@pytest.mark.unit
def test_m4_preserves_do_not_axis(task_archive, trajectory_success, trajectory_failure):
    m = M4AgentWeaveFull()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    lib = m.update_library(lib, trajectory_failure, task_archive)
    out = m.inject_library(lib, task_archive)
    assert out is not None
    assert len(out["do_not_entries"]) >= 1
    assert len(out["do_entries"]) >= 1


@pytest.mark.unit
def test_m4_retrieval_filters_irrelevant_tools(task_archive, task_http, trajectory_success):
    """L5 retrieval must drop entries whose tool isn't even available in the
    current task. This is the load-bearing assertion for M4 vs M3."""
    m = M4AgentWeaveFull()
    lib = m.make_library()
    # task_archive's trajectory uses copy_file/read_file/delete_file
    lib = m.update_library(lib, trajectory_success, task_archive)
    # Now inject against task_http which has none of those tools
    out = m.inject_library(lib, task_http)
    assert out is not None
    assert out["do_entries"] == []  # all filtered out by L5 retrieval


@pytest.mark.unit
def test_m4_vs_m3_diverge_on_retrieval(task_archive, task_http, trajectory_success):
    """The defining difference: given the same library state, M3 still injects
    something (no pre-filtering), M4 drops irrelevant entries."""
    m3 = M3AgentWeaveNoL5()
    m4 = M4AgentWeaveFull()
    lib3 = m3.make_library()
    lib4 = m4.make_library()
    lib3 = m3.update_library(lib3, trajectory_success, task_archive)
    lib4 = m4.update_library(lib4, trajectory_success, task_archive)
    out3 = m3.inject_library(lib3, task_http)
    out4 = m4.inject_library(lib4, task_http)
    # M3 keeps entries; M4 drops them by L5 retrieval
    assert out3 is not None and out3["do_entries"]
    assert out4 is None or out4["do_entries"] == []


# --- Library save/load roundtrip ------------------------------------------


@pytest.mark.unit
def test_library_save_load_roundtrip(tmp_path, task_archive, trajectory_success, trajectory_failure):
    m = M4AgentWeaveFull()
    lib = m.make_library()
    lib = m.update_library(lib, trajectory_success, task_archive)
    lib = m.update_library(lib, trajectory_failure, task_archive)
    p = tmp_path / "lib.json"
    lib.save(p)
    loaded = Library.load(p)
    assert loaded.do_entries == lib.do_entries
    assert loaded.do_not_entries == lib.do_not_entries
    assert loaded.workflows == lib.workflows
    assert loaded.reasoning_chunks == lib.reasoning_chunks
