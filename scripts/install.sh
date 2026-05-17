#!/usr/bin/env bash
# 装 agentweave 各组件 (在 setup.sh 之后跑)
set -euo pipefail

cd $(dirname $0)/..
ROOT=$(pwd)
echo "═══ 安装在 $ROOT ═══"

source ~/.venv/bin/activate

# 1. 装 runner
echo "── 1. 装 runner ──"
pip install -e $ROOT/runner

# 2. 装 swe-skills-bench bridge
echo "── 2. 装 swe-skills-bench ──"
pip install -e $ROOT/swe-skills-bench

# 3. 装 claude-code binary (npm)
echo "── 3. 装 claude-code binary ──"
cd $ROOT/claude-code-exp
npm install @anthropic-ai/claude-code
ln -sf $(pwd)/node_modules/@anthropic-ai/claude-code/bin/claude.exe $(pwd)/node_modules/.bin/claude

# 4. claude-pro-exp 复用同一 binary
cd $ROOT/claude-pro-exp
mkdir -p node_modules/.bin
ln -sf $ROOT/claude-code-exp/node_modules/@anthropic-ai $ROOT/claude-pro-exp/node_modules/@anthropic-ai
ln -sf ../@anthropic-ai/claude-code/bin/claude.exe $ROOT/claude-pro-exp/node_modules/.bin/claude

# 5. codex binary (npm 0.45 版本)
echo "── 5. 装 codex binary v0.45 ──"
cd $ROOT/codex-exp
npm install @openai/codex@0.45.0

# 6. hermes (Python venv)
echo "── 6. 装 hermes ──"
python3 -m venv $ROOT/hermes-exp/.venv
$ROOT/hermes-exp/.venv/bin/pip install -e $ROOT/hermes-exp/upstream

# 7. openclaw (在 harness-exp / npm)
echo "── 7. 装 openclaw ──"
cd $ROOT/openclaw-exp
npm install

echo
echo "── ✅ 所有 harness 装好 ──"
echo "  下一步: bash scripts/pull_images.sh"
