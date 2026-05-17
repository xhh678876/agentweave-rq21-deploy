"""SWE-Skills-Bench → AgentWeave bridge.

Converts SWE-Skills-Bench's 49 real-world software engineering tasks into the
OpenAI function-calling task JSON schema consumed by AgentWeave's existing
LangGraph harness, method runner (M0-M4), and pipeline (L1-L5).

Public entry points::

    from swe_skills_bridge.installer import install
    from swe_skills_bridge.task_adapter import build_all_tasks
    from swe_skills_bridge.eval_adapter import docker_evaluate_trajectory
    from swe_skills_bridge.skill_loader import load_human_skill

The CLI is exposed via ``swe-skills-bridge`` (see :mod:`swe_skills_bridge.cli`).
"""

from __future__ import annotations

__all__ = [
    "__version__",
]

__version__ = "0.1.0"
