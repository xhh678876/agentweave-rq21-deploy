"""Convert SWE-Skills-Bench tasks → OpenAI function-format AgentWeave tasks.

Each upstream task is a markdown prompt at
``upstream/tasks/batch1/<skill_id>.md`` plus an entry in
``upstream/config/benchmark_config.yaml`` that names the Docker image and the
evaluation commands.

We emit one ``task_NNN.json`` per skill, with the OpenAI function-calling tool
descriptors AgentWeave's LangGraph adapter (``harness-exp-langgraph``) already
knows how to dispatch::

    read_file_real, write_file_real, shell_exec_in_workspace, apply_patch

Plus an ``initial_state.files`` block that the LangGraph adapter materialises
into the live Docker workspace with::

    /workspace/TASK.md  -> the task prompt
    /workspace/.skill_id -> the upstream skill_id (for eval_adapter)

Because the upstream Docker images already contain the source repos pre-cloned
under ``/workspace/<repo>/``, the agent can now inspect and edit the original
source through the Docker-backed LangGraph tools.

Verdict computation is *deferred*: the bridge writes ``verification.type =
"swe_skills_docker_test"`` which the existing harness adapter does not know,
so the synchronous verdict will always be ``success=False``. The post-run
``eval_adapter.docker_evaluate_trajectory`` is what actually decides pass/fail.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .skill_loader import project_root


# ---------------------------------------------------------------------------
# OpenAI function descriptors for the tools the LangGraph harness implements.
# Must stay a strict subset of harness-exp-langgraph's TOOL_TABLE.
# ---------------------------------------------------------------------------


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_FUNCTION_TOOLS: list[dict[str, Any]] = [
    _fn(
        "read_file_real",
        "Read a file from the live Docker workspace. Read /workspace/TASK.md first.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _fn(
        "write_file_real",
        (
            "Write or overwrite a file in the live Docker workspace. Use absolute "
            "paths rooted at /workspace."
        ),
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _fn(
        "shell_exec_in_workspace",
        "Run a shell command in the live Docker workspace and return stdout plus stderr.",
        {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        },
        ["command"],
    ),
    _fn(
        "apply_patch",
        (
            "Apply a unified diff to the repository in the Docker workspace using "
            "patch -p1 from /workspace."
        ),
        {"diff": {"type": "string"}},
        ["diff"],
    ),
]


# ---------------------------------------------------------------------------
# Upstream config loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamSkill:
    """Resolved view of one upstream skill entry."""

    skill_id: str
    name: str
    description: str
    type: str
    base_image: str
    repo_url: str | None
    repo_commit: str | None
    eval_specs: tuple[dict[str, Any], ...]
    task_md: str
    skill_md_path: Path | None
    raw: dict[str, Any] = field(default_factory=dict)


def _read_upstream_config(root: Path) -> dict[str, Any]:
    cfg_path = root / "upstream" / "config" / "benchmark_config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"benchmark_config.yaml not found at {cfg_path}")
    text = cfg_path.read_text(encoding="utf-8")
    # Strip BOM if any
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("benchmark_config.yaml did not parse as a mapping")
    return data


def _extract_evaluation(skill_entry: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Pull the evaluation specs out of the (deeply nested) upstream layout.

    The upstream config nests ``evaluation:`` *inside* the second ``limits:``
    block (yes, really — the file has two ``limits`` keys per skill, with PyYAML
    keeping the second one). So look at both the skill-level and the
    environment-level keys.
    """
    candidates: list[Any] = []
    candidates.append(skill_entry.get("evaluation"))
    env_block = skill_entry.get("environment") or {}
    if isinstance(env_block, dict):
        candidates.append(env_block.get("evaluation"))
        limits_block = env_block.get("limits")
        if isinstance(limits_block, dict):
            candidates.append(limits_block.get("evaluation"))
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return tuple(candidate)
    return tuple()


def _resolve_task_md(root: Path, skill_id: str) -> str:
    """Pick the canonical task markdown (batch1 by convention)."""
    primary = root / "upstream" / "tasks" / "batch1" / f"{skill_id}.md"
    if primary.exists():
        return primary.read_text(encoding="utf-8")
    # Fallback: scan any batch directory.
    tasks_dir = root / "upstream" / "tasks"
    if tasks_dir.exists():
        for sub in sorted(tasks_dir.iterdir()):
            candidate = sub / f"{skill_id}.md"
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(f"No task markdown found for {skill_id}")


def _resolve_skill_md(root: Path, skill_id: str) -> Path | None:
    candidate = root / "upstream" / "skills" / skill_id / "SKILL.md"
    return candidate if candidate.exists() else None


def load_upstream_skills(root: Path | None = None) -> list[UpstreamSkill]:
    """Load every skill entry from the upstream benchmark_config.yaml.

    Skills missing a task markdown OR a human SKILL.md are skipped (with a
    note printed) so the bridge's 49-task contract is always met by definition.
    """
    base = root or project_root()
    cfg = _read_upstream_config(base)
    raw_skills = cfg.get("skills") or []
    out: list[UpstreamSkill] = []
    for entry in raw_skills:
        if not isinstance(entry, dict):
            continue
        skill_id = entry.get("id")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        try:
            task_md = _resolve_task_md(base, skill_id)
        except FileNotFoundError:
            continue
        skill_md_path = _resolve_skill_md(base, skill_id)
        env_block = entry.get("environment") or {}
        base_image = (env_block or {}).get("base_image") or ""
        if isinstance(base_image, str) and base_image and ":" not in base_image:
            base_image = f"{base_image}:latest"
        repo_block = entry.get("repo") or {}
        out.append(
            UpstreamSkill(
                skill_id=skill_id,
                name=entry.get("name") or skill_id,
                description=entry.get("description") or "",
                type=entry.get("type") or "unknown",
                base_image=base_image or "",
                repo_url=repo_block.get("url") if isinstance(repo_block, dict) else None,
                repo_commit=repo_block.get("commit") if isinstance(repo_block, dict) else None,
                eval_specs=_extract_evaluation(entry),
                task_md=task_md,
                skill_md_path=skill_md_path,
                raw=entry,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Goal prompt construction
# ---------------------------------------------------------------------------


_GOAL_PREFACE_TEMPLATE = """\
You are working on a real-world software engineering task from SWE-Skills-Bench.

The task description, target files, requirements, and acceptance criteria are \
stored at /workspace/TASK.md inside your Docker workspace. Read it first with \
read_file_real.

The source tree is available at: {repo_root}

Inspect the repository with shell_exec_in_workspace and read_file_real. Make \
changes with apply_patch when possible, or write_file_real when replacing a \
whole file. Use absolute paths for file tools.

After you finish, respond with TASK_COMPLETE.
"""


def _infer_repo_root(skill: "UpstreamSkill") -> str:
    """Best-effort: derive the in-container repo root from the upstream config.

    Upstream images extract the repo into ``/workspace/<repo_name>/``. We
    take the trailing path segment of ``repo.url`` and strip ``.git`` /
    trailing slashes. When ``repo.url`` is null (the PyTorch case where the
    image already has the source baked in), we fall back to ``/workspace``
    so the agent doesn't write to a wrong path.
    """
    url = skill.repo_url
    if not isinstance(url, str) or not url.strip():
        return "/workspace"
    base = url.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    base = base.strip()
    if not base:
        return "/workspace"
    return f"/workspace/{base}"


def build_goal(task_md: str, *, repo_root: str = "/workspace") -> str:
    """Return the prompt that becomes ``task.goal`` for the harness."""
    md = task_md.strip()
    preface = _GOAL_PREFACE_TEMPLATE.format(repo_root=repo_root)
    return f"{preface}\n--- TASK BRIEF (excerpt) ---\n{md}\n--- END BRIEF ---\n"


# ---------------------------------------------------------------------------
# Per-task JSON builder
# ---------------------------------------------------------------------------


_DIFFICULTY_BY_TYPE: dict[str, str] = {
    "repair": "hard",
    "fix": "medium",
    "feature": "hard",
    "audit": "medium",
    "refactor": "medium",
}


def _normalise_id(skill_id: str) -> str:
    """Return the AgentWeave task id (``swe_skills_<sanitised>``)."""
    clean = re.sub(r"[^a-zA-Z0-9_]+", "_", skill_id).strip("_").lower()
    return f"swe_skills_{clean}"


def _eval_command(eval_specs: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    """Pick the canonical evaluation command — unit_test if present else build_check."""
    for level in ("L2", "L3"):
        for spec in eval_specs:
            if (spec.get("level") == level) and spec.get("enabled", True):
                if spec.get("method") == "unit_test":
                    params = spec.get("params") or {}
                    return {
                        "method": "unit_test",
                        "command": params.get("test_command"),
                        "min_pass_rate": float(params.get("min_pass_rate", 1.0)),
                        "timeout": int(params.get("timeout", 600)),
                    }
    # Fall back to L1 build_check.
    for spec in eval_specs:
        if spec.get("level") == "L1" and spec.get("method") == "build_check":
            params = spec.get("params") or {}
            return {
                "method": "build_check",
                "command": params.get("build_command"),
                "expected_exit_code": int(params.get("expected_exit_code", 0)),
                "timeout": int(params.get("timeout", 600)),
            }
    return None


def _setup_command(repo_root: str) -> str:
    """Reset the baked repository before each task run."""
    quoted = shlex.quote(repo_root)
    return (
        f"if [ -d {quoted}/.git ]; then "
        f"cd {quoted} && git checkout HEAD -- . && git clean -fd; "
        "fi"
    )


def build_task_json(skill: UpstreamSkill, sequence: int) -> dict[str, Any]:
    """Build one task_NNN.json payload."""
    task_id = _normalise_id(skill.skill_id)
    difficulty = _DIFFICULTY_BY_TYPE.get(skill.type, "medium")
    repo_root = _infer_repo_root(skill)

    eval_spec = _eval_command(skill.eval_specs)

    initial_files = {
        "/workspace/TASK.md": skill.task_md,
        "/workspace/.skill_id": skill.skill_id,
    }

    verification = {
        "type": "swe_skills_docker_test",
        "skill_id": skill.skill_id,
        "docker_image": skill.base_image,
        "human_skill_path": (
            f"upstream/skills/{skill.skill_id}/SKILL.md"
            if skill.skill_md_path is not None
            else None
        ),
        "evaluation": eval_spec,
        "expected": {
            "files": {"/workspace/TASK.md": skill.task_md},
        },
    }

    return {
        "id": task_id,
        "category": "swe_skills_bench",
        "difficulty": difficulty,
        "goal": build_goal(skill.task_md, repo_root=repo_root),
        "tools": _FUNCTION_TOOLS,
        "workspace": {
            "docker_image": skill.base_image,
            "mount_workspace": "/workspace",
            "setup_command": _setup_command(repo_root),
        },
        "initial_state": {
            "task_id": task_id,
            "category": "swe_skills_bench",
            "files": initial_files,
        },
        "verification": verification,
        "oracle_metrics": {
            "min_steps": 3,
            "max_reasonable_steps": 20,
            "expected_input_tokens": 8000,
            "expected_output_tokens": 4000,
        },
        "swe_skills_meta": {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "type": skill.type,
            "description": skill.description,
            "base_image": skill.base_image,
            "repo_url": skill.repo_url,
            "repo_commit": skill.repo_commit,
            "repo_root": repo_root,
            "human_skill_path": (
                f"upstream/skills/{skill.skill_id}/SKILL.md"
                if skill.skill_md_path is not None
                else None
            ),
            "sequence": sequence,
        },
    }


# ---------------------------------------------------------------------------
# Bulk emitter
# ---------------------------------------------------------------------------


def build_all_tasks(
    *, root: Path | None = None, output_dir: Path | None = None
) -> dict[str, Any]:
    """Render every upstream skill → tasks/task_NNN.json.

    Returns a summary dict::

        {
            "total": 49,
            "skipped": [],
            "tasks": [{"id": ..., "skill_id": ..., "path": ..., "docker_image": ...}],
        }

    The INDEX.json sibling is written alongside the per-task files.
    """
    base = root or project_root()
    out_dir = output_dir or (base / "tasks")
    out_dir.mkdir(parents=True, exist_ok=True)

    skills = load_upstream_skills(base)
    # 49 is the contract from the README.
    skills = [s for s in skills if s.skill_md_path is not None]
    # Keep the very large PyTorch image out of the first few task_NNN files so
    # small limit-based smoke runs are not dominated by a multi-GB first pull.
    skills.sort(key=lambda s: ("swe-skills-bench-pytorch" in s.base_image, s.skill_id))

    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for idx, skill in enumerate(skills, start=1):
        try:
            payload = build_task_json(skill, sequence=idx)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"skill_id": skill.skill_id, "reason": str(exc)})
            continue
        filename = f"task_{idx:03d}.json"
        task_path = out_dir / filename
        task_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            rel_path = str(task_path.relative_to(base))
        except ValueError:
            # output_dir is outside the project root (e.g. a tmp dir during tests);
            # fall back to the absolute path.
            rel_path = str(task_path)
        written.append(
            {
                "id": payload["id"],
                "skill_id": skill.skill_id,
                "path": rel_path,
                "docker_image": skill.base_image,
                "has_human_skill": skill.skill_md_path is not None,
                "evaluation_method": (
                    payload["verification"]["evaluation"]["method"]
                    if payload["verification"]["evaluation"]
                    else None
                ),
            }
        )

    index_payload = {
        "schema_version": "1.0",
        "total": len(written),
        "skipped": skipped,
        "tasks": written,
    }
    (out_dir / "INDEX.json").write_text(
        json.dumps(index_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return index_payload
