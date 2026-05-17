"""CLI entry point for the SWE-Skills-Bench bridge.

Subcommands::

    install            clone upstream + pull docker images + build task JSONs
    build-tasks        re-run task JSON generation only (no clone/pull)
    list-tasks         dump tasks/INDEX.json contents
    eval-trajectory    run docker eval on one already-saved trajectory
    run-one            end-to-end smoke test: harness run + docker eval

Usage examples::

    swe-skills-bridge install
    swe-skills-bridge build-tasks
    swe-skills-bridge list-tasks
    swe-skills-bridge run-one --task-id add-uint-support \\
        --output /tmp/swe_one.json
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from .installer import install as run_install
from .eval_adapter import docker_evaluate_trajectory
from .skill_loader import project_root, load_human_skill
from .task_adapter import build_all_tasks


logger = logging.getLogger("swe-skills-bridge")


@click.group()
@click.option("--log-level", default="INFO", show_default=True)
def cli(log_level: str) -> None:
    """SWE-Skills-Bench bridge for AgentWeave."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )


@cli.command(name="install")
@click.option("--skip-clone", is_flag=True, help="Don't clone upstream (assume present)")
@click.option("--skip-docker-pull", is_flag=True, help="Don't pre-pull Docker images")
def install_cmd(skip_clone: bool, skip_docker_pull: bool) -> None:
    """One-time install: upstream clone + image pull + build tasks."""
    report = run_install(
        skip_clone=skip_clone,
        skip_docker_pull=skip_docker_pull,
    )
    click.echo(json.dumps(report.to_dict(), indent=2))


@cli.command(name="build-tasks")
def build_tasks_cmd() -> None:
    """Re-generate the per-task JSON files under tasks/."""
    summary = build_all_tasks()
    click.echo(json.dumps(summary, indent=2))


@cli.command(name="list-tasks")
@click.option("--json-out", is_flag=True, help="Emit the INDEX.json verbatim")
def list_tasks_cmd(json_out: bool) -> None:
    """Print the task index (49 entries when the install completed)."""
    index_path = project_root() / "tasks" / "INDEX.json"
    if not index_path.exists():
        raise click.ClickException(
            "tasks/INDEX.json missing — run `swe-skills-bridge build-tasks` first"
        )
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if json_out:
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Total tasks: {payload['total']}")
    click.echo(f"Skipped:     {len(payload.get('skipped') or [])}")
    click.echo("-" * 78)
    for entry in payload["tasks"]:
        eval_method = entry.get("evaluation_method") or "?"
        click.echo(
            f"  {entry['id']:48s}  image={entry.get('docker_image') or '-':38s}"
            f"  method={eval_method}"
        )


@cli.command(name="show-skill")
@click.option("--skill-id", required=True)
def show_skill_cmd(skill_id: str) -> None:
    """Dump the upstream SKILL.md for one skill (used to wire M_human runs)."""
    text = load_human_skill(skill_id)
    click.echo(text)


@cli.command(name="eval-trajectory")
@click.option("--trajectory", required=True, type=click.Path(exists=True))
@click.option("--no-rewrite", is_flag=True, help="Don't mutate the trajectory file")
def eval_trajectory_cmd(trajectory: str, no_rewrite: bool) -> None:
    """Run Docker eval on one saved trajectory."""
    verdict = docker_evaluate_trajectory(
        trajectory, rewrite_in_place=not no_rewrite
    )
    click.echo(json.dumps(verdict.to_dict(), indent=2))


@cli.command(name="eval-batch")
@click.option(
    "--runner-out",
    required=True,
    type=click.Path(exists=True),
    help="Directory produced by `run-method` (contains swe_skills_*.json files)",
)
@click.option("--no-rewrite", is_flag=True, help="Don't mutate trajectory files in place")
@click.option(
    "--pattern",
    default="swe_skills_*.json",
    show_default=True,
    help="Glob for trajectory files inside --runner-out",
)
def eval_batch_cmd(runner_out: str, no_rewrite: bool, pattern: str) -> None:
    """Run Docker eval over every SWE-Skills trajectory in a runner output dir."""
    runner_dir = Path(runner_out).expanduser().resolve()
    files = sorted(runner_dir.glob(pattern))
    if not files:
        raise click.ClickException(f"no files matched {pattern} in {runner_dir}")
    results: list[dict[str, Any]] = []
    n_pass = 0
    for fp in files:
        verdict = docker_evaluate_trajectory(fp, rewrite_in_place=not no_rewrite)
        if verdict.success:
            n_pass += 1
        results.append(
            {
                "trajectory": fp.name,
                "success": verdict.success,
                "method": verdict.method,
                "pass_rate": verdict.pass_rate,
                "exit_code": verdict.exit_code,
                "skipped_reason": verdict.skipped_reason,
            }
        )
    summary = {
        "runner_out": str(runner_dir),
        "evaluated": len(files),
        "passed": n_pass,
        "pass_rate": n_pass / len(files) if files else 0.0,
        "results": results,
    }
    click.echo(json.dumps(summary, indent=2))


@cli.command(name="run-one")
@click.option("--task-id", required=True, help="Upstream skill_id (e.g. add-uint-support)")
@click.option("--output", required=True, type=click.Path(), help="Trajectory JSON path")
@click.option("--seed", type=int, default=0, show_default=True)
@click.option("--max-steps", type=int, default=20, show_default=True)
@click.option("--model", default=None, help="Override the harness's default model")
@click.option("--skip-docker-eval", is_flag=True, help="Run harness only; skip Docker eval")
def run_one_cmd(
    task_id: str,
    output: str,
    seed: int,
    max_steps: int,
    model: str | None,
    skip_docker_eval: bool,
) -> None:
    """End-to-end smoke: harness run for one task → Docker eval."""
    # Map skill_id -> task_NNN.json path
    index_path = project_root() / "tasks" / "INDEX.json"
    if not index_path.exists():
        raise click.ClickException("tasks not built yet; run `install` or `build-tasks`")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    match: dict[str, Any] | None = None
    for entry in index["tasks"]:
        if entry["skill_id"] == task_id or entry["id"] == task_id:
            match = entry
            break
    if match is None:
        raise click.ClickException(f"no task found for skill_id={task_id!r}")

    task_path = project_root() / match["path"]

    # Drive the LangGraph harness without depending on its CLI args layout.
    try:
        from langgraph_harness.adapter import run_task  # type: ignore
    except ImportError as exc:  # noqa: BLE001
        raise click.ClickException(
            "langgraph_harness not importable. Activate the harness venv first: "
            "`source ~/agentweave/harness-exp-langgraph/.venv/bin/activate`"
        ) from exc

    task = json.loads(task_path.read_text(encoding="utf-8"))

    kwargs: dict[str, Any] = dict(seed=seed, max_steps=max_steps)
    if model:
        kwargs["model"] = model
    payload = run_task(task, library=None, **kwargs)

    out_path = Path(output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if skip_docker_eval:
        click.echo(f"Harness run done → {out_path}")
        return

    verdict = docker_evaluate_trajectory(out_path)
    summary = {
        "task_id": task["id"],
        "skill_id": task_id,
        "trajectory_path": str(out_path),
        "verdict": verdict.to_dict(),
        "harness_steps": (payload.get("result") or {}).get("steps"),
        "harness_success_before_eval": (payload.get("result") or {}).get("success"),
    }
    click.echo(json.dumps(summary, indent=2))


if __name__ == "__main__":
    cli()
