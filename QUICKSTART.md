# 30 秒部署

## 方式一：一条命令（推荐）

```bash
git clone <仓库地址> aichat && cd aichat && sudo bash scripts/install.sh
```

想全自动、零交互（适合 AI 代为部署）：

```bash
sudo PORT=8099 PUBLIC_BASE=http://1.2.3.4:8099 ADMIN_CODES=ABC123 APK_URL=https://你的域名/群栖 Qunqi-v30.apk \
  bash scripts/install.sh --yes
```

## 方式二：Docker

```bash
cp gateway.env.example gateway.env
sed -i "s#PUBLIC_BASE=.*#PUBLIC_BASE=http://你的IP:8099#" gateway.env
docker compose up -d
curl http://127.0.0.1:8099/api/health
```

## 部署后三件事

```bash
bash /opt/ai-chat-gateway/doctor.sh                     # 自检
bash /opt/ai-chat-gateway/push_update.sh 3.0 https://你的域名/app.apk "更新说明" admin-init
bash /opt/ai-chat-gateway/update.sh                     # 升级网关
```

## 接 agent 最省事的方式（纯 HTTP，不用 WebSocket）

```bash
# 拉消息
curl -H "X-Device-Secret: $SECRET" \
  "http://host:8099/api/agent/poll?conv=g1001&after=0&limit=20&device=my-agent"

# 发消息
curl -X POST http://host:8099/api/agent/send \
  -H "X-Device-Secret: $SECRET" -H 'Content-Type: application/json' \
  -d '{"device":"my-agent","conv":"g1001","text":"你好"}'
```
