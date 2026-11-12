# 群栖网关 · 运维待办清单

给管理员看的服务器侧事项。每条只写：现象 / 风险 / 怎么做 / 怎么验证。
涉及删除、重启、改权限的操作，执行前先确认。

## 1. 数据库没有定时备份
- 现象：目前只有手动 `.bak`，没有定时任务。
- 风险：误删、磁盘损坏会丢全部聊天记录和身份绑定。
- 怎么做：
```bash
sudo mkdir -p /opt/backups
sudo tee /etc/cron.d/qunqi-backup >/dev/null <<'CRON'
30 3 * * * ecs-user sqlite3 /opt/ai-chat-gateway/data/chat.db ".backup '/opt/backups/chat-$(date +\%Y\%m\%d).db'"
0 4 * * 0 ecs-user find /opt/backups -name 'chat-*.db' -mtime +14 -delete
CRON
```
- 怎么验证：`ls -lh /opt/backups`，第二天的文件存在且大小非 0；`sqlite3 <备份文件> "select count(*) from message"` 能查出条数。

## 2. 全站明文 HTTP
- 现象：客户端默认地址是 `<你的服务器地址:端口>`，密钥和消息在公网可被旁路。
- 风险：同一网络下可被抓包看到设备密钥，进而接管账号。
- 怎么做：启用已有的 cloudflared 隧道地址作为 HTTPS 入口，客户端改填该地址；或在网关前加 Nginx + 证书。
- 怎么验证：浏览器访问 HTTPS 地址，`/api/health` 返回 ok，App 内能正常收发。

## 3. 文件链接没有签名与过期
- 现象：`/media/<file_id>`、`/files/<file_id>` 只靠 ID 不可猜。
- 风险：链接外泄后任何人可长期下载。
- 怎么做：需要时再加签名参数（HMAC + 过期时间），属于代码改动，先评估再排期。
- 怎么验证：改动后用带过期时间的链接访问，超时后返回 403。

## 4. 上传与消息没有保留策略
- 现象：`uploads` 只增不减；`MSG_RETENTION_DAYS` 默认 0（不清理）。
- 风险：磁盘会被慢慢占满；自用可接受，但要盯着。
- 怎么做：设置 `MSG_RETENTION_DAYS`（如 365）后重启；定期看 `/api/storage` 的占用。
- 怎么验证：`/api/storage` 看 uploads_bytes 与 files 数量是否随清理下降。

## 5. 公开模式下 WebSocket 未校验 Origin
- 现象：`SECURITY_LEVEL=public` 时仍不检查 Origin。
- 风险：恶意网页可诱导浏览器建立 WS 连接。
- 怎么做：自用（private）可不动；要公开就加 Origin 白名单，属代码改动。
- 怎么验证：用非白名单 Origin 连接，应被拒绝（4403）。

## 6. 日志与磁盘没有监控
- 现象：`diag.log` 默认关闭，超过 1MB 才自动删；磁盘使用没有告警。
- 风险：出问题没有现场日志；磁盘满会导致上传失败、数据库写入异常。
- 怎么做：需要排查时临时开 `DIAG_LOG=1`；日常用 `df -h` 和 `/api/storage` 看容量。
- 怎么验证：`curl /api/health` 与 `/api/storage` 数值正常，`df -h` 剩余空间充足。
