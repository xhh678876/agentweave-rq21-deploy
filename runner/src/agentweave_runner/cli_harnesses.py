"""CLI-based harness adapters for RQ2.1 cross-harness experiments.

Each adapter wraps one real-world agent CLI (Claude Code, Codex, Hermes, OpenClaw)
configured to use the same backend LLM (deepseek-v4-flash) so the only varying
dimension is harness architecture.

Trajectory schema returned matches the LangGraph adapter (OpenAI messages style)
so the existing runner + pipeline can consume them unchanged.

Wrapper scripts (set up earlier in the project):
- ~/agentweave/claude-code-exp/bin/claude-exp
- ~/agentweave/codex-exp/bin/codex-exp
- ~/agentweave/hermes-exp/bin/hermes-exp
- ~/agentweave/harness-exp/bin/openclaw-exp

Each wrapper handles its own env isolation (CLAUDE_CONFIG_DIR / CODEX_HOME /
HERMES_HOME) and DeepSeek routing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

HOME = Path.home()
logger = logging.getLogger(__name__)

# Wall-clock headroom we add ON TOP of each task's eval_spec.timeout. The
# docker SDK has no Python-level timeout on exec_run, so if a container or the
# daemon hangs we'd deadlock the whole ThreadPool batch. Per-task wall budget
# is therefore (eval_spec.timeout + headroom). Headroom covers container start,
# file push, and result marshaling.
_DOCKER_EVAL_HEADROOM_SEC = int(os.environ.get("AGENTWEAVE_DOCKER_EVAL_HEADROOM", "30"))
# Absolute ceiling — even if a task spec asks for more, we cap here. Prevents a
# pathological spec from re-introducing the deadlock symptom.
_DOCKER_EVAL_MAX_WALL_SEC = int(os.environ.get("AGENTWEAVE_DOCKER_EVAL_MAX_WALL", "900"))


def _recover_timeout_output(e: subprocess.TimeoutExpired) -> tuple[str, str]:
    """Decode partial stdout/stderr from a TimeoutExpired exception.

    Python's ``subprocess.TimeoutExpired`` exposes whatever the child wrote
    before the timeout fired, but it returns raw bytes for those attributes
    even when ``text=True`` was passed to ``subprocess.run``. We always want
    decoded strings so the same parsing path as the success path can run on
    the partial output.

    Without this, the timeout path was hardcoding ``steps=0, messages=[]`` and
    discarding 100+ KB of real model output — making ~40% of SWE-Skills
    trajectories look like the agent never started when in fact it had been
    iterating for the full timeout budget.
    """
    so = e.stdout
    if isinstance(so, bytes):
        so = so.decode("utf-8", errors="replace")
    elif so is None:
        so = ""
    se = e.stderr
    if isinstance(se, bytes):
        se = se.decode("utf-8", errors="replace")
    elif se is None:
        se = ""
    return so, se


def _docker_eval_wall_budget(task: dict[str, Any]) -> int:
    """Per-task wall-clock budget for the eval thread.

    Uses ``verification.evaluation.timeout`` (the same value the eval_adapter
    passes to its internal logic) plus a fixed headroom for container startup
    and file push. Capped at _DOCKER_EVAL_MAX_WALL_SEC to keep deadlock blast
    radius bounded.
    """
    verification = task.get("verification") or {}
    eval_spec = verification.get("evaluation") or {}
    base = int(eval_spec.get("timeout", 600))
    budget = base + _DOCKER_EVAL_HEADROOM_SEC
    return min(budget, _DOCKER_EVAL_MAX_WALL_SEC)


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _build_prompt(
    task: dict[str, Any],
    library: dict[str, Any] | None,
    *,
    sandbox: Path | None = None,
) -> str:
    """Build a single string prompt from task + library injection.

    CLIs receive a flat prompt; library DO/DO NOT guidance gets prepended as
    system-prompt-style instructions inside the same string.

    If ``sandbox`` is supplied, prepend a workspace-root preamble telling the
    agent that every absolute path mentioned in the goal is relative to that
    sandbox root.
    """
    parts: list[str] = []

    if sandbox is not None:
        parts.append(f"Workspace root: {sandbox}")
        parts.append(
            "All filesystem paths in this task are inside that workspace root. "
            "Read/write files there, do NOT touch your real home or root filesystem."
        )
        parts.append("")

    if library:
        do_not = library.get("do_not_entries") or []
        do_entries = library.get("do_entries") or []
        if do_not or do_entries:
            parts.append("Library guidance (constraint-first):")
            parts.append("")
            if do_not:
                parts.append("DO NOT:")
                for item in do_not:
                    parts.append(f"- {item}")
                parts.append("")
            if do_entries:
                parts.append("DO:")
                for item in do_entries:
                    parts.append(f"- {item}")
                parts.append("")
            parts.append(
                "Treat DO NOT items as hard constraints and DO items as preferred patterns."
            )
            parts.append("")

    parts.append("Task:")
    parts.append(task.get("goal", "").strip())
    parts.append("")
    parts.append("After completing the task, respond with exactly TASK_COMPLETE.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sandbox: per-task temp dir with task's initial_state materialized.
# ---------------------------------------------------------------------------


def _setup_sandbox(task: dict[str, Any]) -> Path:
    """Create a fresh temp dir and write task.initial_state.files into it.

    For tasks with ``workspace.mount_workspace`` (e.g. SWE-Skills), paths like
    ``/workspace/TASK.md`` are mapped to ``<sandbox>/TASK.md`` (strip mount).
    Otherwise paths are placed verbatim relative to sandbox root, e.g.
    ``/notes/todo.md`` → ``<sandbox>/notes/todo.md``.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="agentweave-task-"))
    workspace = task.get("workspace") or {}
    mount = (workspace.get("mount_workspace") or "").rstrip("/")
    initial = task.get("initial_state") or {}
    files = initial.get("files") or {}
    for relpath, content in files.items():
        path = str(relpath)
        # Strip mount prefix if task is workspace-mounted (SWE-Skills)
        if mount and path.startswith(mount + "/"):
            path = path[len(mount):]
        target = sandbox / Path(path.lstrip("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(str(content if content is not None else ""))
    return sandbox


def _verify_sandbox(task: dict[str, Any], sandbox: Path) -> tuple[bool, list[str]]:
    """Walk verification.expected.files and check each file in the sandbox.

    Returns (success, list_of_failure_messages).

    Falls back to Docker pytest for ``swe_skills_docker_test`` verification —
    caller should use _verify_docker_or_state for SWE-Skills tasks. This
    simple path only handles ``state_match`` style verification.
    """
    expected = (task.get("verification") or {}).get("expected") or {}
    files = expected.get("files") or {}
    failures: list[str] = []
    for relpath, want_content in files.items():
        target = sandbox / Path(str(relpath).lstrip("/"))
        if not target.exists():
            failures.append(f"missing: {relpath}")
            continue
        try:
            got = target.read_text()
        except UnicodeDecodeError:
            got = target.read_bytes().decode("utf-8", errors="replace")
        want = "" if want_content is None else str(want_content)
        if got != want:
            failures.append(
                f"mismatch: {relpath} "
                f"(want {len(want)}B, got {len(got)}B)"
            )
    return (not failures), failures


def _verify_docker_or_state(
    task: dict[str, Any],
    sandbox: Path,
    envelope: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any] | None]:
    """Verify a task, dispatching to either Docker pytest or simple state match.

    Returns (success, failure_messages, docker_verdict_dict_or_None).
    For docker tasks, envelope is mutated in-place to add final_state.files +
    task_spec; the docker verdict dict is also returned for envelope['verdict'].
    """
    if _is_swe_skills_docker_task(task):
        success, verdict_dict = _run_swe_skills_eval_with_timeout(
            envelope, task, sandbox, timeout_sec=_docker_eval_wall_budget(task)
        )
        if success:
            return True, [], verdict_dict
        # Build a concise failure summary
        reason = verdict_dict.get("error") or verdict_dict.get("skipped_reason") or "tests failed"
        pass_rate = verdict_dict.get("pass_rate")
        if pass_rate is not None:
            reason = f"pass_rate={pass_rate} (need {((task.get('verification') or {}).get('evaluation') or {}).get('min_pass_rate', 1.0)})"
        return False, [reason], verdict_dict
    success, failures = _verify_sandbox(task, sandbox)
    return success, failures, None


def _cleanup_sandbox(sandbox: Path) -> None:
    shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# SWE-Skills (Docker-eval) support: collect agent's written files + run pytest
# ---------------------------------------------------------------------------


def _is_swe_skills_docker_task(task: dict[str, Any]) -> bool:
    return (task.get("verification") or {}).get("type") == "swe_skills_docker_test"


def _collect_sandbox_files(
    task: dict[str, Any], sandbox: Path
) -> dict[str, str]:
    """Walk sandbox, collect non-seeded files, key them by docker-side path.

    Files seeded via initial_state.files are NOT skipped — we always include
    seeded TASK.md so the verifier has full context. Excludes:
    - binary files (UTF-8 decode failure)
    - dotfile-prefixed directory components (.git, .openclaw, .claude/, etc.)
    - OpenClaw embedded-agent self-written personality docs

    Caps content at 200KB per file to keep trajectories sane.
    """
    workspace = task.get("workspace") or {}
    mount = workspace.get("mount_workspace", "/workspace").rstrip("/")

    # Files the OpenClaw embedded agent writes into its workspace automatically
    # (its "personality" docs). These are NOT agent output for the user task,
    # they're harness-internal. Skip when collecting agent files.
    HARNESS_DOCS = {
        "AGENTS.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "SOUL.md",
        "CLAUDE.md",
        "TOOLS.md",
        "USER.md",
        "MEMORY.md",
        "README.md",
    }

    out: dict[str, str] = {}
    for f in sandbox.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(sandbox).as_posix()
        # Skip files inside hidden directories (.git, .openclaw, .claude, ...)
        if any(part.startswith(".") for part in rel.split("/")[:-1]):
            continue
        # Skip top-level harness personality files
        if rel in HARNESS_DOCS:
            continue
        # Skip hidden files at any depth
        if Path(rel).name.startswith("."):
            # exception: allow .skill_id since SWE-Skills seeds it
            if Path(rel).name != ".skill_id":
                continue
        docker_path = f"{mount}/{rel}" if mount else f"/{rel}"
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(content) > 200_000:
            content = content[:200_000] + "\n... (truncated)"
        out[docker_path] = content
    return out


def _run_swe_skills_eval(
    trajectory_payload: dict[str, Any],
    task: dict[str, Any],
    sandbox: Path,
) -> tuple[bool, dict[str, Any]]:
    """For docker-test tasks: write trajectory + final_state.files, run eval.

    Returns (success, verdict_dict). Verdict_dict is the DockerVerdict
    serialised to a plain dict suitable for inclusion in envelope['verdict'].
    """
    # 1) Collect files agent left in sandbox
    final_files = _collect_sandbox_files(task, sandbox)
    trajectory_payload["final_state"] = {"files": final_files}
    # 2) Stuff task_spec so eval_adapter can find verification
    trajectory_payload["task_spec"] = task

    # 3) Save to a temp trajectory file
    eval_tmp = Path(tempfile.mkdtemp(prefix="agentweave-eval-"))
    traj_path = eval_tmp / "trajectory.json"
    traj_path.write_text(json.dumps(trajectory_payload), encoding="utf-8")

    # 4) Import + call docker_evaluate_trajectory
    try:
        bridge_src = (
            HOME / "agentweave" / "swe-skills-bench" / "src"
        )
        import sys

        if str(bridge_src) not in sys.path:
            sys.path.insert(0, str(bridge_src))

        # Point docker SDK at the colima socket (default /var/run/docker.sock
        # doesn't exist on macOS when using colima)
        colima_sock = HOME / ".colima" / "default" / "docker.sock"
        if colima_sock.exists() and "DOCKER_HOST" not in os.environ:
            os.environ["DOCKER_HOST"] = f"unix://{colima_sock}"

        from swe_skills_bridge.eval_adapter import docker_evaluate_trajectory  # type: ignore

        verdict = docker_evaluate_trajectory(traj_path, rewrite_in_place=False)
        verdict_dict = {
            "success": verdict.success,
            "method": verdict.method,
            "exit_code": verdict.exit_code,
            "pass_rate": verdict.pass_rate,
            "stdout_tail": verdict.stdout_tail,
            "stderr_tail": verdict.stderr_tail,
            "duration_sec": verdict.duration_sec,
            "skipped_reason": getattr(verdict, "skipped_reason", None),
            "error": getattr(verdict, "error", None),
        }
        return verdict.success, verdict_dict
    except Exception as e:
        return False, {
            "success": False,
            "method": "error",
            "error": f"eval invocation failed: {type(e).__name__}: {e}",
        }
    finally:
        shutil.rmtree(eval_tmp, ignore_errors=True)


def _run_swe_skills_eval_with_timeout(
    trajectory_payload: dict[str, Any],
    task: dict[str, Any],
    sandbox: Path,
    *,
    timeout_sec: int,
) -> tuple[bool, dict[str, Any]]:
    """Wall-clock bounded wrapper around _run_swe_skills_eval.

    The docker SDK's exec_run has no Python-level timeout — if the daemon or
    container hangs, the calling thread blocks forever on a socket recv.
    Inside our ThreadPoolExecutor that means one stuck eval freezes the whole
    batch (since run_in_parallel waits for all futures). We wrap the eval in
    a daemon thread and abandon it on wall-clock timeout: the leaked thread is
    harmless (it'll either unblock or die with the process), but the worker
    returns control to the ThreadPool so the batch can finish.
    """
    holder: dict[str, Any] = {"value": None, "done": False, "exc": None}

    def _worker() -> None:
        try:
            holder["value"] = _run_swe_skills_eval(trajectory_payload, task, sandbox)
        except BaseException as exc:
            holder["exc"] = exc
        finally:
            holder["done"] = True

    t = threading.Thread(
        target=_worker,
        name=f"swe-eval-{task.get('id', 'unknown')}",
        daemon=True,
    )
    t.start()
    t.join(timeout=timeout_sec)

    if not holder["done"]:
        logger.warning(
            "docker eval HUNG >%ds for task %s; abandoning thread, marking failure",
            timeout_sec,
            task.get("id", "?"),
        )
        return False, {
            "success": False,
            "method": "error",
            "exit_code": -1,
            "pass_rate": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_sec": float(timeout_sec),
            "error": f"docker_eval_wall_timeout_{timeout_sec}s",
            "skipped_reason": "docker_eval_wall_timeout",
        }

    if holder["exc"] is not None:
        # Same envelope shape as _run_swe_skills_eval's exception path
        exc = holder["exc"]
        return False, {
            "success": False,
            "method": "error",
            "error": f"eval invocation failed: {type(exc).__name__}: {exc}",
        }
    return holder["value"]


def _make_result_envelope(
    task: dict[str, Any],
    *,
    model: str,
    harness_name: str,
    seed: int,
    success: bool,
    steps: int,
    duration_sec: float,
    finish_reason: str = "assistant_final",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    error_count: int = 0,
    recovery_count: int = 0,
) -> dict[str, Any]:
    return {
        "result": {
            "model": model,
            "harness": harness_name,
            "task_id": task.get("id", "?"),
            "category": task.get("category", "?"),
            "difficulty": task.get("difficulty", "?"),
            "seed": seed,
            "success": success,
            "steps": steps,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens
            if total_tokens is not None
            else (input_tokens + output_tokens),
            "duration_sec": round(duration_sec, 3),
            "finish_reason": finish_reason,
            "error_count": error_count,
            "recovery_count": recovery_count,
        },
        "verdict": {"success": success, "details": {}},
        "errors": [],
        "recoveries": [],
        "messages": [],  # filled in by individual adapters
    }


def _run_subprocess(
    cmd: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    timeout: int = 300,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Claude Code (Anthropic, native Anthropic protocol via DeepSeek /anthropic)
# ---------------------------------------------------------------------------


class ClaudeCodeAdapter:
    """Run tasks through ~/agentweave/claude-code-exp/bin/claude-exp."""

    name = "claude-code"

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-flash",  # Claude Code uses ANTHROPIC_MODEL env (overrides this)
        max_turns: int = 20,
        timeout: int = 600,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.timeout = timeout
        self.wrapper = HOME / "agentweave" / "claude-code-exp" / "bin" / "claude-exp"

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        sandbox = _setup_sandbox(task)
        prompt = _build_prompt(task, library, sandbox=sandbox)
        run_id = run_id or f"claude_{uuid.uuid4().hex[:8]}"

        t0 = time.time()
        cmd = [
            str(self.wrapper),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        try:
            proc = _run_subprocess(cmd, timeout=self.timeout, cwd=str(sandbox))
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - t0
            partial_stdout, partial_stderr = _recover_timeout_output(e)
            envelope = self._parse_stream_json(partial_stdout, partial_stderr)
            envelope["result"].update(
                {
                    "model": self.model,
                    "harness": self.name,
                    "task_id": task.get("id", "?"),
                    "category": task.get("category", "?"),
                    "difficulty": task.get("difficulty", "?"),
                    "seed": seed,
                    "duration_sec": round(elapsed, 3),
                    "finish_reason": "timeout",
                }
            )
            success, failures, docker_verdict = _verify_docker_or_state(
                task, sandbox, envelope
            )
            envelope["result"]["success"] = success
            envelope["verdict"]["success"] = success
            envelope["verdict"]["details"] = {"failures": failures}
            if docker_verdict is not None:
                envelope["verdict"]["docker"] = docker_verdict
            _cleanup_sandbox(sandbox)
            return envelope

        elapsed = time.time() - t0
        envelope = self._parse_stream_json(proc.stdout, proc.stderr)
        envelope["result"].update(
            {
                "model": self.model,
                "harness": self.name,
                "task_id": task.get("id", "?"),
                "category": task.get("category", "?"),
                "difficulty": task.get("difficulty", "?"),
                "seed": seed,
                "duration_sec": round(elapsed, 3),
            }
        )

        # Real-state verification (docker pytest for SWE-Skills, state match for pilot)
        success, failures, docker_verdict = _verify_docker_or_state(task, sandbox, envelope)
        envelope["result"]["success"] = success
        envelope["verdict"]["success"] = success
        envelope["verdict"]["details"] = {"failures": failures}
        if docker_verdict is not None:
            envelope["verdict"]["docker"] = docker_verdict
        _cleanup_sandbox(sandbox)
        return envelope

    @staticmethod
    def _parse_stream_json(stdout: str, stderr: str) -> dict[str, Any]:
        """Parse Claude Code --output-format stream-json output.

        Each line is a JSON object describing an event (system, message, tool_use,
        tool_result, assistant_final). We flatten these into OpenAI-style messages.
        """
        messages: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        steps = 0
        finish_reason = "assistant_final"

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "user":
                content = event.get("message", {}).get("content")
                if isinstance(content, list):
                    text = "".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = str(content or "")
                messages.append({"role": "user", "content": text})
            elif etype == "assistant":
                msg = event.get("message", {})
                content = msg.get("content")
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append(
                                {
                                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", "?"),
                                        "arguments": json.dumps(
                                            block.get("input", {}), ensure_ascii=False
                                        ),
                                    },
                                }
                            )
                else:
                    text_parts.append(str(content or ""))
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(text_parts),
                }
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                messages.append(entry)
                steps += 1
                usage = msg.get("usage") or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
            elif etype == "tool_result":
                content = event.get("content")
                if isinstance(content, list):
                    text = "".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = str(content or "")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": event.get("tool_use_id", ""),
                        "content": text,
                    }
                )
            elif etype == "result":
                finish_reason = event.get("subtype", finish_reason)

        return _make_result_envelope(
            {"id": "?", "category": "?", "difficulty": "?"},
            model="deepseek-v4-flash",
            harness_name="claude-code",
            seed=0,
            success=False,
            steps=steps,
            duration_sec=0,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ) | {"messages": messages}


# ---------------------------------------------------------------------------
# Claude Pro: same Claude Code binary but pointed at DeepSeek-V4-Pro (stronger
# backend). Used to test how much of the low pass rate is model-limitation vs
# harness-limitation. Isolated under ~/agentweave/claude-pro-exp/ so the
# original claude-code experiment (deepseek-v4-flash) stays untouched.
# ---------------------------------------------------------------------------


class ClaudeProAdapter(ClaudeCodeAdapter):
    """Same as ClaudeCodeAdapter but routed through deepseek-v4-pro."""

    name = "claude-pro"

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-pro",
        max_turns: int = 20,
        timeout: int = 600,
    ) -> None:
        super().__init__(model=model, max_turns=max_turns, timeout=timeout)
        self.wrapper = HOME / "agentweave" / "claude-pro-exp" / "bin" / "claude-pro"


# ---------------------------------------------------------------------------
# Codex (OpenAI Codex CLI v0.45, chat wire_api → DeepSeek /v1/chat/completions)
# ---------------------------------------------------------------------------


class CodexAdapter:
    """Run tasks through ~/agentweave/codex-exp/bin/codex-exp."""

    name = "codex"

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-flash",
        max_turns: int = 20,
        timeout: int = 600,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.timeout = timeout
        self.wrapper = HOME / "agentweave" / "codex-exp" / "bin" / "codex-exp"

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        sandbox = _setup_sandbox(task)
        prompt = _build_prompt(task, library, sandbox=sandbox)
        t0 = time.time()
        cmd = [
            str(self.wrapper),
            "exec",
            "--skip-git-repo-check",
            "-s",
            "workspace-write",  # 允许 agent 在 sandbox dir 写文件（不是 default read-only）
            prompt,
        ]
        try:
            proc = _run_subprocess(cmd, timeout=self.timeout, cwd=str(sandbox))
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - t0
            partial_stdout, partial_stderr = _recover_timeout_output(e)
            combined = partial_stdout + "\n" + partial_stderr
            _, steps, messages = self._parse_codex_output(
                combined, partial_stderr, prompt
            )
            envelope = _make_result_envelope(
                task,
                model=self.model,
                harness_name=self.name,
                seed=seed,
                success=False,
                steps=steps,
                duration_sec=elapsed,
                finish_reason="timeout",
            )
            envelope["messages"] = messages
            success, failures, docker_verdict = _verify_docker_or_state(
                task, sandbox, envelope
            )
            envelope["result"]["success"] = success
            envelope["verdict"]["success"] = success
            envelope["verdict"]["details"] = {"failures": failures}
            if docker_verdict is not None:
                envelope["verdict"]["docker"] = docker_verdict
            _cleanup_sandbox(sandbox)
            return envelope
        elapsed = time.time() - t0
        # Codex writes conversation to stderr; combine for parsing
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        _, steps, messages = self._parse_codex_output(combined, proc.stderr, prompt)

        # Pre-fill envelope so eval can attach final_state.files
        envelope = _make_result_envelope(
            task,
            model=self.model,
            harness_name=self.name,
            seed=seed,
            success=False,
            steps=steps,
            duration_sec=elapsed,
            finish_reason="step_limit",
        )
        envelope["messages"] = messages
        success, failures, docker_verdict = _verify_docker_or_state(task, sandbox, envelope)
        envelope["result"]["success"] = success
        envelope["result"]["finish_reason"] = "assistant_final" if success else "step_limit"
        envelope["verdict"]["success"] = success
        envelope["verdict"]["details"] = {"failures": failures}
        if docker_verdict is not None:
            envelope["verdict"]["docker"] = docker_verdict
        _cleanup_sandbox(sandbox)
        return envelope

    @staticmethod
    def _parse_codex_output(
        stdout: str,
        stderr: str,
        prompt: str,
    ) -> tuple[bool, int, list[dict[str, Any]]]:
        """Parse Codex `exec` plain-text output.

        Codex 0.45 prints conversation as text blocks:
            user
            <text>

            codex
            <text>

            exec
            <command>

        We extract assistant turns + tool calls (exec blocks) into OpenAI schema.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        steps = 0
        success = False

        # Simple state machine: walk lines, group blocks by header.
        current: dict[str, Any] | None = None
        for raw in stdout.splitlines():
            line = raw.rstrip()
            if line in {"user", "codex", "exec", "thinking"}:
                if current:
                    messages.append(current)
                    current = None
                if line == "codex":
                    current = {"role": "assistant", "content": ""}
                    steps += 1
                elif line == "exec":
                    current = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {"name": "shell", "arguments": ""},
                            }
                        ],
                    }
                    steps += 1
                else:
                    current = None
                continue
            if current is None:
                continue
            if "tool_calls" in current:
                current["tool_calls"][0]["function"]["arguments"] += (line + "\n")
            else:
                current["content"] += (line + "\n")

        if current:
            messages.append(current)

        # Success = last assistant content contains TASK_COMPLETE
        last_asst = next(
            (m for m in reversed(messages) if m.get("role") == "assistant"), None
        )
        if last_asst:
            content = last_asst.get("content") or ""
            success = "TASK_COMPLETE" in content
        return success, steps, messages


# ---------------------------------------------------------------------------
# Hermes (Nous Research, openrouter overlay → DeepSeek /v1/chat/completions)
# ---------------------------------------------------------------------------


class HermesAdapter:
    """Run tasks through ~/agentweave/hermes-exp/bin/hermes-exp.

    Per-task isolation: a fresh ephemeral ``HERMES_HOME`` is rsync'd from the
    stock template (``~/agentweave/hermes-exp/home-template/``) for every run.
    This prevents Hermes's cross-session memory / FTS5 session search / auto
    skill creation from leaking from one task to the next.
    """

    name = "hermes"

    SESSION_RE = re.compile(r"session_id:\s*(\S+)")
    TEMPLATE_HOME = HOME / "agentweave" / "hermes-exp" / "home-template"

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-flash",
        provider: str = "openrouter",
        max_turns: int = 20,
        timeout: int = 600,
    ) -> None:
        self.model = model
        self.provider = provider
        self.max_turns = max_turns
        self.timeout = timeout
        self.wrapper = HOME / "agentweave" / "hermes-exp" / "bin" / "hermes-exp"

    def _make_ephemeral_home(self) -> Path:
        """Materialize a fresh Hermes home from the stock template."""
        tmp_home = Path(tempfile.mkdtemp(prefix="hermes-task-"))
        if self.TEMPLATE_HOME.exists():
            # Mirror template into tmp_home
            for src in self.TEMPLATE_HOME.rglob("*"):
                rel = src.relative_to(self.TEMPLATE_HOME)
                dst = tmp_home / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        return tmp_home

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        sandbox = _setup_sandbox(task)
        hermes_home = self._make_ephemeral_home()
        prompt = _build_prompt(task, library, sandbox=sandbox)
        t0 = time.time()
        cmd = [
            str(self.wrapper),
            "chat",
            "-q",
            prompt,
            "-Q",
            "-m",
            self.model,
            "--provider",
            self.provider,
            "--max-turns",
            str(self.max_turns),
            "--yolo",
            "--pass-session-id",
        ]
        env_extra = {"HERMES_HOME": str(hermes_home)}
        try:
            proc = _run_subprocess(
                cmd, timeout=self.timeout, cwd=str(sandbox), env_extra=env_extra
            )
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - t0
            partial_stdout, partial_stderr = _recover_timeout_output(e)
            match = self.SESSION_RE.search(partial_stdout) or self.SESSION_RE.search(
                partial_stderr
            )
            session_id = match.group(1) if match else None
            session_data = (
                self._load_session(hermes_home, session_id) if session_id else None
            )
            if session_data:
                messages = session_data.get("messages", [])
                steps = sum(1 for m in messages if m.get("role") == "assistant")
            else:
                messages = [{"role": "user", "content": prompt}]
                steps = 0
            envelope = _make_result_envelope(
                task,
                model=self.model,
                harness_name=self.name,
                seed=seed,
                success=False,
                steps=steps,
                duration_sec=elapsed,
                finish_reason="timeout",
            )
            envelope["messages"] = messages
            success, failures, docker_verdict = _verify_docker_or_state(
                task, sandbox, envelope
            )
            envelope["result"]["success"] = success
            envelope["verdict"]["success"] = success
            envelope["verdict"]["details"] = {"failures": failures}
            if docker_verdict is not None:
                envelope["verdict"]["docker"] = docker_verdict
            if session_id:
                envelope["result"]["hermes_session_id"] = session_id
            _cleanup_sandbox(sandbox)
            shutil.rmtree(hermes_home, ignore_errors=True)
            return envelope
        elapsed = time.time() - t0

        # Find session id and load the per-session JSON (already OpenAI-style)
        match = self.SESSION_RE.search(proc.stdout) or self.SESSION_RE.search(
            proc.stderr
        )
        session_id = match.group(1) if match else None
        session_data = self._load_session(hermes_home, session_id) if session_id else None

        if session_data:
            messages = session_data.get("messages", [])
            steps = sum(1 for m in messages if m.get("role") == "assistant")
        else:
            messages = [{"role": "user", "content": prompt}]
            steps = 0

        envelope = _make_result_envelope(
            task,
            model=self.model,
            harness_name=self.name,
            seed=seed,
            success=False,
            steps=steps,
            duration_sec=elapsed,
            finish_reason="step_limit",
        )
        envelope["messages"] = messages
        success, failures, docker_verdict = _verify_docker_or_state(task, sandbox, envelope)
        envelope["result"]["success"] = success
        envelope["result"]["finish_reason"] = "assistant_final" if success else "step_limit"
        envelope["verdict"]["success"] = success
        envelope["verdict"]["details"] = {"failures": failures}
        if docker_verdict is not None:
            envelope["verdict"]["docker"] = docker_verdict
        if session_id:
            envelope["result"]["hermes_session_id"] = session_id
        _cleanup_sandbox(sandbox)
        shutil.rmtree(hermes_home, ignore_errors=True)
        return envelope

    @staticmethod
    def _load_session(home: Path, session_id: str) -> dict[str, Any] | None:
        sessions_dir = home / "sessions"
        for candidate in [
            sessions_dir / f"session_{session_id}.json",
            sessions_dir / f"{session_id}.json",
        ]:
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None
        return None


# ---------------------------------------------------------------------------
# OpenClaw (wraps ~/agentweave/harness-exp/bin/openclaw-exp)
#
# OpenClaw has no clean single-turn agent mode without a running gateway,
# so this adapter uses `capability model run` to drive a flat tool-less
# single-prompt completion. That's not a real agent loop — for RQ2.1 we
# treat OpenClaw as a "single-shot LLM" harness baseline. If we later need
# real OpenClaw agent behaviour, switch to `openclaw agent` + Gateway.
# ---------------------------------------------------------------------------


class OpenClawAdapter:
    """Run tasks through ~/agentweave/harness-exp/bin/openclaw-exp via `agent --local`.

    Uses OpenClaw's embedded agent runtime which has real filesystem tools.
    Per-task workspace is set by patching ``agents.defaults.workspace`` in the
    isolated profile config right before each run. Because config is a global
    file, OpenClaw runs must be **serialized** (runner concurrency=1).
    """

    name = "openclaw"

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-flash",
        timeout: int = 600,
        thinking: str = "off",
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.thinking = thinking
        self.wrapper = HOME / "agentweave" / "harness-exp" / "bin" / "openclaw-exp"

    def _patch_workspace(self, sandbox: Path) -> None:
        """Set agents.defaults.workspace to the sandbox dir via openclaw config."""
        patch_doc = json.dumps(
            {"agents": {"defaults": {"workspace": str(sandbox)}}}
        )
        subprocess.run(
            [str(self.wrapper), "config", "patch", "--stdin"],
            input=patch_doc,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        sandbox = _setup_sandbox(task)
        # OpenClaw's agent operates in agents.defaults.workspace; point it at sandbox.
        try:
            self._patch_workspace(sandbox)
        except Exception:
            pass
        prompt = _build_prompt(task, library, sandbox=sandbox)
        session_id = f"agentweave-{task.get('id', 'task')}-{seed}-{uuid.uuid4().hex[:6]}"

        t0 = time.time()
        cmd = [
            str(self.wrapper),
            "agent",
            "--local",
            "--json",
            "--session-id",
            session_id,
            "--thinking",
            self.thinking,
            "--model",
            f"deepseek/{self.model}",
            "--message",
            prompt,
        ]
        try:
            proc = _run_subprocess(cmd, timeout=self.timeout, cwd=str(sandbox))
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - t0
            # OpenClaw session is keyed by the pre-generated session_id (above),
            # so partial trajectories are recoverable from the session file even
            # when the subprocess timed out.
            steps, messages, tokens_in, tokens_out = self._load_session_messages(
                session_id, prompt
            )
            envelope = _make_result_envelope(
                task,
                model=self.model,
                harness_name=self.name,
                seed=seed,
                success=False,
                steps=steps,
                duration_sec=elapsed,
                finish_reason="timeout",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
            envelope["messages"] = messages
            success, failures, docker_verdict = _verify_docker_or_state(
                task, sandbox, envelope
            )
            envelope["result"]["success"] = success
            envelope["verdict"]["success"] = success
            envelope["verdict"]["details"] = {"failures": failures}
            if docker_verdict is not None:
                envelope["verdict"]["docker"] = docker_verdict
            envelope["result"]["openclaw_session_id"] = session_id
            _cleanup_sandbox(sandbox)
            return envelope
        elapsed = time.time() - t0

        # Parse OpenClaw agent --json output
        result_json = self._extract_agent_json(proc.stdout)
        steps, messages, tokens_in, tokens_out = self._load_session_messages(session_id, prompt)

        envelope = _make_result_envelope(
            task,
            model=self.model,
            harness_name=self.name,
            seed=seed,
            success=False,
            steps=steps,
            duration_sec=elapsed,
            finish_reason="step_limit",
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
        envelope["messages"] = messages
        success, failures, docker_verdict = _verify_docker_or_state(task, sandbox, envelope)
        envelope["result"]["success"] = success
        envelope["result"]["finish_reason"] = "assistant_final" if success else "step_limit"
        envelope["verdict"]["success"] = success
        envelope["verdict"]["details"] = {"failures": failures}
        if docker_verdict is not None:
            envelope["verdict"]["docker"] = docker_verdict
        envelope["result"]["openclaw_session_id"] = session_id
        _cleanup_sandbox(sandbox)
        return envelope

    @staticmethod
    def _extract_agent_json(stdout: str) -> dict[str, Any] | None:
        """OpenClaw agent --json may emit warnings before the JSON. Find first '{'."""
        start = stdout.find("{")
        if start < 0:
            return None
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _load_session_messages(
        session_id: str, prompt: str
    ) -> tuple[int, list[dict[str, Any]], int, int]:
        """Read the agent's per-session JSONL and convert to OpenAI-schema messages."""
        path = (
            HOME
            / ".openclaw-agentweave-exp"
            / "agents"
            / "main"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        messages: list[dict[str, Any]] = []
        steps = 0
        tokens_in = 0
        tokens_out = 0
        if not path.exists():
            return 0, [{"role": "user", "content": prompt}], 0, 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "message":
                continue
            msg = ev.get("message", {})
            role = msg.get("role")
            content = msg.get("content", [])
            if not isinstance(content, list):
                content = [{"type": "text", "text": str(content)}]
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text_parts.append(block.get("text", ""))
                elif bt == "toolCall":
                    tool_calls.append(
                        {
                            "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type": "function",
                            "function": {
                                "name": block.get("name", "?"),
                                "arguments": json.dumps(
                                    block.get("arguments", {}), ensure_ascii=False
                                ),
                            },
                        }
                    )
                elif bt == "toolResult":
                    txt = block.get("text") or json.dumps(block, ensure_ascii=False)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("toolCallId", ""),
                            "content": txt,
                        }
                    )
            if role == "assistant":
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(text_parts),
                }
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                messages.append(entry)
                steps += 1
            elif role == "user":
                messages.append({"role": "user", "content": "".join(text_parts)})
            usage = msg.get("usage") or {}
            tokens_in += usage.get("input", 0) or 0
            tokens_out += usage.get("output", 0) or 0
        if not messages:
            messages = [{"role": "user", "content": prompt}]
        return steps, messages, tokens_in, tokens_out
