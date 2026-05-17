"""Installer: clone upstream + pull Docker images + verify.

Used once after ``git clone`` of this bridge repo. ``swe-skills-bridge install``
will:

1. Clone https://github.com/GeniusHTX/SWE-Skills-Bench into ``upstream/`` if
   absent (or refresh ``git pull`` if present and not skipped).
2. Pre-pull every Docker base image referenced by the upstream config so
   later eval runs don't pay first-touch latency.
3. Build the per-task JSON files under ``tasks/`` (via
   :mod:`swe_skills_bridge.task_adapter`).

Re-running is safe: clone is skipped when ``upstream/`` already exists.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .skill_loader import project_root
from .task_adapter import build_all_tasks, load_upstream_skills


logger = logging.getLogger(__name__)


UPSTREAM_GIT_URL = "https://github.com/GeniusHTX/SWE-Skills-Bench.git"


@dataclass
class InstallReport:
    cloned: bool
    pulled_images: list[str]
    failed_images: list[tuple[str, str]]
    tasks_written: int
    tasks_skipped: int

    def to_dict(self) -> dict:
        return {
            "cloned": self.cloned,
            "pulled_images": self.pulled_images,
            "failed_images": self.failed_images,
            "tasks_written": self.tasks_written,
            "tasks_skipped": self.tasks_skipped,
        }


def install(
    *,
    root: Path | None = None,
    skip_clone: bool = False,
    skip_docker_pull: bool = False,
) -> InstallReport:
    base = root or project_root()
    upstream = base / "upstream"

    cloned = False
    if not skip_clone:
        if not upstream.exists():
            _git_clone(UPSTREAM_GIT_URL, upstream)
            cloned = True
        else:
            logger.info("upstream already exists at %s — skipping clone", upstream)

    pulled, failed = [], []
    if not skip_docker_pull:
        images = _collect_docker_images(base)
        pulled, failed = _pull_images(images)

    summary = build_all_tasks(root=base)
    return InstallReport(
        cloned=cloned,
        pulled_images=pulled,
        failed_images=failed,
        tasks_written=summary["total"],
        tasks_skipped=len(summary["skipped"]),
    )


def _git_clone(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("cloning %s -> %s", url, target)
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(target)],
        check=True,
    )


def _collect_docker_images(root: Path) -> list[str]:
    skills = load_upstream_skills(root)
    images: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        image = skill.base_image
        if image and image not in seen:
            seen.add(image)
            images.append(image)
    return images


def _pull_images(images: Iterable[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Best-effort Docker pull. Returns (pulled, failed)."""
    pulled: list[str] = []
    failed: list[tuple[str, str]] = []
    if shutil.which("docker") is None:
        return pulled, [(img, "docker CLI not on PATH") for img in images]
    for image in images:
        logger.info("docker pull %s", image)
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if result.returncode == 0:
                pulled.append(image)
            else:
                tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
                failed.append((image, "\n".join(tail)))
        except Exception as exc:  # noqa: BLE001
            failed.append((image, f"{type(exc).__name__}: {exc}"))
    return pulled, failed
