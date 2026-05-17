#!/usr/bin/env bash
# 快速验证: 5 harness × 1 seed × 48 task × M0 only
# = 240 trajectory，预计 1-2 小时
set -uo pipefail

cd $(dirname $0)/..
ROOT=$(pwd)

if ! pgrep -f multi_key_proxy.py > /dev/null; then
  nohup python3 $ROOT/scripts/multi_key_proxy.py > /tmp/proxy.log 2>&1 &
  sleep 3
fi

TASKS=$ROOT/rq21_subset_tasks
OUT_ROOT=$ROOT/runs/quick

source ~/.venv/bin/activate

declare -A CONC=([claude-code]=6 [claude-pro]=6 [codex]=6 [hermes]=4 [openclaw]=4)

for HARNESS in claude-code claude-pro codex hermes openclaw; do
  C=${CONC[$HARNESS]}
  tmux kill-session -t "quick_$HARNESS" 2>/dev/null || true
  tmux new-session -d -s "quick_$HARNESS" "
    $ROOT/runner/bin/run-method \
      --method M0 --seed 0 \
      --tasks $TASKS \
      --harness $HARNESS \
      --concurrency $C \
      --out $OUT_ROOT/$HARNESS/M0/seed_0 \
      --resume
    echo done
  "
done

echo "5 tmux session 启动了 (quick_<harness>)"
echo "查看: tmux list-sessions"
