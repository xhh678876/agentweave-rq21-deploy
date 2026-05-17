# AGENTS.md — Universal Agent Instructions

See [CLAUDE.md](./CLAUDE.md) for the full deployment runbook. This file is for agents that prefer AGENTS.md as the convention (Codex, OpenClaw, Hermes, etc).

## TL;DR

You're on a Linux server. Run the AgentWeave RQ2.1 experiment:

```bash
bash scripts/setup.sh                            # 1. OS deps
newgrp docker
bash scripts/install.sh                          # 2. Agent components
git clone https://github.com/hermes-org/hermes.git hermes-exp/upstream  # 3. Hermes upstream
hermes-exp/.venv/bin/pip install -e hermes-exp/upstream
bash scripts/pull_images.sh                      # 4. Docker images (24 GB)
cat > ~/.deepseek_keys << EOF                    # 5. API keys (ask user)
sk-key1
sk-key2
EOF
sed -i "s|sk-REPLACE_ME_DEEPSEEK_KEY|$(head -1 ~/.deepseek_keys)|g" \
  claude-code-exp/config/.env claude-pro-exp/config/.env
nohup python3 scripts/multi_key_proxy.py &       # 6. Proxy
bash scripts/run_full_experiment.sh              # 7. Run (6-8 h)
bash scripts/monitor.sh                          # 8. Watch progress
```

Verify each step before moving on. Full details in CLAUDE.md.

## Hard rules

1. Never push data back to GitHub (`runs/` is gitignored anyway).
2. Don't edit `cli_harnesses.py` patches.
3. If a phase fails twice, stop and ask the user — don't keep retrying.
4. Always re-check `pgrep -f multi_key_proxy.py` is alive before starting runners.

## Failure escalation

If something breaks:
1. Capture: `ps auxf | grep -E "agentweave|claude|codex|hermes|openclaw"`
2. Capture: `tail -50 /tmp/*.log`
3. Capture: `docker ps -a | head`
4. Summarize to user with exact symptom + commands you ran.

## Done = all 30 cells have _summary.json

```bash
find runs/rq2_1 -name "_summary.json" | wc -l   # expect 30
```
