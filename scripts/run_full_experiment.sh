#!/usr/bin/env bash
# 跑全实验: 5 harness × 3 seed × 48 task × 2 method (M0 + M4)
# = 720 trajectory M0 + 720 trajectory M4 = 1440 trajectory
# 预计 6-8 小时 (4-key proxy + 55 核)
set -uo pipefail

cd $(dirname $0)/..
ROOT=$(pwd)

# 启 proxy (后台)
if ! pgrep -f multi_key_proxy.py > /dev/null; then
  nohup python3 $ROOT/scripts/multi_key_proxy.py > /tmp/proxy.log 2>&1 &
  echo "proxy 启动 PID=$!"
  sleep 3
fi

TASKS=$ROOT/rq21_subset_tasks
OUT_ROOT=$ROOT/runs/rq2_1

source ~/.venv/bin/activate

# 各 harness 并发度
declare -A CONC=([claude-code]=6 [claude-pro]=6 [codex]=6 [hermes]=4 [openclaw]=4)

# 同时启动所有 harness × seed (并行 in tmux sessions)
for HARNESS in claude-code claude-pro codex hermes openclaw; do
  C=${CONC[$HARNESS]}
  for METHOD in M0 M4; do
    SESSION="rq21_${HARNESS}_${METHOD}"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new-session -d -s "$SESSION" "
      for SEED in 0 1 2; do
        echo \"\$(date) ▶ $HARNESS/$METHOD/seed_\$SEED\"
        $ROOT/runner/bin/run-method \
          --method $METHOD --seed \$SEED \
          --tasks $TASKS \
          --harness $HARNESS \
          --concurrency $C \
          --out $OUT_ROOT/$HARNESS/$METHOD/seed_\$SEED \
          --resume
      done
      echo \"\$(date) ━━ $HARNESS/$METHOD DONE ━━\"
    "
  done
done

echo
echo "全 ${#CONC[@]}×2 = $((${#CONC[@]}*2)) 个 tmux session 启动了"
echo
echo "查看:"
echo "  tmux list-sessions"
echo "  tmux attach -t rq21_<harness>_<method>"
echo
echo "监控:"
echo "  bash $ROOT/scripts/monitor.sh"
