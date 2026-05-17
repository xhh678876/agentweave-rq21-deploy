# AgentWeave Method Runner

Orchestrates the AgentWeave paper's RQ1-RQ4 experiments: runs each method
(M0, M1, M2, M3, M4) over a task set at one seed through one harness, writes
trajectories and library snapshots, supports resume.

## Methods

| ID | Name | Library shape | Injection ordering | Pre-filter |
|----|------|---------------|--------------------|-----------:|
| M0 | No-Sharing | none | none (empty system prompt) | — |
| M1 | AWM | workflow steps | DO only | tool overlap |
| M2 | ReasoningBank | reasoning chunks | DO only | tool overlap |
| M3 | AgentWeave w/o L5 | DO + DO NOT | flattened into DO list (no constraint-first) | none |
| M4 | AgentWeave Full | DO + DO NOT | DO NOT first, DO second (constraint-first) | tool overlap (L5) |

## Install

The runner re-uses the LangGraph harness venv to avoid re-pinning langgraph:

```bash
cd ~/agentweave/runner
# Reuse harness venv (which has langgraph_harness importable)
~/agentweave/harness-exp-langgraph/.venv/bin/pip install -e .
```

Or create a local venv:

```bash
cd ~/agentweave/runner
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install -e ~/agentweave/harness-exp-langgraph
```

## Smoke

```bash
cd ~/agentweave/runner
~/agentweave/harness-exp-langgraph/.venv/bin/python -m agentweave_runner.cli \
    --method M4 --seed 0 \
    --tasks ~/agentweave/pilot_experiment/tasks/ \
    --harness langgraph \
    --concurrency 4 --limit 5 \
    --out /tmp/runner_smoke/

ls /tmp/runner_smoke/task_*.json | wc -l   # → 5
cat /tmp/runner_smoke/_summary.json        # → metrics
ls /tmp/runner_smoke/_library_snapshots/   # → snapshots
```

## EXP-1 command sheet

The standard call shape for a full RQ1 sweep (one method × one seed)::

```bash
~/agentweave/runner/bin/run-method \
    --method M4 \
    --seed 0 \
    --tasks ~/agentweave/tasks_v2/ \
    --harness langgraph \
    --concurrency 32 \
    --out ~/agentweave/runs/rq1/M4/seed_0/
```

For a full method sweep:

```bash
for METHOD in M0 M1 M2 M3 M4; do
  for SEED in 0 1 2; do
    ~/agentweave/runner/bin/run-method \
        --method $METHOD --seed $SEED \
        --tasks ~/agentweave/tasks_v2/ \
        --harness langgraph --concurrency 32 \
        --out ~/agentweave/runs/rq1/$METHOD/seed_$SEED/
  done
done
```

## Resume

```bash
~/agentweave/runner/bin/run-method \
    --method M4 --seed 0 \
    --tasks ~/agentweave/tasks_v2/ \
    --harness langgraph \
    --concurrency 32 \
    --out ~/agentweave/runs/rq1/M4/seed_0/ \
    --resume
```

## Output layout

```
out_dir/
├── task_01.json              # trajectory per task
├── task_02.json
├── ...
├── _library_snapshots/       # library after every N completed tasks
│   ├── after_0000.json
│   ├── after_0010.json
│   └── ...
├── _checkpoint.json          # resume cursor
└── _summary.json             # metrics: success_rate, mean_steps, tokens, ...
```

## Concurrency / batch semantics

Tasks run in mini-batches of size `--concurrency`. Within a batch every task
sees the same library snapshot; after the batch we serially apply trajectories
to the library and snapshot. `--concurrency 1` collapses this to strict
sequential streaming (the natural baseline for memory-augmented agents).

## Tests

```bash
~/agentweave/harness-exp-langgraph/.venv/bin/python -m pytest \
    ~/agentweave/runner/tests/ -m unit -v
```

The unit suite is hermetic (no LLM, no network). For an end-to-end smoke
test, run the `--method M4 --limit 5` CLI command above.
