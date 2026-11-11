#!/usr/bin/env bash
set -euo pipefail
# 部署者可选：从 GitHub Release 拉最新 APK 并推给本站客户端
DIR=/opt/ai-chat-gateway
OWNER_REPO="${OWNER_REPO:?例如 yourname/aichat-gateway}"
set -a; . "$DIR/gateway.env"; set +a
BASE="${PUBLIC_BASE:-http://127.0.0.1:$PORT}"
LATEST=$(curl -fsSL "https://api.github.com/repos/$OWNER_REPO/releases/latest")
VER=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['tag_name'].lstrip('v'))" "$LATEST")
URL=$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print(next(a['browser_download_url'] for a in d['assets'] if a['name'].endswith('.apk')))" "$LATEST")
CUR=$(curl -fsSL "$BASE/api/version" | python3 -c "import json,sys;print(json.load(sys.stdin).get('version','0'))")
[ "$VER" = "$CUR" ] && exit 0
curl -fsSL -X POST "$BASE/api/admin/push_update" -H 'Content-Type: application/json' \
  -H "X-Device-Secret: $BOT_TOKEN" \
  -d "$(python3 -c "import json,sys;print(json.dumps({'device':'bot','version':sys.argv[1],'apk_url':sys.argv[2],'notes':'自动同步自 GitHub Release'}))" "$VER" "$URL")"
echo "已推送 $VER"
