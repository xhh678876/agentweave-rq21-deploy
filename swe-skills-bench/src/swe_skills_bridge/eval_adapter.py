"""Docker-backed evaluator for SWE-Skills-Bench trajectories.

The bridge's pipeline is:

1. The agent runs inside the LangGraph harness using mocked file tools.
2. Every file the agent writes goes into ``MockToolState.files`` and ends up in
   ``trajectory["result"]["final_state"]["files"]`` of the saved trajectory.
3. After the harness returns, we call :func:`docker_evaluate_trajectory` which
   pulls the agent-written files (anything *not* part of the seeded inputs),
   pushes them into the task's upstream Docker image (which already contains
   the original source tree), executes the upstream test command, and rewrites
   ``trajectory["result"]["verdict"]`` and ``trajectory["result"]["success"]``.

This module is import-safe even when Docker isn't running — actual eval calls
fail loudly with a clear error so the runner can mark them as ``docker_unavailable``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Files we seed into the mocked workspace; these are NOT agent output and must
# be stripped before materialising into the real Docker image.
_SEEDED_FILES = frozenset({"/workspace/TASK.md", "/workspace/.skill_id"})


# ---------------------------------------------------------------------------
# Public verdict envelope
# ---------------------------------------------------------------------------


@dataclass
class DockerVerdict:
    success: bool
    method: str
    exit_code: int
    pass_rate: float | None
    stdout_tail: str
    stderr_tail: str
    duration_sec: float
    error: str | None = None
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "method": self.method,
            "exit_code": self.exit_code,
            "pass_rate": self.pass_rate,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "duration_sec": round(self.duration_sec, 3),
            "error": self.error,
            "skipped_reason": self.skipped_reason,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _docker_client():
    """Lazy import + connect — keeps the package importable without Docker."""
    import docker  # type: ignore

    client = docker.from_env()
    client.ping()
    return client


def _load_upstream_test_files(skill_id: str) -> dict[str, str]:
    """Read upstream/tests/batch1/test_<sanitised>.py (+ shared helpers).

    The upstream pytest commands assume tests live at /workspace/tests/.
    We translate dashes to underscores to mirror the upstream filename
    convention (e.g. ``bash-defensive-patterns`` → ``test_bash_defensive_patterns.py``).
    Returns an empty dict when the test file isn't present — eval will then
    fail with the upstream error message, which is the desired behaviour.
    """
    from .skill_loader import project_root

    sanitised = skill_id.replace("-", "_")
    candidates: list[Path] = []
    base = project_root() / "upstream" / "tests"
    for batch in ("batch1", "batch2", "batch3", "batch4", "batch5",
                  "batch6", "batch7", "batch8", "batch9", "batch10"):
        batch_dir = base / batch
        if not batch_dir.exists():
            continue
        candidates.append(batch_dir / f"test_{sanitised}.py")
        # Shared helpers used by some tests.
        helper = batch_dir / "_dependency_utils.py"
        if helper.exists():
            candidates.append(helper)
        if candidates and candidates[0].exists():
            break

    out: dict[str, str] = {}
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.exists():
            continue
        target_path = f"/workspace/tests/{candidate.name}"
        if target_path in seen:
            continue
        out[target_path] = candidate.read_text(encoding="utf-8")
        seen.add(target_path)
    return out


def _agent_output_files(trajectory: dict[str, Any]) -> dict[str, str]:
    """Pull the agent-written file map out of a saved trajectory.

    The LangGraph harness stores ``final_state`` at the trajectory top level
    (see harness-exp-langgraph/.../trajectory.py). We fall back to the legacy
    nested location for forward-compat with future harnesses.
    """
    final_state = trajectory.get("final_state")
    if not isinstance(final_state, dict):
        result = trajectory.get("result") or {}
        final_state = result.get("final_state") or {}
    files = (final_state or {}).get("files") or {}
    if not isinstance(files, dict):
        return {}
    files_out = {
        path: (content if isinstance(content, str) else json.dumps(content))
        for path, content in files.items()
        if path not in _SEEDED_FILES
    }
    # Docker-backed LangGraph tasks mutate the live container via write_file_real.
    # The container is intentionally cleaned up after the run, so replay those
    # writes into the fresh evaluator container from the saved trajectory.
    for step in trajectory.get("trajectory") or []:
        if not isinstance(step, dict) or step.get("tool") != "write_file_real":
            continue
        args = step.get("arguments") or {}
        path = args.get("path")
        content = args.get("content")
        if isinstance(path, str) and path not in _SEEDED_FILES:
            files_out[path] = content if isinstance(content, str) else json.dumps(content)
    return files_out


def _tail(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


# ---------------------------------------------------------------------------
# Test-output parsing
# ---------------------------------------------------------------------------


def _parse_pytest_pass_rate(stdout: str) -> float | None:
    """Best-effort pass-rate parse from a pytest --tb=short output.

    Pytest's summary line can appear in either order — ``5 passed, 2 failed`` or
    ``4 failed, 7 passed`` — and may also include ``error`` / ``skipped``.
    We don't try to lock in a strict order; we just look for the summary banner
    and pick out each counter individually.
    """
    import re

    banner_re = re.compile(r"(?:^|\n)=+(?P<inner>[^\n]+?)=+\s*$", re.MULTILINE)
    counter_re = re.compile(
        r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors|skipped|xfailed|xpassed)\b"
    )

    last_counts: dict[str, int] | None = None
    for banner_match in banner_re.finditer(stdout):
        inner = banner_match.group("inner")
        counts: dict[str, int] = {}
        for cm in counter_re.finditer(inner):
            kind = cm.group("kind")
            if kind == "errors":
                kind = "error"
            counts[kind] = counts.get(kind, 0) + int(cm.group("count"))
        if counts:
            last_counts = counts

    if not last_counts:
        return None

    passed = last_counts.get("passed", 0)
    failed = last_counts.get("failed", 0)
    errored = last_counts.get("error", 0)
    total = passed + failed + errored
    if total == 0:
        return None
    return passed / total


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def docker_evaluate_trajectory(
    trajectory_path: str | Path,
    *,
    rewrite_in_place: bool = True,
    docker_timeout_sec: int | None = None,
) -> DockerVerdict:
    """Evaluate one harness trajectory by running its upstream tests in Docker.

    Behaviour:
    - If ``verification.type != "swe_skills_docker_test"``, the call is a no-op
      and returns a ``skipped`` verdict.
    - If Docker is unavailable, returns ``success=False`` with
      ``skipped_reason="docker_unavailable"``. The trajectory is left untouched
      unless ``rewrite_in_place`` is True (in which case verdict.error captures the
      reason).
    - Otherwise: start container → write agent files → run test command →
      decide pass/fail → optionally rewrite the trajectory file.
    """
    trajectory_path = Path(trajectory_path).expanduser().resolve()
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))

    # LangGraph adapter stores the task at top-level under "task_spec";
    # legacy "task" key kept for forward-compat.
    task = trajectory.get("task_spec") or trajectory.get("task") or {}
    verification = task.get("verification") or {}

    if verification.get("type") != "swe_skills_docker_test":
        return DockerVerdict(
            success=False,
            method="skipped",
            exit_code=-1,
            pass_rate=None,
            stdout_tail="",
            stderr_tail="",
            duration_sec=0.0,
            skipped_reason="not a swe_skills_docker_test trajectory",
        )

    eval_spec = verification.get("evaluation") or {}
    docker_image = verification.get("docker_image") or ""
    skill_id = verification.get("skill_id") or ""

    if not docker_image or not eval_spec or not eval_spec.get("command"):
        verdict = DockerVerdict(
            success=False,
            method=eval_spec.get("method", "unknown"),
            exit_code=-1,
            pass_rate=None,
            stdout_tail="",
            stderr_tail="",
            duration_sec=0.0,
            skipped_reason="missing docker_image or evaluation command",
        )
        _maybe_rewrite(trajectory, trajectory_path, verdict, rewrite_in_place)
        return verdict

    if _is_truthy_env("SWE_SKILLS_BRIDGE_DRY_RUN"):
        verdict = DockerVerdict(
            success=False,
            method=eval_spec.get("method", "unknown"),
            exit_code=-1,
            pass_rate=None,
            stdout_tail="",
            stderr_tail="",
            duration_sec=0.0,
            skipped_reason="dry_run_env",
        )
        _maybe_rewrite(trajectory, trajectory_path, verdict, rewrite_in_place)
        return verdict

    # Connect to Docker; on failure, fall back gracefully.
    try:
        client = _docker_client()
    except Exception as exc:  # noqa: BLE001
        verdict = DockerVerdict(
            success=False,
            method=eval_spec.get("method", "unknown"),
            exit_code=-1,
            pass_rate=None,
            stdout_tail="",
            stderr_tail="",
            duration_sec=0.0,
            error=f"docker_unavailable: {type(exc).__name__}: {exc}",
            skipped_reason="docker_unavailable",
        )
        _maybe_rewrite(trajectory, trajectory_path, verdict, rewrite_in_place)
        return verdict

    agent_files = _agent_output_files(trajectory)
    # Also inject the upstream test file into the container — the upstream
    # lifecycle does this via a separate copy step. Without it, pytest runs
    # against a missing file path inside the image.
    test_files = _load_upstream_test_files(skill_id)
    command = eval_spec["command"]
    timeout = docker_timeout_sec or int(eval_spec.get("timeout", 600))
    method = eval_spec.get("method", "unit_test")

    container = None
    started = time.time()
    try:
        container_name = f"swe-skills-eval-{skill_id}-{uuid.uuid4().hex[:8]}"
        container = client.containers.run(
            docker_image,
            name=container_name,
            command="sleep infinity",
            detach=True,
            network_mode=os.environ.get("SWE_SKILLS_NETWORK_MODE", "bridge"),
            working_dir="/workspace",
        )
        _ensure_running(container)
        # Push the upstream test files first so the evaluator harness picks
        # them up at the canonical /workspace/tests/test_<skill>.py path used by
        # the upstream pytest commands.
        _push_files(container, test_files)
        _push_files(container, agent_files)

        exec_result = container.exec_run(
            cmd=["bash", "-lc", command],
            workdir="/workspace",
            demux=True,
        )
        exit_code = exec_result.exit_code
        stdout_bytes, stderr_bytes = exec_result.output
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")

        if method == "build_check":
            success = exit_code == int(eval_spec.get("expected_exit_code", 0))
            pass_rate = 1.0 if success else 0.0
        else:
            pass_rate = _parse_pytest_pass_rate(stdout) or (1.0 if exit_code == 0 else 0.0)
            min_pass = float(eval_spec.get("min_pass_rate", 1.0))
            success = exit_code == 0 and pass_rate >= min_pass

        verdict = DockerVerdict(
            success=bool(success),
            method=method,
            exit_code=int(exit_code),
            pass_rate=pass_rate,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            duration_sec=time.time() - started,
        )
    except Exception as exc:  # noqa: BLE001
        verdict = DockerVerdict(
            success=False,
            method=method,
            exit_code=-1,
            pass_rate=None,
            stdout_tail="",
            stderr_tail="",
            duration_sec=time.time() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if container is not None:
            try:
                container.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass

    _maybe_rewrite(trajectory, trajectory_path, verdict, rewrite_in_place)
    return verdict


def _ensure_running(container) -> None:
    """Wait briefly for the sleep-infinity container to be alive."""
    for _ in range(20):
        container.reload()
        if container.status == "running":
            return
        time.sleep(0.1)


def _push_files(container, files: dict[str, str]) -> None:
    """Write each ``path: content`` into the container via put_archive.

    Files are grouped by directory so we issue one ``put_archive`` per dir.
    """
    if not files:
        return
    by_dir: dict[str, dict[str, bytes]] = {}
    for path, content in files.items():
        if not path.startswith("/"):
            path = "/" + path
        dir_path = os.path.dirname(path) or "/"
        by_dir.setdefault(dir_path, {})[os.path.basename(path)] = content.encode("utf-8")

    # Create directories first.
    if by_dir:
        mkdir = "mkdir -p " + " ".join(f"'{d}'" for d in sorted(by_dir.keys()))
        container.exec_run(["bash", "-lc", mkdir])

    for dir_path, dir_files in by_dir.items():
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            for filename, content in dir_files.items():
                info = tarfile.TarInfo(name=filename)
                info.size = len(content)
                info.mtime = time.time()
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
        tar_stream.seek(0)
        container.put_archive(dir_path, tar_stream)


def _maybe_rewrite(
    trajectory: dict[str, Any],
    path: Path,
    verdict: DockerVerdict,
    rewrite_in_place: bool,
) -> None:
    """Write the docker verdict back into the trajectory.

    We update both the LangGraph schema (top-level ``verdict`` + flat
    ``result.success``) and stash the docker-specific details under
    ``result.docker_verdict`` so the pipeline's L1 ingestion can pick them up
    without colliding with the harness's own verification verdict.
    """
    if not rewrite_in_place:
        return
    docker_dict = verdict.to_dict()

    # Stash the raw docker verdict on the result row.
    result = trajectory.setdefault("result", {})
    result["docker_verdict"] = docker_dict

    # Promote to the top-level verdict ONLY when we actually ran the eval
    # (not skipped). The harness's own verdict for unknown verification
    # types is always success=False, so this is the authoritative answer.
    if verdict.skipped_reason is None:
        result["success"] = bool(verdict.success)
        trajectory["verdict"] = {
            "success": bool(verdict.success),
            "details": {
                "method": verdict.method,
                "exit_code": verdict.exit_code,
                "pass_rate": verdict.pass_rate,
            },
        }
    else:
        result.setdefault("success", False)

    path.write_text(
        json.dumps(trajectory, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DockerVerdict",
    "docker_evaluate_trajectory",
]
