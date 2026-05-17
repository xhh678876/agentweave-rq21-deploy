#!/usr/bin/env bash
# 实时显示 5 harness × cells 进度
set -uo pipefail

cd $(dirname $0)/..
ROOT=$(pwd)
OUT=$ROOT/runs

while true; do
  clear
  echo "═══ $(date '+%H:%M:%S') AgentWeave 实时进度 ═══"
  echo
  echo "─── 进程 ───"
  pgrep -fl "run-method\|agentweave_runner.cli" | head -10
  echo
  echo "─── 进度 ───"
  python3 << 'PY'
import json, os
from pathlib import Path
root = Path(os.getcwd()) / 'runs'
total = 0
target = 0
for run_type in ['rq2_1', 'quick']:
    base = root / run_type
    if not base.exists(): continue
    print(f"\n  {run_type}:")
    for h in ['claude-code', 'claude-pro', 'codex', 'hermes', 'openclaw']:
        h_total = 0
        for m in ['M0', 'M4']:
            for s in range(3):
                cell = base / h / m / f'seed_{s}'
                if not cell.exists(): continue
                trajs = [f for f in cell.glob('swe_skills_*.json') if '_library_snapshots' not in f.parts]
                h_total += len(trajs)
                target += 48
        if h_total:
            total += h_total
            print(f"    {h:<14} {h_total:>3}")
print(f"\n  总: {total}/{target}")
PY
  echo
  echo "─── Mac / Server 资源 ───"
  uptime
  echo
  echo "─── 5 sec 刷新 ───"
  sleep 5
done
