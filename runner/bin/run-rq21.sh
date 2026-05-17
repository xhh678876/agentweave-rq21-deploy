#!/usr/bin/env bash
# RQ2.1 完整实验脚本
# 5 harness × 49 SWE-Skills × 2 methods × 3 seeds = 1470 runs
#
# 串行运行：保护 OpenClaw config + Mac 资源
# 每个 (harness, method, seed) 组合作为一个 batch

set -uo pipefail

TASKS=/tmp/rq21_subset/tasks/
OUT_ROOT=~/agentweave/runs/rq2_1
LOG=/tmp/rq21_main.log

mkdir -p "$OUT_ROOT" "$(dirname $LOG)"
echo "$(date)  ━━ RQ2.1 START ━━" | tee -a "$LOG"

# Harnesses in order: 4 non-OpenClaw first (faster), OpenClaw last (slowest, serial)
HARNESSES=("langgraph" "hermes" "claude-code" "codex" "openclaw")

for HARNESS in "${HARNESSES[@]}"; do
  CONCURRENCY=$([ "$HARNESS" = "openclaw" ] && echo 1 || echo 4)
  echo "$(date)  ── $HARNESS (concurrency=$CONCURRENCY) ──" | tee -a "$LOG"
  for METHOD in M0 M4; do
    for SEED in 0 1 2; do
      CELL="$HARNESS/$METHOD/seed_$SEED"
      OUT_DIR="$OUT_ROOT/$CELL"
      echo "$(date)  ▶ $CELL" | tee -a "$LOG"
      ~/agentweave/runner/bin/run-method \
        --method "$METHOD" --seed "$SEED" \
        --tasks "$TASKS" \
        --harness "$HARNESS" \
        --concurrency "$CONCURRENCY" \
        --out "$OUT_DIR" \
        --resume \
        >> "$LOG" 2>&1
      RC=$?
      if [ $RC -eq 0 ]; then
        echo "$(date)  ✅ $CELL done" | tee -a "$LOG"
      else
        echo "$(date)  ❌ $CELL FAILED (rc=$RC), continuing" | tee -a "$LOG"
      fi
    done
  done
done

echo "$(date)  ━━ RQ2.1 ALL DONE ━━" | tee -a "$LOG"
