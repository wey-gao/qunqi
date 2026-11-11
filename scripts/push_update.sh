#!/usr/bin/env bash
set -euo pipefail
# 用法：push_update.sh <版本> <APK直链> [说明] [管理员device_id]
VER="${1:?版本，如 3.0}"; URL="${2:?APK直链}"; NOTES="${3:-}"; DEV="${4:-}"
DIR=/opt/ai-chat-gateway
set -a; . "$DIR/gateway.env"; set +a
BASE="${PUBLIC_BASE:-http://127.0.0.1:$PORT}"
if [ -z "$DEV" ]; then read -rp "管理员 device_id: " DEV; fi
SECRET=$(curl -fsS "$BASE/api/me?device=$DEV" | python3 -c "import json,sys;print(json.load(sys.stdin)['secret'])")
curl -fsS -X POST "$BASE/api/admin/push_update" \
  -H 'Content-Type: application/json' -H "X-Device-Secret: $SECRET" \
  -d "$(python3 -c "import json,sys;print(json.dumps({'device':sys.argv[1],'version':sys.argv[2],'apk_url':sys.argv[3],'notes':sys.argv[4]}))" "$DEV" "$VER" "$URL" "$NOTES")"
echo; echo "已推送 $VER 给本站客户端"
