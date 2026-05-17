"""Checkpoint + resume helpers.

Layout under ``--out`` directory::

    runs/.../
        task_001.json
        task_002.json
        ...
        _library_snapshots/
            after_000.json   (initial — empty library)
            after_010.json
            after_020.json
            ...
        _checkpoint.json     (resume cursor)
        _summary.json        (final aggregate metrics)

The checkpoint file is a small JSON document::

    {
        "method": "M4",
        "seed": 0,
        "harness": "langgraph",
        "completed_task_ids": ["task_01", "task_02", ...],
        "last_library_snapshot": "_library_snapshots/after_020.json",
        "timestamp": "2026-05-16T13:37:00Z"
    }

Resume protocol: on startup, if ``_checkpoint.json`` exists we load the
``last_library_snapshot`` as the starting library and skip any task whose id
appears in ``completed_task_ids``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHECKPOINT_NAME = "_checkpoint.json"
SNAPSHOT_DIR = "_library_snapshots"
SUMMARY_NAME = "_summary.json"


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkpoint_path(out_dir: Path) -> Path:
    return out_dir / CHECKPOINT_NAME


def snapshot_dir(out_dir: Path) -> Path:
    return out_dir / SNAPSHOT_DIR


def summary_path(out_dir: Path) -> Path:
    return out_dir / SUMMARY_NAME


def load_checkpoint(out_dir: Path) -> dict[str, Any] | None:
    p = checkpoint_path(out_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def write_checkpoint(out_dir: Path, payload: dict[str, Any]) -> None:
    p = checkpoint_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["timestamp"] = utc_iso_now()
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def snapshot_library_path(out_dir: Path, completed_count: int) -> Path:
    return snapshot_dir(out_dir) / f"after_{completed_count:04d}.json"


def list_snapshots(out_dir: Path) -> list[Path]:
    d = snapshot_dir(out_dir)
    if not d.exists():
        return []
    return sorted(d.glob("after_*.json"))
