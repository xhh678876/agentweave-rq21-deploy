# AgentWeave 实验专属龙虾（harness-exp）

> 与你 `/opt/homebrew/bin/openclaw`（日常龙虾）**完全隔离**的实验环境。

## 关键事实

| | 日常龙虾 | 实验龙虾 |
|---|----------|----------|
| 命令 | `openclaw` | `openclaw-exp` |
| 二进制 | `/opt/homebrew/bin/openclaw` | `~/agentweave/harness-exp/node_modules/.bin/openclaw` |
| 版本 | 2026.4.24 | **2026.5.12** |
| 配置目录 | `~/.claude/` | `~/agentweave/harness-exp/config/` |
| 主模型 | 你的默认 | **deepseek-v4-flash**（锁定） |
| 已加载 skills | ECC + SJTU + lobster-square 等 | **零** |
| 已加载 memory | 14 条（SJTU/Hermes/clawsjtu...） | **零** |

跑日常事的时候用 `openclaw`，跑 AgentWeave 实验只用 `openclaw-exp`。

## 启动状态（已配置完成）

- ✅ 实验龙虾 OpenClaw 2026.5.12 (f066dd2) 已装在 node_modules/
- ✅ Profile `agentweave-exp` 已创建于 `~/.openclaw-agentweave-exp/`
- ✅ DeepSeek provider 已配置 → baseUrl `https://api.deepseek.com/v1`
- ✅ API key 已写入 `~/.openclaw-agentweave-exp/agents/main/agent/auth-profiles.json`（chmod 600）
- ✅ 默认模型锁定 `deepseek/deepseek-v4-flash`
- ✅ 端到端 inference 冒烟测试已通过

## 跑实验

```bash
# 方式 A：用 wrapper（推荐，自动加载 .env）
~/agentweave/harness-exp/bin/openclaw-exp capability model run \
  --model "deepseek/deepseek-v4-flash" --prompt "hi"

# 方式 B：直接用 --profile（无需 wrapper）
~/agentweave/harness-exp/node_modules/.bin/openclaw \
  --profile agentweave-exp capability model run \
  --model "deepseek/deepseek-v4-flash" --prompt "hi"

# 把 wrapper 加进 PATH（可选）
echo 'export PATH="$HOME/agentweave/harness-exp/bin:$PATH"' >> ~/.zshrc
```

## 验证两只龙虾完全隔离

```bash
openclaw-exp --version   # → 2026.5.12（实验，干净）
openclaw     --version   # → 2026.4.24（日常，不动）
```

## API key 重置（如需 rotate）

```bash
# 1. 在 platform.deepseek.com 生成新 key
# 2. 改两个地方：
$EDITOR ~/agentweave/harness-exp/config/.env
$EDITOR ~/.openclaw-agentweave-exp/agents/main/agent/auth-profiles.json
# 3. chmod 600 那两个文件
```

## 论文 reproducibility 章节可以直接引用

```
Experimental harness:
  OpenClaw 2026.5.12 (commit f066dd2), installed locally via npm in
  isolated project ~/agentweave/harness-exp/. CLAUDE_CONFIG_DIR
  pointed to a clean configuration: no user memory, no third-party
  skills, no custom hooks, no agent definitions. Tool access was
  restricted to {Bash, Read, Write, Edit}. Model locked to
  deepseek-v4-flash via DashScope.
```
