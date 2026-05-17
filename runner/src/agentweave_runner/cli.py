"""CLI for the AgentWeave method runner.

Examples::

    # smoke
    python -m agentweave_runner.cli \\
        --method M4 --seed 0 \\
        --tasks ~/agentweave/pilot_experiment/tasks/ \\
        --harness langgraph \\
        --concurrency 4 --limit 5 \\
        --out /tmp/runner_smoke/

    # resume an interrupted run
    python -m agentweave_runner.cli \\
        --method M4 --seed 0 \\
        --tasks ~/agentweave/tasks_v2/ \\
        --harness langgraph \\
        --concurrency 32 \\
        --out ~/agentweave/runs/rq1/M4/seed_0/ \\
        --resume
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from agentweave_runner.harness import get_harness
from agentweave_runner.methods import get_method
from agentweave_runner.runner import run_method


logger = logging.getLogger("agentweave_runner")


def _load_tasks(tasks_arg: str, limit: int | None) -> list[dict[str, Any]]:
    """Load tasks from a directory of task_*.json files, or from a single file."""
    path = Path(tasks_arg).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"--tasks path does not exist: {path}")
    if path.is_file():
        return [json.loads(path.read_text(encoding="utf-8"))]
    files = sorted(path.glob("task_*.json"))
    if not files:
        # Tolerate v2 naming variations
        files = sorted(path.glob("*.json"))
    tasks: list[dict[str, Any]] = []
    for fp in files:
        try:
            tasks.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed task file %s: %s", fp, exc)
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentweave-runner",
        description=(
            "Run a method (M0/M1/M2/M3/M4) over a task set at a single seed via "
            "a chosen harness (langgraph or openclaw)."
        ),
    )
    parser.add_argument(
        "--method",
        required=True,
        choices=["M0", "M1", "M2", "M3", "M4"],
        help="Method id",
    )
    parser.add_argument("--seed", type=int, required=True, help="Run seed")
    parser.add_argument(
        "--tasks",
        required=True,
        help="Directory containing task_*.json files, or a single task JSON file",
    )
    from .harness import HARNESS_REGISTRY
    parser.add_argument(
        "--harness",
        default="langgraph",
        choices=sorted(HARNESS_REGISTRY.keys()),
        help="Harness adapter to use (default: langgraph)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory (trajectories + snapshots + summary go here)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=32,
        help="Max concurrent in-flight tasks within a mini-batch (default: 32)",
    )
    parser.add_argument(
        "--snapshot-every",
        type=int,
        default=10,
        help="Snapshot library after every N completed tasks (default: 10)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on number of tasks loaded (smoke-test convenience)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint in --out (default: off)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override model name (default: AGENTWEAVE_RUNNER_MODEL env or deepseek-v4-flash)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Override per-task step cap (default: env AGENTWEAVE_RUNNER_MAX_STEPS or 20)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tasks = _load_tasks(args.tasks, args.limit)
    if not tasks:
        logger.error("No tasks loaded from %s", args.tasks)
        return 2

    method = get_method(args.method)
    harness_kwargs: dict[str, Any] = {}
    if args.model:
        harness_kwargs["model"] = args.model
    if args.max_steps is not None:
        harness_kwargs["max_steps"] = args.max_steps
    harness = get_harness(args.harness, **harness_kwargs)

    out_dir = Path(args.out).expanduser().resolve()
    summary = run_method(
        method,
        seed=args.seed,
        tasks=tasks,
        harness=harness,
        out_dir=out_dir,
        concurrency=args.concurrency,
        snapshot_every=args.snapshot_every,
        resume=args.resume,
    )
    logger.info("Run summary: %s", json.dumps(summary["metrics"]))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
