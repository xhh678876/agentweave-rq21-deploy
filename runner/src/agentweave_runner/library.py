"""Library abstraction shared across M0-M4.

Each method only uses a subset of the fields, but we use one container class
so the runner machinery (snapshots, resume, summaries) is uniform.

Fields:
    do_entries        : list of {"text": str, ...} — used by M3, M4
    do_not_entries    : list of {"text": str, ...} — used by M3, M4
    workflows         : list of {"name": str, "steps": [tool_name, ...], ...}
                        — used by M1 (AWM)
    reasoning_chunks  : list of {"text": str, "task_id": str, ...}
                        — used by M2 (ReasoningBank)

The on-disk format is a single JSON blob so snapshots and resume are trivial.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Library:
    do_entries: list[dict[str, Any]] = field(default_factory=list)
    do_not_entries: list[dict[str, Any]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    reasoning_chunks: list[dict[str, Any]] = field(default_factory=list)
    # Metadata for traceability across snapshots
    meta: dict[str, Any] = field(default_factory=dict)

    def save(self, path: str | Path) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "Library":
        p = Path(path).expanduser()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            do_entries=list(data.get("do_entries") or []),
            do_not_entries=list(data.get("do_not_entries") or []),
            workflows=list(data.get("workflows") or []),
            reasoning_chunks=list(data.get("reasoning_chunks") or []),
            meta=dict(data.get("meta") or {}),
        )

    # --- Convenience accessors for inspection / tests ---

    @property
    def size(self) -> int:
        return (
            len(self.do_entries)
            + len(self.do_not_entries)
            + len(self.workflows)
            + len(self.reasoning_chunks)
        )

    def summary(self) -> dict[str, int]:
        return {
            "do": len(self.do_entries),
            "do_not": len(self.do_not_entries),
            "workflows": len(self.workflows),
            "reasoning_chunks": len(self.reasoning_chunks),
        }


def empty_library() -> Library:
    return Library()
