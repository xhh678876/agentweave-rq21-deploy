# swe-skills-bench

Bridge between [SWE-Skills-Bench](https://github.com/GeniusHTX/SWE-Skills-Bench)
and AgentWeave's runner + pipeline infrastructure.

The upstream benchmark ships 49 real-world software engineering tasks, each
paired with a human-written `SKILL.md` document. Its central question — *does
giving an agent a skill document actually help?* — is exactly the question
AgentWeave's RQ1 main table asks of *machine-learned* libraries (M4) versus a
no-injection baseline (M0). This bridge wires the 49 tasks into AgentWeave's
existing harness, runner, and pipeline **without modifying any of those
upstream components**.

## What this gives you

For RQ1 the AgentWeave paper can now compare, on identical tasks:

| Method | Library at run-time | Source |
| --- | --- | --- |
| M0 | empty | baseline |
| M1 / M2 | AWM workflows / ReasoningBank chunks | AgentWeave's own runner |
| M3 / M4 | AgentWeave DO + DO NOT | AgentWeave's own runner |
| **M_human** | the upstream `SKILL.md` document | this bridge's `skill_loader` |

Adding `M_human` requires only a few lines in `agentweave/runner/methods.py`
that read `upstream/skills/<skill_id>/SKILL.md` through
`swe_skills_bridge.skill_loader.load_human_skill`; the harness contract is
unchanged.

## Architecture

```
SWE-Skills-Bench (upstream/)
  config/benchmark_config.yaml      ← 49 skills × image × eval command
  tasks/batch1/*.md                 ← per-task prompts
  tests/batch1/*.py                 ← per-task pytest files
  skills/<skill_id>/SKILL.md        ← human-written reference skill

         │
         │  task_adapter.py
         ▼
tasks/task_001.json … task_049.json   (OpenAI function-format)
  goal       — TASK.md prompt + repo-root hint
  tools      — list_directory/read_file/write_file/delete_file/copy_file
               (strict subset of langgraph_harness's TOOL_TABLE)
  initial_state.files
               /workspace/TASK.md     → prompt
               /workspace/.skill_id   → upstream skill_id
  verification.type = "swe_skills_docker_test"
               (intentionally unknown to the harness — the synchronous
                verdict will be `success=False`; real eval is deferred)
  verification.evaluation
               method, command, min_pass_rate, timeout
  verification.docker_image
               upstream image to run tests in
  verification.human_skill_path
               relative path to the human SKILL.md, used by M_human

         │
         │  run-method (existing AgentWeave runner)
         ▼
runner-out/swe_skills_<id>.json     (LangGraph trajectory; final_state.files
                                     holds the source files the agent wrote)

         │
         │  eval_adapter.docker_evaluate_trajectory
         ▼
Docker container from verification.docker_image:
  1. push upstream/tests/batch1/test_<sanitised>.py → /workspace/tests/
  2. push trajectory.final_state.files (minus the seeded /workspace/TASK.md
     and /workspace/.skill_id) into the container
  3. exec verification.evaluation.command
  4. parse pytest banner → pass_rate
  5. rewrite trajectory.result.success + trajectory.result.docker_verdict
     + trajectory.verdict so downstream pipeline (L1-L5) sees the real
     pass/fail
```

Because no harness file is touched, `eval_adapter` is a *post-hoc* rewriter
of trajectories produced by the unchanged LangGraph adapter. The agent
operates in the mocked filesystem; we materialise the files it wrote into a
real container only at evaluation time.

## Install

```bash
# from this directory
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# one-time setup: pull docker images + rebuild task JSONs
./bin/swe-skills-bridge install              # ~12 GB total for 8 images
# or, if upstream/ is already cloned:
./bin/swe-skills-bridge install --skip-clone
# or, to refresh tasks/ only:
./bin/swe-skills-bridge build-tasks
```

The wrapper auto-detects `colima`'s Docker socket on macOS and sets
`DOCKER_HOST` accordingly. Override with `DOCKER_HOST=unix:///...` if you use a
different daemon.

## Quick reference

```bash
# enumerate the 49 tasks
./bin/swe-skills-bridge list-tasks

# end-to-end smoke (harness run → docker eval on one task)
./bin/swe-skills-bridge run-one \
    --task-id bash-defensive-patterns \
    --output /tmp/swe_one.json \
    --max-steps 10

# end-to-end smoke without the docker step
./bin/swe-skills-bridge run-one --task-id bash-defensive-patterns \
    --output /tmp/swe_one.json --skip-docker-eval

# evaluate a single saved trajectory
./bin/swe-skills-bridge eval-trajectory --trajectory /tmp/swe_one.json

# evaluate an entire runner output directory in one shot
./bin/swe-skills-bridge eval-batch --runner-out /tmp/swe_runner/

# print the human SKILL.md (used by M_human in the runner)
./bin/swe-skills-bridge show-skill --skill-id add-uint-support
```

## Driving the AgentWeave runner

Once `tasks/` is built, the existing AgentWeave runner consumes them with no
changes:

```bash
~/agentweave/runner/bin/run-method \
    --method M0 --seed 0 \
    --tasks ~/agentweave/swe-skills-bench/tasks/ \
    --harness langgraph \
    --concurrency 4 --limit 3 \
    --out /tmp/swe_runner/

# then materialise verdicts into the trajectories:
~/agentweave/swe-skills-bench/bin/swe-skills-bridge eval-batch \
    --runner-out /tmp/swe_runner/
```

After `eval-batch`, every trajectory's `result.success`, top-level `verdict`,
and the new `result.docker_verdict` reflect the real Docker pytest outcome,
so downstream L1 → L5 pipeline ingestion sees authoritative pass/fail.

## Project layout

```
swe-skills-bench/
├── README.md                   this file
├── pyproject.toml              setuptools + pytest config
├── .gitignore
├── .venv/                      local venv
├── upstream/                   git clone of GeniusHTX/SWE-Skills-Bench
├── bin/swe-skills-bridge       wrapper (auto-detects venvs + docker socket)
├── src/swe_skills_bridge/
│   ├── __init__.py
│   ├── cli.py                  click CLI
│   ├── installer.py            clone upstream + pull docker images + build tasks
│   ├── task_adapter.py         upstream task → OpenAI function task JSON
│   ├── eval_adapter.py         trajectory → Docker pytest → verdict rewrite
│   └── skill_loader.py         load upstream/skills/<id>/SKILL.md
├── tasks/
│   ├── INDEX.json              49-entry registry
│   └── task_001.json … task_049.json
└── tests/
    └── test_smoke_e2e.py       unit + (gated) integration smoke
```

## Reproducibility note for the paper

To reproduce the SWE-Skills-Bench column of the RQ1 main table:

1. `./bin/swe-skills-bridge install`
   Pulls the 8 upstream Docker images (≈12 GB) and writes `tasks/task_001.json …
   tasks/task_049.json`.

2. Drive each method through the existing runner, once per seed:

   ```bash
   for METHOD in M0 M1 M2 M3 M4; do
     ~/agentweave/runner/bin/run-method \
         --method $METHOD --seed 0 \
         --tasks ~/agentweave/swe-skills-bench/tasks/ \
         --harness langgraph --concurrency 8 \
         --out ~/agentweave/runs/swe_skills/$METHOD/seed_0/
   done
   ```

3. Compute the real verdicts in Docker:

   ```bash
   for METHOD in M0 M1 M2 M3 M4; do
     ~/agentweave/swe-skills-bench/bin/swe-skills-bridge eval-batch \
         --runner-out ~/agentweave/runs/swe_skills/$METHOD/seed_0/
   done
   ```

4. Feed the trajectories through AgentWeave's existing aggregator scripts
   (`~/agentweave/pipeline/`) for the SR / token / step columns.

## Known limitations

- **Mock-vs-real workspace gap.** The agent runs in AgentWeave's mocked
  in-memory filesystem (the LangGraph harness's `TOOL_TABLE`), not inside the
  real Docker image. It does not see the upstream repo's source files —
  only the task brief at `/workspace/TASK.md`. This is by design (we cannot
  modify the harness), and matches the *spirit* of the upstream "skill
  injection only, no source preview" baseline since the agent must reason
  from the brief alone. Tasks that require *reading* existing code (e.g.
  PyTorch `add-uint-support`) are expected to do worse than the upstream's
  Claude-Code-CLI baseline. This is documented as a Limitation in the paper.
- **No build step.** Only `unit_test` evaluations are wired up. If a task's
  upstream config defines an `L1: build_check`, it is recorded in
  `verification.evaluation` but `unit_test` takes precedence when both are
  present.
- **Docker images are large.** ~2 GB per image, 8 unique images → ~12 GB of
  disk for a full install. The `install --skip-docker-pull` flag lets you
  rebuild tasks without re-pulling.
- **Image availability.** All eight `zhangyiiiiii/swe-skills-bench-*` images
  are public on Docker Hub at the time of writing. If a future image is
  pulled, the task's verdict will be `skipped_reason="docker_unavailable"`
  rather than crashing the runner.

## Verification

```bash
# offline tests (no docker, no LLM)
.venv/bin/python -m pytest tests/test_smoke_e2e.py -m unit

# gated end-to-end test (requires docker + DeepSeek key)
RUN_E2E_SMOKE=1 .venv/bin/python -m pytest -k test_e2e_run_one_smoke -s
```
