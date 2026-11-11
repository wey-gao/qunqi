#!/usr/bin/env bash
set -euo pipefail
# 可选：网关 + 本站客户端一起自动跟随 GitHub Release
# 配置文件 /opt/ai-chat-gateway/update.env：
#   OWNER_REPO=yourname/aichat-gateway
#   REPO_DIR=/opt/aichat-src        # 可选，git 部署时填
#   AUTO_PUSH_CLIENT=1              # 1=发现新 APK 就广播给本站客户端
DIR=/opt/ai-chat-gateway
CONF="$DIR/update.env"
[ -f "$CONF" ] || { echo "缺少 $CONF"; exit 0; }
set -a; . "$CONF"; set +a
set -a; . "$DIR/gateway.env"; set +a
BASE="${PUBLIC_BASE:-http://127.0.0.1:$PORT}"
REL=$(curl -fsSL "https://api.github.com/repos/$OWNER_REPO/releases/latest")
TAG=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['tag_name'])" "$REL")
LOCAL=$(cat "$DIR/VERSION" 2>/dev/null || echo "0")
if [ "$TAG" = "$LOCAL" ]; then echo "已是最新 $TAG"; exit 0; fi

# 1) 网关代码：git 部署直接拉，否则下 release 源码包
if [ -n "${REPO_DIR:-}" ] && [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch --tags --force
  git -C "$REPO_DIR" checkout "$TAG"
  cp "$REPO_DIR/server/server.py" "$DIR/server.py"
  cp "$REPO_DIR/server/static/"* "$DIR/static/"
else
  TMP=$(mktemp -d)
  curl -fsSL "https://github.com/$OWNER_REPO/archive/refs/tags/$TAG.tar.gz" | tar -xz -C "$TMP"
  SRC=$(find "$TMP" -maxdepth 1 -type d | tail -1)
  cp "$SRC/server/server.py" "$DIR/server.py"
  cp "$SRC/server/static/"* "$DIR/static/"
  rm -rf "$TMP"
fi
systemctl restart ai-chat-gateway
sleep 2
curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null && echo "$TAG" > "$DIR/VERSION"

# 2) 客户端 APK：把 release 里的 apk 广播给本站用户
if [ "${AUTO_PUSH_CLIENT:-0}" = "1" ]; then
  APK_URL=$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print(next((a['browser_download_url'] for a in d['assets'] if a['name'].endswith('.apk')), ''))" "$REL")
  if [ -n "$APK_URL" ]; then
    curl -fsSL -X POST "$BASE/api/admin/push_update" -H 'Content-Type: application/json' \
      -H "X-Device-Secret: $BOT_TOKEN" \
      -d "$(python3 -c "import json,sys;print(json.dumps({'device':'bot','version':sys.argv[1].lstrip('v'),'apk_url':sys.argv[2],'notes':'自动更新 '+sys.argv[1]}))" "$TAG" "$APK_URL")" >/dev/null
    echo "已推送客户端 $TAG"
  fi
fi
