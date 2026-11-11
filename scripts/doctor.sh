#!/usr/bin/env bash
set -uo pipefail
DIR=/opt/ai-chat-gateway
ok(){ echo "[OK] $*"; }; bad(){ echo "[!!] $*"; }
set -a; . "$DIR/gateway.env" 2>/dev/null || bad "缺少 gateway.env"
set +a
PORT="${PORT:-8099}"

command -v python3 >/dev/null && ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)" || bad "缺 python3"
[ -x "$DIR/.venv/bin/python" ] && ok "venv 就绪" || bad "venv 缺失，重跑 install.sh"
[ "$(stat -c %a "$DIR/gateway.env" 2>/dev/null)" = "600" ] && ok "gateway.env 权限 600" || bad "gateway.env 权限不是 600"
[ -n "${BOT_TOKEN:-}" ] && ok "BOT_TOKEN 已设置" || bad "BOT_TOKEN 为空"
systemctl is-active --quiet ai-chat-gateway && ok "服务运行中" || bad "服务未运行"
curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null && ok "健康检查通过" || bad "健康检查失败"
curl -fsS "http://127.0.0.1:$PORT/api/version" >/dev/null && ok "版本接口可用" || bad "版本接口异常"
[ -f "$DIR/data/chat.db" ] && ok "数据库 $(du -h "$DIR/data/chat.db" | cut -f1)" || bad "数据库不存在"
FREE=$(df -Pm "$DIR" | awk 'NR==2{print $4}')
[ "${FREE:-0}" -gt 500 ] && ok "磁盘剩余 ${FREE}MB" || bad "磁盘剩余仅 ${FREE}MB"
command -v ffmpeg >/dev/null && ok "ffmpeg 可用（视频首帧）" || bad "缺 ffmpeg（视频首帧不可用）"
ss -lnt 2>/dev/null | grep -q ":$PORT " && ok "端口 $PORT 监听中" || bad "端口 $PORT 未监听"
echo
echo "外部访问需确认：云安全组放行 $PORT；反向代理要转发 WebSocket Upgrade。"
