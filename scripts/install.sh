#!/usr/bin/env bash
set -euo pipefail
# 支持全自动：PORT=8099 PUBLIC_BASE=http://1.2.3.4:8099 ADMIN_CODES=ABC123 bash install.sh --yes
DIR="${QUNQI_DIR:-/opt/ai-chat-gateway}"
SERVICE_NAME="${QUNQI_SERVICE:-ai-chat-gateway}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1
[ -t 0 ] || ASSUME_YES=1

ask() { # ask "提示" 默认值 -> echo 值
  local p="$1" d="${2:-}" v
  if [ "$ASSUME_YES" = "1" ]; then echo "$d"; return; fi
  read -rp "$p [$d]: " v || true
  echo "${v:-$d}"
}

PORT="${PORT:-$(ask '监听端口' 8099)}"
if [ -z "${PUBLIC_BASE:-}" ]; then
  IP=$(curl -4 -fsS --max-time 4 https://ifconfig.me 2>/dev/null || true)
  [ -z "$IP" ] && IP=$(hostname -I | awk '{print $1}')
  PUBLIC_BASE="http://${IP}:${PORT}"
  PUBLIC_BASE=$(ask '公网访问地址' "$PUBLIC_BASE")
fi
BOT_NAME="${BOT_NAME:-$(ask 'AI 名称' 'AI 助手')}"
BOT_ID="${BOT_ID:-$(ask 'AI 数字 uid' 2001)}"
ADMIN_CODES="${ADMIN_CODES:-$(ask '管理员身份码（留空自动生成）' '')}"
[ -z "$ADMIN_CODES" ] && ADMIN_CODES=$(head -c 8 /dev/urandom | tr -dc 'A-Z0-9')
BOT_TOKEN="${BOT_TOKEN:-$(head -c 40 /dev/urandom | tr -dc 'A-Za-z0-9')}"
ASTRBOT_WS="${ASTRBOT_WS:-$(ask 'AstrBot WS 地址（留空=纯本地网关）' '')}"
APK_URL="${APK_URL:-$(ask 'APK 直链（可留空）' '')}"
REPO_SLUG="${REPO_SLUG:-$(ask 'GitHub 仓库 slug（可留空）' '')}"

echo "→ 安装到 $DIR"

# 干净机器常常缺 python3-venv / ensurepip，先补齐
PY_BIN="$(command -v python3)"
if ! "$PY_BIN" -c "import venv, ensurepip" >/dev/null 2>&1; then
  echo "→ 缺少 python3-venv，尝试安装"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq || true
    sudo apt-get install -y -qq python3-venv python3-pip || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3-pip || true
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3-pip || true
  fi
fi
"$PY_BIN" -c "import venv, ensurepip" >/dev/null 2>&1 || {
  echo "python3-venv 仍不可用，请先执行：sudo apt install -y python3-venv"; exit 1; }

sudo mkdir -p "$DIR/data/uploads" "$DIR/data/posters" "$DIR/static" "$DIR/plugins"
sudo cp "$REPO_DIR/server/server.py" "$DIR/server.py"
sudo cp "$REPO_DIR/server/static/"* "$DIR/static/"
sudo cp "$REPO_DIR/scripts/ai-chat-gateway.service" "/etc/systemd/system/$SERVICE_NAME.service"
sudo cp "$REPO_DIR/scripts/auto_update.sh" "$REPO_DIR/scripts/push_update.sh" "$REPO_DIR/scripts/check_update.sh" "$REPO_DIR/scripts/doctor.sh" "$DIR/"
sudo cp "$REPO_DIR/VERSION" "$DIR/VERSION"
sudo chmod +x "$DIR"/*.sh
if [ -d "$REPO_DIR/plugins" ]; then sudo cp -r "$REPO_DIR/plugins/." "$DIR/plugins/" 2>/dev/null || true; fi

if [ ! -x "$DIR/.venv/bin/python" ]; then
  sudo rm -rf "$DIR/.venv"
  sudo "$PY_BIN" -m venv "$DIR/.venv" || { echo "venv 创建失败"; exit 1; }
fi
sudo "$DIR/.venv/bin/pip" install -q --upgrade pip
sudo "$DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

sudo tee "$DIR/gateway.env" >/dev/null <<ENV
PORT=$PORT
PUBLIC_BASE="$PUBLIC_BASE"
AGENT_MODE=onebot
ASTRBOT_WS="$ASTRBOT_WS"
BOT_ID=$BOT_ID
BOT_NAME="$BOT_NAME"
BOT_TOKEN=$BOT_TOKEN
ADMIN_CODES=$ADMIN_CODES
MAX_UPLOAD_MB=300
ENV
sudo chmod 600 "$DIR/gateway.env"

sudo sed -i "s#__DIR__#$DIR#g; s#__PORT__#$PORT#g; s#ai-chat-gateway.service#$SERVICE_NAME.service#g" "/etc/systemd/system/$SERVICE_NAME.service"
if [ "$SERVICE_NAME" != "ai-chat-gateway" ]; then sudo sed -i "s#ai-chat-gateway#$SERVICE_NAME#g" "/etc/systemd/system/$SERVICE_NAME.service"; fi
if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  sudo systemctl restart "$SERVICE_NAME"
else
  echo "→ 当前环境没有 systemd，直接用 nohup 启动做自检"
  sudo bash -c "set -a; . '$DIR/gateway.env'; set +a; cd '$DIR' && nohup '$DIR/.venv/bin/python' '$DIR/server.py' >> '$DIR/gateway.log' 2>&1 &"
fi
sleep 3

if ! curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null; then
  echo "启动失败，最近日志："; sudo journalctl -u "$SERVICE_NAME" -n 25 --no-pager; exit 1
fi

if [ -n "$APK_URL" ]; then
  DEV=admin-init
  curl -fsS -X POST "http://127.0.0.1:$PORT/api/device" -H 'Content-Type: application/json' \
    -d "{\"device_id\":\"$DEV\",\"name\":\"管理员\",\"kind\":\"user\"}" >/dev/null || true
  SECRET=$(curl -fsS "http://127.0.0.1:$PORT/api/me?device=$DEV" | "$DIR/.venv/bin/python" -c "import json,sys;print(json.load(sys.stdin)['secret'])")
  curl -fsS -X POST "http://127.0.0.1:$PORT/api/admin/push_update" -H 'Content-Type: application/json' \
    -H "X-Device-Secret: $SECRET" \
    -d "{\"device\":\"$DEV\",\"version\":\"$(cat "$REPO_DIR/VERSION")\",\"apk_url\":\"$APK_URL\",\"notes\":\"初始版本\"}" >/dev/null
  echo "→ 已广播首版客户端"
fi

if [ -n "$REPO_SLUG" ] && [ "$ASSUME_YES" = "0" ]; then
  read -rp "启用每日自动跟随 GitHub Release？[y/N]: " AUTO || true
  if [ "${AUTO:-n}" = "y" ]; then
    sudo tee "$DIR/update.env" >/dev/null <<UPD
OWNER_REPO=$REPO_SLUG
AUTO_PUSH_CLIENT=1
UPD
    sudo cp "$REPO_DIR/scripts/aichat-autoupdate."{service,timer} /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now aichat-autoupdate.timer
    echo "→ 已启用每日自动更新（关闭：systemctl disable --now aichat-autoupdate.timer）"
  fi
fi

cat <<INFO

✅ 部署完成
站点：$PUBLIC_BASE
管理员身份码：$ADMIN_CODES
AI 口令：$BOT_TOKEN（已写入 $DIR/gateway.env，权限 600）

自检：bash $DIR/doctor.sh
推客户端：bash $DIR/push_update.sh <版本> <APK直链> "<说明>" admin-init
网关升级：bash $DIR/update.sh
INFO
