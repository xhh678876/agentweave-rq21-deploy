"""Read upstream/skills/<skill_id>/SKILL.md → string.

Used by the runner's M_human method (a ceiling baseline) to inject the
benchmark's curated skill document instead of (or in addition to) AgentWeave's
auto-extracted library.

The actual injection logic stays in ``agentweave/runner/`` — this module just
exposes a small read API so the runner can be extended without depending on
the upstream layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final


_UPSTREAM_SKILLS_REL: Final = Path("upstream") / "skills"


def project_root() -> Path:
    """Return the swe-skills-bench project root (this repo, not upstream).

    The package lives at ``<root>/src/swe_skills_bridge/``; walk up two levels.
    """
    return Path(__file__).resolve().parents[2]


def skill_path(skill_id: str, *, root: Path | None = None) -> Path:
    """Return the absolute path to ``upstream/skills/<skill_id>/SKILL.md``."""
    base = root or project_root()
    return base / _UPSTREAM_SKILLS_REL / skill_id / "SKILL.md"


def load_human_skill(skill_id: str, *, root: Path | None = None) -> str:
    """Load the human-written SKILL.md for ``skill_id``.

    Raises
    ------
    FileNotFoundError
        If the skill file is absent. Callers (the runner's M_human method)
        should treat this as "no human skill available; fall back to M0".
    """
    path = skill_path(skill_id, root=root)
    if not path.exists():
        raise FileNotFoundError(f"human skill not found: {path}")
    return path.read_text(encoding="utf-8")


def list_skill_ids(*, root: Path | None = None) -> list[str]:
    """Enumerate every skill_id that has a SKILL.md under upstream/skills/."""
    base = (root or project_root()) / _UPSTREAM_SKILLS_REL
    if not base.exists():
        return []
    return sorted(
        entry.name
        for entry in base.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").exists()
    )
