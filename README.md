# AgentWeave RQ2.1 Experiment Deployment

**SWE-Skills-Bench cross-harness experiment** — runs M0 baseline (and M4 with library injection) on 5 industry-grade agent CLIs to study cross-harness experience transfer.

## What's in this repo

```
runner/                # Experiment runner (含 patches: docker eval timeout + partial output)
swe-skills-bench/      # Benchmark: 49 skills × ~11 task instances ≈ 565 tasks
rq21_subset_tasks/     # 48-task subset we used (excludes pytorch task)
claude-code-exp/       # Claude Code on DeepSeek V4 Flash
claude-pro-exp/        # Claude Code on DeepSeek V4 Pro (model strength control)
codex-exp/             # OpenAI Codex 0.45 with multi-key proxy
hermes-exp/            # Hermes Agent (need to clone upstream separately, see below)
openclaw-exp/          # OpenClaw
scripts/
  setup.sh              # OS env (docker, node, python)
  install.sh            # All 5 agent binaries (npm/pip)
  pull_images.sh        # 8 SWE-Skills docker images (24 GB)
  multi_key_proxy.py    # DeepSeek API multi-key round-robin proxy
  run_full_experiment.sh
  run_quick.sh
  monitor.sh
```

## Quick Start (Server Setup)

**Target: 55-core / 500GB RAM Linux server (Ubuntu 22.04/24.04)**

```bash
# 1. Clone (use https or ssh)
git clone https://github.com/xhh678876/agentweave-rq21-deploy.git agentweave_deploy
cd agentweave_deploy

# 2. Install OS dependencies (docker, node 20, python 3.13)
bash scripts/setup.sh
newgrp docker  # or re-login

# 3. Install agent components
bash scripts/install.sh

# 4. Clone hermes upstream separately (excluded from this repo, see below)
git clone https://github.com/hermes-org/hermes.git hermes-exp/upstream
hermes-exp/.venv/bin/pip install -e hermes-exp/upstream

# 5. Pull docker images (24 GB)
bash scripts/pull_images.sh

# 6. Configure DeepSeek API keys
cat > ~/.deepseek_keys << EOK
sk-your-key-1
sk-your-key-2
sk-your-key-3
sk-your-key-4
EOK

# 7. Fill in .env files (replace sk-REPLACE_ME)
sed -i 's/sk-REPLACE_ME_DEEPSEEK_KEY/sk-your-actual-key/g' \
  claude-code-exp/config/.env \
  claude-pro-exp/config/.env
```

## Run Experiments

```bash
# Quick validation (1-2 hours, 240 trajectories)
bash scripts/run_quick.sh

# Full experiment (6-8 hours, 1440 trajectories)
# 5 harness × 3 seed × 48 task × (M0 + M4)
bash scripts/run_full_experiment.sh

# Monitor progress
bash scripts/monitor.sh

# View tmux sessions
tmux list-sessions
tmux attach -t rq21_claude-code_M0
```

## Why hermes-exp/upstream is excluded

The Hermes Agent upstream codebase (74 MB, 4500+ files) is the canonical Hermes project. To keep this repo lean and avoid duplicating a third-party project:

- **You should clone it from the official source** during setup
- Step 4 above shows the exact command
- Without it, `hermes` harness won't work, but the other 4 harnesses will

## Patches applied (already in cli_harnesses.py)

1. **`_recover_timeout_output()`** — When subprocess times out, decode partial stdout/stderr (was lost before, agent's real work discarded)
2. **`_run_swe_skills_eval_with_timeout()`** — Wall-clock bounded docker eval (prevents deadlock from hung docker SDK socket recv)
3. **Per-task budget** — Each task gets `eval_spec.timeout + 30s` (was hardcoded 180s, too aggressive)

## DeepSeek API setup

The 5 harnesses use DeepSeek differently:
- **claude-code, claude-pro**: native Anthropic protocol via `https://api.deepseek.com/anthropic`
- **codex**: OpenAI-compat protocol via local proxy `127.0.0.1:8765` (proxy injects `thinking: disabled`)
- **hermes, openclaw**: each has their own LLM client config

Get DeepSeek keys: https://platform.deepseek.com

**4+ keys recommended** for parallel use to avoid rate limits (60 RPM per key).

## Expected results

On 55-core/500GB server with 4 API keys:

| Range | ETA |
|---|---|
| Quick (240 traj) | ~1-2 h |
| Full (1440 traj) | ~6-8 h |

Pass rate on the 48-task subset (M0 baseline):
- Mean pass rate ~10% (sandbox doesn't include repo source, agent writes "blind")
- pass@1 (strict, all tests pass): ~2-5%

To boost to paper-level pass rate (~90%), apply the `sandbox_with_repo.patch` (see ROADMAP.md — not yet included).

## Data outputs

```
runs/rq2_1/<harness>/M0/seed_<S>/swe_skills_<task>.json
runs/rq2_1/<harness>/M4/seed_<S>/swe_skills_<task>.json
```

Each trajectory JSON contains:
- `result`: steps, tokens, duration, finish_reason, success
- `verdict.docker`: pass_rate, exit_code, stdout_tail (pytest output)
- `messages[]`: full agent conversation
- `final_state.files`: files agent wrote

## License

Research code, for SWE-Skills-Bench experiments. See LICENSE in upstream projects for their licenses.
