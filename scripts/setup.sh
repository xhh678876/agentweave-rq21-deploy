#!/usr/bin/env bash
# 服务器一键装环境 (Ubuntu 22.04/24.04)
set -euo pipefail

echo "═══ AgentWeave 服务器环境安装 ═══"

# 1. 基础工具
echo "── 1. 装基础工具 ──"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  docker.io git tmux htop curl jq build-essential \
  python3 python3-pip python3-venv python3-dev

# 2. Docker 设置
echo "── 2. Docker 启动 ──"
sudo systemctl enable --now docker
sudo usermod -aG docker $USER

# 3. Docker daemon 高并发配置
echo "── 3. Docker daemon 调优 ──"
sudo tee /etc/docker/daemon.json > /dev/null << 'DCONF'
{
  "max-concurrent-downloads": 30,
  "max-concurrent-uploads": 30,
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
DCONF
sudo systemctl restart docker

# 4. Node.js 20
echo "── 4. Node.js 20 ──"
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
  sudo apt-get install -y -qq nodejs
fi
node --version

# 5. Python venv
echo "── 5. Python venv ──"
cd ~
python3 -m venv ~/.venv
source ~/.venv/bin/activate
pip install --upgrade pip wheel
pip install docker requests

echo "── ✅ 基础环境装好 ──"
echo "  下一步: bash scripts/install.sh"
