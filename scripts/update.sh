#!/usr/bin/env bash
set -euo pipefail
# 从仓库拉最新代码并重启（不覆盖 gateway.env / data）
REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
DIR=/opt/ai-chat-gateway
cd "$REPO_DIR" && git pull --ff-only
sudo cp server/server.py "$DIR/server.py"
sudo cp server/static/* "$DIR/static/"
sudo systemctl restart ai-chat-gateway
sleep 2
curl -sf "http://127.0.0.1:${PORT:-8099}/api/health" >/dev/null && echo "已更新"
