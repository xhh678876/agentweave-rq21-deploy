"""run_method: orchestrate a single (method, seed, harness, task-set) run.

Public API::

    result = run_method(
        method,            # one of methods.get_method(name)
        seed=0,
        tasks=[...],       # list of task dicts (already loaded)
        harness=adapter,
        out_dir=Path(...),
        concurrency=32,
        snapshot_every=10,
        resume=True,
    )

Concurrency model:
    - LLM/tool inference runs in a thread pool (IO-bound).
    - ``update_library`` is serialized under ``LibraryLock``.
    - Tasks are processed in input order so that earlier successes inform
      later injections — strict semantics. Parallel execution is allowed
      WITHIN a snapshot boundary: tasks 1..N all see the same pre-snapshot
      library, then we update the library serially with all N trajectories
      and snapshot. This is the standard "mini-batch streaming" pattern used
      in memory-augmented agent papers and keeps reproducibility tractable.

We snapshot the library every ``snapshot_every`` tasks (default 10) and at
the end of the run.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from agentweave_runner.checkpoint import (
    list_snapshots,
    load_checkpoint,
    snapshot_library_path,
    summary_path,
    utc_iso_now,
    write_checkpoint,
)
from agentweave_runner.concurrency import LibraryLock, run_in_parallel
from agentweave_runner.harness import Harness
from agentweave_runner.library import Library
from agentweave_runner.methods import Method

logger = logging.getLogger(__name__)


def _task_id_of(task: dict[str, Any]) -> str:
    tid = task.get("id")
    if isinstance(tid, str) and tid:
        return tid
    return "task_unknown"


def _trajectory_filename(task: dict[str, Any]) -> str:
    return f"{_task_id_of(task)}.json"


def _write_trajectory(out_dir: Path, task: dict[str, Any], trajectory: dict[str, Any]) -> Path:
    target = out_dir / _trajectory_filename(task)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(trajectory, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def _initial_library(
    method: Method,
    out_dir: Path,
    resume: bool,
) -> tuple[Library, set[str]]:
    """Return (starting library, set of already-completed task ids)."""
    if not resume:
        return method.make_library(), set()
    ckpt = load_checkpoint(out_dir)
    if not ckpt:
        return method.make_library(), set()
    completed = set(ckpt.get("completed_task_ids") or [])
    snap = ckpt.get("last_library_snapshot")
    if snap:
        snap_path = (out_dir / snap).resolve() if not Path(snap).is_absolute() else Path(snap)
        if snap_path.exists():
            try:
                lib = Library.load(snap_path)
                logger.info(
                    "Resumed from %s with %d completed tasks", snap_path, len(completed)
                )
                return lib, completed
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load snapshot %s: %s", snap_path, exc)
    # Fall back to the most recent snapshot on disk.
    snaps = list_snapshots(out_dir)
    if snaps:
        try:
            lib = Library.load(snaps[-1])
            logger.info("Resumed from latest snapshot %s", snaps[-1])
            return lib, completed
        except Exception:  # noqa: BLE001
            pass
    return method.make_library(), completed


def _summarize(trajectories: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trajectories)
    if n == 0:
        return {
            "n": 0,
            "success_rate": 0.0,
            "mean_steps": 0.0,
            "total_tokens": 0,
            "mean_duration_sec": 0.0,
        }
    successes = 0
    steps_sum = 0
    duration_sum = 0.0
    in_sum = 0
    out_sum = 0
    tot_sum = 0
    for traj in trajectories:
        r = traj.get("result") or {}
        if r.get("success"):
            successes += 1
        steps_sum += int(r.get("steps") or 0)
        duration_sum += float(r.get("duration_sec") or 0.0)
        in_sum += int(r.get("input_tokens") or 0)
        out_sum += int(r.get("output_tokens") or 0)
        tot_sum += int(r.get("total_tokens") or 0)
    return {
        "n": n,
        "success_rate": successes / n,
        "successes": successes,
        "mean_steps": steps_sum / n,
        "mean_duration_sec": duration_sum / n,
        "total_input_tokens": in_sum,
        "total_output_tokens": out_sum,
        "total_tokens": tot_sum,
    }


def run_method(
    method: Method,
    *,
    seed: int,
    tasks: list[dict[str, Any]],
    harness: Harness,
    out_dir: Path,
    concurrency: int = 32,
    snapshot_every: int = 10,
    resume: bool = True,
) -> dict[str, Any]:
    """Run ``method`` over ``tasks`` (single seed) through ``harness``.

    Returns a summary dict written as ``_summary.json`` in ``out_dir``.

    Mini-batch semantics:
        Tasks are grouped into batches of size ``concurrency``. Within a batch,
        every task sees the SAME library snapshot. After the batch finishes we
        update the library with all batch trajectories (in input order) under
        the library lock, snapshot it, and proceed to the next batch.

        Why: this trades a small amount of "experience freshness" (a task in
        batch i never sees lessons from another task in batch i) for full
        determinism + linear speedup. With ``concurrency=1`` the semantics
        collapse to strict sequential streaming.
    """
    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    library, completed = _initial_library(method, out_dir, resume)
    library_lock = LibraryLock()

    pending: list[dict[str, Any]] = [
        task for task in tasks if _task_id_of(task) not in completed
    ]
    total = len(tasks)
    already_done = total - len(pending)
    logger.info(
        "method=%s seed=%d harness=%s tasks=%d (resumed: %d)",
        method.name,
        seed,
        getattr(harness, "name", "?"),
        total,
        already_done,
    )

    # If resume, recover trajectories on disk so the summary is correct.
    accumulated: list[dict[str, Any]] = []
    if resume:
        for task in tasks:
            tid = _task_id_of(task)
            if tid in completed:
                fp = out_dir / _trajectory_filename(task)
                if fp.exists():
                    try:
                        accumulated.append(json.loads(fp.read_text(encoding="utf-8")))
                    except Exception:  # noqa: BLE001
                        pass

    started_at = utc_iso_now()
    t_start = time.time()

    # If we are starting fresh, snapshot the empty library at index 0.
    if not resume or already_done == 0:
        snap_path = snapshot_library_path(out_dir, already_done)
        library.save(snap_path)

    batch_size = max(1, concurrency)
    batch_index = 0
    while pending:
        batch = pending[:batch_size]
        pending = pending[batch_size:]
        batch_index += 1

        # Snapshot library state visible to this batch (already done above on first batch).
        # Each worker takes the current library, computes injection, and runs the task.
        injected_for_batch: dict[str, dict[str, Any] | None] = {}
        with library_lock.read():
            for task in batch:
                injected_for_batch[_task_id_of(task)] = method.inject_library(
                    library, task
                )

        run_id_for = lambda task: (  # noqa: E731
            f"{method.name}-seed{seed}-batch{batch_index}-{_task_id_of(task)}"
        )

        def _worker(task: dict[str, Any]) -> dict[str, Any]:
            injected = injected_for_batch[_task_id_of(task)]
            return harness.run_task(
                task, library=injected, seed=seed, run_id=run_id_for(task)
            )

        results = run_in_parallel(batch, _worker, concurrency=concurrency)

        # Apply trajectories to disk + update library in input order
        for task, trajectory, error in results:
            tid = _task_id_of(task)
            if error is not None:
                logger.exception("task %s failed in worker: %s", tid, error)
                # Synthesize a failure trajectory so the summary stays honest.
                trajectory = _synthesize_failure_trajectory(task, error, seed)
            _write_trajectory(out_dir, task, trajectory)
            accumulated.append(trajectory)

            with library_lock.write():
                library = method.update_library(library, trajectory, task)

            completed.add(tid)

        # Snapshot + checkpoint at the end of each batch
        snap_path = snapshot_library_path(out_dir, len(completed))
        library.save(snap_path)
        rel_snap = str(snap_path.relative_to(out_dir))
        write_checkpoint(
            out_dir,
            {
                "method": method.name,
                "seed": seed,
                "harness": getattr(harness, "name", "?"),
                "completed_task_ids": sorted(completed),
                "last_library_snapshot": rel_snap,
            },
        )
        logger.info(
            "batch=%d completed=%d/%d library=%s",
            batch_index,
            len(completed),
            total,
            library.summary(),
        )

    ended_at = utc_iso_now()
    duration = time.time() - t_start

    summary = {
        "method": method.name,
        "seed": seed,
        "harness": getattr(harness, "name", "?"),
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_duration_sec": round(duration, 3),
        "task_count": total,
        "concurrency": concurrency,
        "snapshot_every": snapshot_every,
        "metrics": _summarize(accumulated),
        "library_final": library.summary(),
    }
    sp = summary_path(out_dir)
    sp.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def _synthesize_failure_trajectory(
    task: dict[str, Any], exc: BaseException, seed: int
) -> dict[str, Any]:
    """Build a minimal trajectory payload for a task that crashed at the
    harness level (not from within the LLM loop). Keeps schema parity with
    the langgraph harness output.
    """
    return {
        "result": {
            "harness": "runner_synth",
            "model": "",
            "task_id": _task_id_of(task),
            "category": task.get("category", ""),
            "difficulty": task.get("difficulty", ""),
            "seed": seed,
            "success": False,
            "steps": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "duration_sec": 0.0,
            "finish_reason": "runner_exception",
            "raw_path": "",
            "recovery_count": 0,
            "error_count": 1,
        },
        "verdict": {"success": False, "details": {"failures": ["runner exception"]}},
        "errors": [f"{type(exc).__name__}: {exc}"],
        "recoveries": [],
        "messages": [],
        "trajectory": [],
        "final_state": {},
    }
