"""Method implementations: M0 (No-Sharing), M1 (AWM), M2 (ReasoningBank),
M3 (AgentWeave w/o L5), M4 (AgentWeave Full).

Each method conforms to this protocol::

    class Method(Protocol):
        name: str
        def make_library(self) -> Library: ...
        def update_library(self, lib: Library, trajectory: dict, task: dict) -> Library: ...
        def inject_library(self, lib: Library, task: dict) -> dict | None: ...

``inject_library`` returns the dict that gets passed to ``Harness.run_task``.
It MUST honor the langgraph_harness contract — ``{"do_entries": [...], "do_not_entries": [...]}``
— so that the harness ``library.py:build_system_prompt`` accepts it without
modification. Methods that have richer state (M1 workflows, M2 reasoning
chunks) flatten that state into ``do_entries`` lines so the harness still sees
something injectable.

Returning ``None`` means: do not inject anything (M0 always, M1/M2/M3/M4 when
their library is empty before the first task).

Differences at a glance (the part the paper has to defend)::

    M0 : no-op make/update/inject.
    M1 : extract tool-call sequence -> {"name": .., "steps": [..]} into workflows.
         inject top-k workflows as DO lines (no DO NOT axis).
    M2 : extract a reasoning chunk per failed/successful task into reasoning_chunks.
         inject top-k reasoning chunks as DO lines (no constraint-first ordering).
    M3 : full L2 -> L4 -> emit DO and DO NOT entries; inject DO first / DO NOT
         second (NO constraint-first re-ordering, NO retrieval pre-filtering).
    M4 : same extraction as M3, but inject DO NOT first + apply retrieval
         pre-filtering by tool overlap (constraint-first L5 retrieval).

All updates use immutable patterns: ``update_library`` returns a NEW Library;
``Library.do_entries`` lists are replaced with new lists rather than appended in
place. This matches the project-level coding-style rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Protocol

from agentweave_runner.library import Library


# ---------------------------------------------------------------------------
# Shared trajectory utilities
# ---------------------------------------------------------------------------

# Cap on library entries per axis to keep system prompts reasonable.
# Tuned to fit a ~2-3k token guidance budget at average DO/DO NOT line length.
MAX_LIBRARY_PER_AXIS = 24

# Cap on entries injected into a single system prompt (after retrieval).
INJECT_TOP_K = 6


def _trajectory_tool_steps(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    """Return tool-result steps from a harness trajectory payload."""
    steps = trajectory.get("trajectory") or trajectory.get("trajectory_steps") or []
    out: list[dict[str, Any]] = []
    for entry in steps:
        if entry.get("role") == "tool":
            out.append(entry)
    return out


def _trajectory_assistant_text(trajectory: dict[str, Any]) -> str:
    """Concatenate assistant content (non-tool-call) text from a trajectory."""
    chunks: list[str] = []
    for msg in trajectory.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or ""
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
    return "\n".join(chunks)


def _tool_sequence(trajectory: dict[str, Any]) -> list[str]:
    """Return the ordered list of tool names used in this trajectory."""
    return [step.get("tool", "unknown") for step in _trajectory_tool_steps(trajectory)]


def _task_tool_names(task: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in task.get("tools") or []:
        fn = (entry or {}).get("function") or {}
        name = fn.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _task_id(trajectory: dict[str, Any], task: dict[str, Any]) -> str:
    return (
        task.get("id")
        or (trajectory.get("result") or {}).get("task_id")
        or "unknown_task"
    )


def _success(trajectory: dict[str, Any]) -> bool:
    return bool((trajectory.get("result") or {}).get("success"))


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Method(Protocol):
    name: str

    def make_library(self) -> Library: ...

    def update_library(
        self, lib: Library, trajectory: dict[str, Any], task: dict[str, Any]
    ) -> Library: ...

    def inject_library(
        self, lib: Library, task: dict[str, Any]
    ) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# M0 — No-Sharing baseline
# ---------------------------------------------------------------------------


class M0NoSharing:
    """M0: every task starts with an empty context. No extraction, no injection."""

    name = "M0"

    def make_library(self) -> Library:
        return Library(meta={"method": self.name})

    def update_library(
        self, lib: Library, trajectory: dict[str, Any], task: dict[str, Any]
    ) -> Library:
        return lib  # no-op

    def inject_library(
        self, lib: Library, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        return None


# ---------------------------------------------------------------------------
# M1 — AWM (Agent Workflow Memory)
# ---------------------------------------------------------------------------


class M1AWM:
    """M1: Agent Workflow Memory.

    Reference: AWM extracts reusable workflow step sequences from successful
    trajectories and replays the most relevant one in the system prompt.

    Here a workflow is the canonical tool-call sequence of a successful
    trajectory, named by task id + abbreviated tool chain. At injection time we
    retrieve the top-K workflows whose tool set overlaps the current task's
    available tools.
    """

    name = "M1"

    def make_library(self) -> Library:
        return Library(meta={"method": self.name})

    def update_library(
        self, lib: Library, trajectory: dict[str, Any], task: dict[str, Any]
    ) -> Library:
        # AWM only memorizes successful workflows.
        if not _success(trajectory):
            return lib
        seq = _tool_sequence(trajectory)
        if not seq:
            return lib
        task_id = _task_id(trajectory, task)
        wf = {
            "name": f"wf_{task_id}",
            "task_id": task_id,
            "goal": task.get("goal", ""),
            "steps": seq,
        }
        new_workflows = list(lib.workflows)
        # Deduplicate by tool-sequence signature.
        signature = tuple(seq)
        existing_signatures = {tuple(w.get("steps") or []) for w in new_workflows}
        if signature not in existing_signatures:
            new_workflows.append(wf)
        # Cap library size; keep most recent.
        new_workflows = new_workflows[-MAX_LIBRARY_PER_AXIS:]
        return replace(lib, workflows=new_workflows)

    def inject_library(
        self, lib: Library, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not lib.workflows:
            return None
        available = _task_tool_names(task)
        ranked = sorted(
            lib.workflows,
            key=lambda w: _overlap(set(w.get("steps") or []), available),
            reverse=True,
        )
        top = ranked[:INJECT_TOP_K]
        do_entries = [_format_workflow_line(w) for w in top]
        # AWM has no DO-NOT axis.
        return {"do_entries": do_entries, "do_not_entries": []}


def _format_workflow_line(wf: dict[str, Any]) -> str:
    steps = wf.get("steps") or []
    chain = " -> ".join(steps[:8])
    if len(steps) > 8:
        chain += f" -> ... ({len(steps)} steps total)"
    goal = (wf.get("goal") or "").strip().splitlines()[0] if wf.get("goal") else ""
    if goal:
        return f"For goals like '{_truncate(goal, 80)}', a successful tool chain is: {chain}."
    return f"Successful tool chain pattern: {chain}."


def _overlap(a: set[str], b: set[str]) -> int:
    return len(a & b)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# M2 — ReasoningBank
# ---------------------------------------------------------------------------


class M2ReasoningBank:
    """M2: ReasoningBank.

    Reference: ReasoningBank stores reasoning traces (short natural-language
    explanations / heuristics) extracted from past trajectories and retrieves
    the top-k to seed the model's reasoning on a new task.

    We approximate the "reasoning trace" with a deterministic textual summary
    derived from the trajectory: the goal, the tool sequence, and a one-line
    success/failure verdict. Real ReasoningBank uses LLM-generated reasoning;
    using a deterministic stand-in keeps M2 falsifiable in this experiment.
    The retrieval signal is tool-name overlap (same as M1), which is the
    apples-to-apples comparator for tool-using tasks.
    """

    name = "M2"

    def make_library(self) -> Library:
        return Library(meta={"method": self.name})

    def update_library(
        self, lib: Library, trajectory: dict[str, Any], task: dict[str, Any]
    ) -> Library:
        # ReasoningBank stores both successes and failures — the failure
        # reasoning is exactly the "what went wrong" signal it learns from.
        seq = _tool_sequence(trajectory)
        task_id = _task_id(trajectory, task)
        success = _success(trajectory)
        goal = (task.get("goal") or "").strip()
        verdict = "succeeded" if success else "failed"
        finish = (trajectory.get("result") or {}).get("finish_reason", "")
        text = (
            f"For task '{_truncate(goal, 100)}' the agent {verdict} using tools "
            f"[{', '.join(seq[:8])}]"
            + (" + ..." if len(seq) > 8 else "")
            + (f" (finish_reason={finish})." if finish else ".")
        )
        chunk = {
            "task_id": task_id,
            "success": success,
            "tool_set": sorted(set(seq)),
            "text": text,
        }
        new_chunks = list(lib.reasoning_chunks)
        # Deduplicate by (task_id, success) pair.
        key = (task_id, success)
        if not any((c.get("task_id"), c.get("success")) == key for c in new_chunks):
            new_chunks.append(chunk)
        new_chunks = new_chunks[-MAX_LIBRARY_PER_AXIS:]
        return replace(lib, reasoning_chunks=new_chunks)

    def inject_library(
        self, lib: Library, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not lib.reasoning_chunks:
            return None
        available = _task_tool_names(task)
        ranked = sorted(
            lib.reasoning_chunks,
            key=lambda c: _overlap(set(c.get("tool_set") or []), available),
            reverse=True,
        )
        top = ranked[:INJECT_TOP_K]
        do_entries = [str(c.get("text") or "") for c in top if c.get("text")]
        return {"do_entries": do_entries, "do_not_entries": []}


# ---------------------------------------------------------------------------
# Shared L2 -> L4 extraction for M3 and M4
# ---------------------------------------------------------------------------


# Patterns that strongly indicate a tool call failed for a learnable reason
_TRANSIENT_RE = re.compile(r"MALFORMED_TRANSIENT_OUTPUT|transient", re.IGNORECASE)


def _step_status(step: dict[str, Any]) -> str:
    result = step.get("result")
    if isinstance(result, dict):
        if result.get("ok") is True:
            return "success"
        if result.get("ok") is False:
            return "failure"
    if isinstance(result, str) and _TRANSIENT_RE.search(result):
        return "failure"
    return "unknown"


def _format_args(args: Any) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        s = str(args)
    return _truncate(s, 100)


def _format_result(result: Any) -> str:
    try:
        s = json.dumps(result, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        s = str(result)
    return _truncate(s, 120)


def _extract_do_do_not(
    trajectory: dict[str, Any], task: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk a trajectory and emit one DO entry per successful tool call and
    one DO NOT entry per failed tool call. Both M3 and M4 share this extraction.
    """
    task_id = _task_id(trajectory, task)
    do_list: list[dict[str, Any]] = []
    do_not_list: list[dict[str, Any]] = []
    final_success = _success(trajectory)
    for step in _trajectory_tool_steps(trajectory):
        status = _step_status(step)
        tool = step.get("tool", "unknown")
        args = step.get("arguments") or {}
        result = step.get("result")
        if status == "success":
            do_list.append(
                {
                    "text": f"When working on tasks involving `{tool}`, calling `{tool}({_format_args(args)})` is a known good pattern (from {task_id}).",
                    "tool": tool,
                    "task_id": task_id,
                    "final_success": final_success,
                }
            )
        elif status == "failure":
            do_not_list.append(
                {
                    "text": f"Avoid calling `{tool}({_format_args(args)})`: it previously returned {_format_result(result)} (from {task_id}).",
                    "tool": tool,
                    "task_id": task_id,
                    "final_success": final_success,
                }
            )
    return do_list, do_not_list


def _merge_entries(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
    cap: int,
) -> list[dict[str, Any]]:
    """Append new entries, deduplicating by 'text' field, capping at most `cap`."""
    seen = {e.get("text") for e in existing}
    merged = list(existing)
    for entry in new:
        text = entry.get("text")
        if not text or text in seen:
            continue
        merged.append(entry)
        seen.add(text)
    return merged[-cap:]


# ---------------------------------------------------------------------------
# M3 — AgentWeave w/o L5
# ---------------------------------------------------------------------------


class M3AgentWeaveNoL5:
    """M3: Full L1-L4 pipeline but NO L5 constraint-first retrieval.

    Difference from M4 (this is the load-bearing ablation for RQ1):
      - injection order is DO -> DO NOT (NOT constraint-first)
      - NO retrieval pre-filtering: we just inject the most recent K entries

    Update path is identical to M4 so the libraries are comparable.
    """

    name = "M3"

    def make_library(self) -> Library:
        return Library(meta={"method": self.name})

    def update_library(
        self, lib: Library, trajectory: dict[str, Any], task: dict[str, Any]
    ) -> Library:
        new_do, new_dont = _extract_do_do_not(trajectory, task)
        return replace(
            lib,
            do_entries=_merge_entries(lib.do_entries, new_do, MAX_LIBRARY_PER_AXIS),
            do_not_entries=_merge_entries(
                lib.do_not_entries, new_dont, MAX_LIBRARY_PER_AXIS
            ),
        )

    def inject_library(
        self, lib: Library, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not lib.do_entries and not lib.do_not_entries:
            return None
        # No retrieval prefiltering: take the last K of each axis as-is.
        # No constraint-first ordering: we put DO entries FIRST in the prompt
        # by collapsing both axes into a single DO list (and prefixing each
        # would-be DO NOT entry with "Note:" so the information is preserved
        # but not foregrounded as a hard constraint). This bypasses the
        # harness's hardcoded DO-NOT-first rendering, which is exactly the
        # L5 ablation we want.
        recent_do = [e.get("text", "") for e in lib.do_entries[-INJECT_TOP_K:]]
        recent_dont = [
            f"Note: {e.get('text', '')}"
            for e in lib.do_not_entries[-INJECT_TOP_K:]
        ]
        merged = [t for t in recent_do + recent_dont if t.strip()]
        return {"do_entries": merged, "do_not_entries": []}


# ---------------------------------------------------------------------------
# M4 — AgentWeave Full (L1-L5)
# ---------------------------------------------------------------------------


class M4AgentWeaveFull:
    """M4: Full AgentWeave with L5 constraint-first retrieval.

    Differences from M3:
      - retrieval pre-filtering by available-tool overlap (constraint-first
        retrieval uses the tools the current task can actually invoke).
      - DO NOT entries injected before DO entries (delegated to the harness's
        build_system_prompt, which already does this when do_not_entries is
        non-empty).
    """

    name = "M4"

    def make_library(self) -> Library:
        return Library(meta={"method": self.name})

    def update_library(
        self, lib: Library, trajectory: dict[str, Any], task: dict[str, Any]
    ) -> Library:
        new_do, new_dont = _extract_do_do_not(trajectory, task)
        return replace(
            lib,
            do_entries=_merge_entries(lib.do_entries, new_do, MAX_LIBRARY_PER_AXIS),
            do_not_entries=_merge_entries(
                lib.do_not_entries, new_dont, MAX_LIBRARY_PER_AXIS
            ),
        )

    def inject_library(
        self, lib: Library, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not lib.do_entries and not lib.do_not_entries:
            return None
        available = _task_tool_names(task)
        do_ranked = _retrieve_by_tool_overlap(lib.do_entries, available, INJECT_TOP_K)
        dont_ranked = _retrieve_by_tool_overlap(
            lib.do_not_entries, available, INJECT_TOP_K
        )
        return {
            "do_entries": [e.get("text", "") for e in do_ranked],
            "do_not_entries": [e.get("text", "") for e in dont_ranked],
        }


def _retrieve_by_tool_overlap(
    entries: list[dict[str, Any]], available_tools: set[str], top_k: int
) -> list[dict[str, Any]]:
    """L5 constraint-first retrieval signal: filter by tool relevance.

    An entry whose tool is in the task's available tools gets a relevance
    boost. Entries irrelevant to *any* tool in the current task are dropped
    (this is the "pre-filter" part of L5).
    """
    if not entries:
        return []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, entry in enumerate(entries):
        tool = entry.get("tool")
        relevant = 1 if (isinstance(tool, str) and tool in available_tools) else 0
        if relevant == 0:
            # Pre-filter: drop entries whose tool is not even available now.
            continue
        # Tiebreaker: prefer recent.
        scored.append((relevant, idx, entry))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [e for _, _, e in scored[:top_k]]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


METHOD_REGISTRY: dict[str, type] = {
    "M0": M0NoSharing,
    "M1": M1AWM,
    "M2": M2ReasoningBank,
    "M3": M3AgentWeaveNoL5,
    "M4": M4AgentWeaveFull,
}


def get_method(name: str):
    """Return a freshly instantiated method by name (case-insensitive)."""
    key = name.upper()
    if key not in METHOD_REGISTRY:
        raise ValueError(
            f"Unknown method '{name}'. Available: {sorted(METHOD_REGISTRY)}"
        )
    return METHOD_REGISTRY[key]()
