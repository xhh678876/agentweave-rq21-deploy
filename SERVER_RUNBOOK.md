# Server Runbook — Quick Reference

> 给值班人员（or AI agent）的极简操作清单。

## 0. 进服务器后第一件事

```bash
git clone https://github.com/xhh678876/agentweave-rq21-deploy.git agentweave_deploy
cd agentweave_deploy

# 如果在国内拉 GitHub 慢：
# git clone https://ghfast.com/https://github.com/xhh678876/agentweave-rq21-deploy.git agentweave_deploy

# 启动 AI agent (Claude Code) 接管
claude
# claude code 会自动读 CLAUDE.md，知道怎么做
```

## 1. 让 Claude Code 跑全流程

在 claude 里说：

> "按 CLAUDE.md 跑全流程。先做 phase 0 pre-flight，每个 phase 完成后告诉我状态再做下一个。"

Claude 会按 phase 0-7 一步步做，每步都验证。

## 2. 直接命令版（不用 AI）

```bash
# Phase 1: OS
bash scripts/setup.sh && newgrp docker

# Phase 2: Components
bash scripts/install.sh
git clone https://github.com/hermes-org/hermes.git hermes-exp/upstream
hermes-exp/.venv/bin/pip install -e hermes-exp/upstream

# Phase 3: Images
bash scripts/pull_images.sh

# Phase 4: Config
cat > ~/.deepseek_keys << EOF
sk-key1
sk-key2
sk-key3
sk-key4
EOF
PRIMARY=$(head -1 ~/.deepseek_keys)
sed -i "s|sk-REPLACE_ME_DEEPSEEK_KEY|${PRIMARY}|g" \
  claude-code-exp/config/.env claude-pro-exp/config/.env
nohup python3 scripts/multi_key_proxy.py > /tmp/proxy.log 2>&1 &

# Phase 5: Smoke
~/.venv/bin/python -m agentweave_runner.cli \
  --method M0 --seed 0 --tasks rq21_subset_tasks \
  --harness claude-code --concurrency 1 \
  --out /tmp/smoke --limit 1

# Phase 6: Full run
bash scripts/run_full_experiment.sh

# Phase 7: Monitor
bash scripts/monitor.sh
```

## 3. 运行中常用命令

```bash
# 看 tmux sessions
tmux list-sessions
tmux attach -t rq21_claude-code_M0    # 看具体 session

# 看哪些 cell 完成
find runs/rq2_1 -name "_summary.json" | wc -l   # 目标 30

# 看 proxy 健康
pgrep -f multi_key_proxy.py
curl -s http://127.0.0.1:8765/v1/models  # 应该 401 (need auth) 或 200

# 看 docker eval 卡死
docker ps | head
# 如果有大量 "Up X minutes" 容器 → docker daemon 可能卡了

# 紧急停止
pkill -f agentweave_runner.cli
pkill -f rq21_run_
tmux kill-server
```

## 4. 实验结束后

```bash
# 检查
find runs/rq2_1 -name "_summary.json" | wc -l   # 应该 = 30

# 压缩结果
tar -czf runs_rq2_1_$(date +%Y%m%d).tar.gz runs/rq2_1/

# 下载到本地
scp server:agentweave_deploy/runs_rq2_1_*.tar.gz ~/Downloads/
```

## 5. 救场命令

| 症状 | 命令 |
|---|---|
| proxy 死了 | `nohup python3 scripts/multi_key_proxy.py > /tmp/proxy.log 2>&1 &` |
| docker daemon 卡 | `sudo systemctl restart docker` |
| 某 harness session 死 | `bash scripts/run_full_experiment.sh` 会 resume |
| Mac 满 | 服务器有 500GB，不会满 |
| 想看实时输出 | `tmux attach -t rq21_<harness>_<method>` |

## 6. 联系方式

- 异常 → 截图 `tmux list-sessions` + `pgrep -fl runner` + 跑出的 `_summary.json` 数发给我
- 数据问题 → tar 压缩 `runs/rq2_1/` 发给我（或挂 cloud OSS）

Good hunting.
