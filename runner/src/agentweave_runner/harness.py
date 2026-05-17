"""Harness adapters.

Two adapters are defined:

    LangGraphAdapter  — wraps ~/agentweave/harness-exp-langgraph (working)
    OpenClawAdapter   — wraps ~/agentweave/harness-exp           (stub for now)

Both conform to the same Protocol::

    class Harness(Protocol):
        def run_task(self, task: dict, library: dict | None) -> dict: ...

The LangGraph adapter pulls in the project venv via ``sys.path`` injection so
the runner does not need to re-pin langgraph/langchain. We rely on the fact
that the user already installed the harness in editable mode inside
``harness-exp-langgraph/.venv``. The runner is expected to be invoked from
THAT venv (or any venv where ``langgraph_harness`` is importable).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Protocol


DEFAULT_MODEL = os.environ.get("AGENTWEAVE_RUNNER_MODEL", "deepseek-v4-flash")
DEFAULT_MAX_STEPS = int(os.environ.get("AGENTWEAVE_RUNNER_MAX_STEPS", "20"))


class Harness(Protocol):
    name: str

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# LangGraph adapter
# ---------------------------------------------------------------------------


def _ensure_langgraph_importable() -> None:
    """Add the langgraph harness source dir to sys.path if not importable."""
    try:
        import langgraph_harness  # noqa: F401
        return
    except ImportError:
        pass

    # Fall back to path injection against the known sibling project layout.
    candidate = Path.home() / "agentweave" / "harness-exp-langgraph" / "src"
    if candidate.exists():
        sys.path.insert(0, str(candidate))


class LangGraphAdapter:
    """Run tasks through the LangGraph harness (Harness B)."""

    name = "langgraph"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.model = model
        self.max_steps = max_steps
        _ensure_langgraph_importable()

    def run_task(
        self,
        task: dict[str, Any],
        library: dict[str, Any] | None,
        *,
        seed: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        from langgraph_harness.adapter import run_task as _lg_run_task

        return _lg_run_task(
            task,
            library=library,
            model=self.model,
            seed=seed,
            max_steps=self.max_steps,
            run_id=run_id,
            raw_path="",
        )


# ---------------------------------------------------------------------------
# CLI-based adapters (RQ2.1 cross-harness: real industry CLIs all using DeepSeek)
# ---------------------------------------------------------------------------

from .cli_harnesses import (
    ClaudeCodeAdapter,
    ClaudeProAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
)


HARNESS_REGISTRY: dict[str, type] = {
    "langgraph": LangGraphAdapter,
    "openclaw": OpenClawAdapter,
    "claude-code": ClaudeCodeAdapter,
    "claude-pro": ClaudeProAdapter,
    "codex": CodexAdapter,
    "hermes": HermesAdapter,
}


def get_harness(name: str, **kwargs: Any) -> Harness:
    key = name.lower()
    if key not in HARNESS_REGISTRY:
        raise ValueError(
            f"Unknown harness '{name}'. Available: {sorted(HARNESS_REGISTRY)}"
        )
    return HARNESS_REGISTRY[key](**kwargs)
