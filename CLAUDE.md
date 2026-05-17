# Project: AgentWeave RQ2.1 Experiment Deployment

You are about to deploy and run the AgentWeave RQ2.1 cross-harness experiment on this Linux server.

## Your Mission

Run the SWE-Skills-Bench evaluation across **5 agent harnesses × 3 seeds × 48 tasks × 2 methods (M0/M4)** = 1440 trajectories. Target wall-clock: 6-8 hours on a 55-core / 500GB RAM server.

## Phase Map (do in order)

| Phase | What | How to know it's done |
|---|---|---|
| **0** | Verify hardware + network | `nproc` ≥ 32, can reach api.deepseek.com |
| **1** | OS setup | docker + node + python installed |
| **2** | Agent components | runner / harness binaries / hermes upstream |
| **3** | Docker images | 8 SWE-Skills images pulled |
| **4** | API keys + .env | proxy responds 200 on test request |
| **5** | Smoke test | `run_quick.sh` produces ≥1 trajectory |
| **6** | Full run | tmux sessions running 5 harness × 2 method |
| **7** | Monitor + collect | All cells produce `_summary.json` |

## Phase 0 — Pre-flight (≤2 min)

```bash
# Verify hardware
nproc                              # need ≥ 32
free -g                            # need ≥ 100 GB available
df -h ~                            # need ≥ 100 GB free

# Verify network to DeepSeek
curl -s -o /dev/null -w "%{http_code}\n" \
  https://api.deepseek.com/v1/models \
  -H "Authorization: Bearer test"
# Expect 401 (not 000/timeout) → network OK
```

**Fail fast** if any check fails. Tell the user and stop.

## Phase 1 — OS Setup (≤5 min)

```bash
bash scripts/setup.sh
newgrp docker   # or sudo su - $USER

# Verify
docker --version       # ≥ 20.x
node --version         # ≥ 20.x
python3 --version      # ≥ 3.10
```

## Phase 2 — Agent Components (≤15 min)

```bash
bash scripts/install.sh

# Clone hermes upstream (excluded from repo)
git clone https://github.com/hermes-org/hermes.git hermes-exp/upstream
hermes-exp/.venv/bin/pip install -e hermes-exp/upstream

# Verify each harness binary is runnable
~/.venv/bin/python -c "import agentweave_runner; print('runner OK')"
claude-code-exp/node_modules/.bin/claude --version  # any output OK
codex-exp/node_modules/.bin/codex --version
hermes-exp/.venv/bin/hermes --help | head -3
openclaw-exp/node_modules/.bin/openclaw --version || echo "ok"
```

**If hermes upstream clone fails** (404 or org renamed):
- Search: `gh search repos hermes-agent` to find current location
- Or use the `hermes.tar.gz` previously bundled with Mac (NOT in this repo)
- Or skip hermes harness — comment it out in `run_full_experiment.sh`

## Phase 3 — Docker Images (≤15 min, parallel)

```bash
bash scripts/pull_images.sh
docker images | grep swe-skills | wc -l     # expect 8
```

## Phase 4 — API Keys + Configuration

**Ask the user for DeepSeek API keys** (need 4-8 keys for parallel rate limit). Then:

```bash
# Write keys
cat > ~/.deepseek_keys << EOF
sk-your-actual-key-1
sk-your-actual-key-2
sk-your-actual-key-3
sk-your-actual-key-4
EOF
chmod 600 ~/.deepseek_keys

# Patch .env files (replace placeholders)
PRIMARY_KEY=$(head -1 ~/.deepseek_keys)
for env in claude-code-exp/config/.env claude-pro-exp/config/.env; do
  sed -i "s|sk-REPLACE_ME_DEEPSEEK_KEY|${PRIMARY_KEY}|g" $env
done

# Verify proxy works
nohup python3 scripts/multi_key_proxy.py > /tmp/proxy.log 2>&1 &
sleep 3
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
# Expect 200
```

## Phase 5 — Smoke Test (~5 min)

```bash
# Run a single task on a single harness to verify the pipeline
~/.venv/bin/python -m agentweave_runner.cli \
  --method M0 --seed 0 \
  --tasks rq21_subset_tasks \
  --harness claude-code \
  --concurrency 1 \
  --out /tmp/smoke \
  --limit 1
# Check: /tmp/smoke/swe_skills_*.json exists with steps > 0
ls /tmp/smoke/*.json | head
python3 -c "
import json
for f in __import__('glob').glob('/tmp/smoke/*.json'):
    d = json.load(open(f))
    if '_summary' in f or '_checkpoint' in f: continue
    r = d['result']
    print(f'{f}: steps={r[\"steps\"]} dur={r[\"duration_sec\"]:.0f}s')
"
```

**If smoke test fails**, check:
- proxy is running: `pgrep -f multi_key_proxy.py`
- claude binary works: `claude-code-exp/bin/claude-exp -p "hi" --output-format stream-json` (should print JSON events)
- docker is accessible: `docker ps` (should not error)

## Phase 6 — Full Experiment (6-8 hours)

```bash
bash scripts/run_full_experiment.sh
tmux list-sessions   # should see rq21_<harness>_<method> × 10
```

## Phase 7 — Monitoring (continuous)

```bash
# Live progress (refreshes every 5s)
bash scripts/monitor.sh

# Or one-shot check
python3 << 'PY'
import json, os
from pathlib import Path
base = Path("runs/rq2_1")
total = 0
for h in ['claude-code', 'claude-pro', 'codex', 'hermes', 'openclaw']:
    for m in ['M0', 'M4']:
        for s in range(3):
            cell = base / h / m / f'seed_{s}'
            if not cell.exists(): continue
            trajs = [f for f in cell.glob('swe_skills_*.json') if '_library_snapshots' not in f.parts]
            total += len(trajs)
            print(f"{h}/{m}/seed_{s}: {len(trajs)}/48")
print(f"\nTotal: {total}/1440 ({100*total/1440:.1f}%)")
PY
```

**Expected completion**: each cell ≈ 30-60 minutes. 10 cells run in parallel, finishing around the same time.

## Common Issues & Fixes

### Issue: proxy returns 404
**Symptom**: codex / claude-pro trajectories have 0 steps
**Cause**: `agents-chat` or other service hijacked port 8765
**Fix**:
```bash
sudo lsof -iTCP:8765 -sTCP:LISTEN
sudo kill -9 <pid>
nohup python3 scripts/multi_key_proxy.py > /tmp/proxy.log 2>&1 &
```

### Issue: docker eval times out repeatedly
**Symptom**: trajectories show `error: docker_eval_wall_timeout_*`
**Cause**: docker daemon under stress
**Fix**:
```bash
# Reduce concurrency in scripts/run_full_experiment.sh
# Change CONC=([claude-code]=6 ...) to CONC=([claude-code]=4 ...)
# Restart affected sessions
```

### Issue: Mac out of memory (if running on macOS, not your server)
**Skip** — server has 500 GB, won't happen.

### Issue: Hermes adapter fails to load session
**Symptom**: hermes trajectories have valid `pass_rate` but `messages=[]`
**Cause**: known limitation in HermesAdapter session parsing
**Action**: data still usable for pass_rate analysis; flag for paper limitations section.

## Success Criteria (How to know we're done)

After `run_full_experiment.sh` runs to completion:

```bash
# All 10 cells must have _summary.json
for h in claude-code claude-pro codex hermes openclaw; do
  for m in M0 M4; do
    for s in 0 1 2; do
      summary="runs/rq2_1/$h/$m/seed_$s/_summary.json"
      if [ -f "$summary" ]; then
        echo "✅ $h/$m/seed_$s"
      else
        echo "❌ $h/$m/seed_$s missing"
      fi
    done
  done
done
```

Expected: **30 ✅ ** (5 harness × 2 method × 3 seed)

## What to do when finished

1. Compress results:
   ```bash
   tar -czf runs_rq2_1_$(date +%Y%m%d).tar.gz runs/rq2_1/
   ```
2. Generate summary report:
   ```bash
   bash scripts/monitor.sh  # final snapshot
   ```
3. Notify the user: "Experiment done. Results at `runs/rq2_1/`. Total N trajectories across 5 harness × 2 method × 3 seed."

## Important Constraints

- **Never push results back to GitHub** (data files are too large + may contain task content)
- **Don't modify** `runner/src/agentweave_runner/cli_harnesses.py` — patches are critical
- **Don't run** without `multi_key_proxy.py` active (single key will hit rate limit)
- **If anything ambiguous**, ask the user; don't guess on infrastructure

## Reference

- Repo: https://github.com/xhh678876/agentweave-rq21-deploy
- Paper context: SWE-Skills-Bench (Han et al., arXiv:2603.15401)
- Key patches explained in `runner/src/agentweave_runner/cli_harnesses.py` docstrings

Good luck. Be methodical, verify each phase, and tell the user concretely what you observe.
