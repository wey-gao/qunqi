# 群栖 Qunqi · 开源私域聊天系统

纯开源、自托管：**没有中心服务器**（Qunqi，读作“群栖”：AI 与人群栖于同一间聊天室）。任何人 clone 后部署成自己的私域聊天站，用户数据、APK 分发、更新推送都归部署者自己掌控。

```
你的 GitHub 仓库 ──(发布 vX.Y 标签)──> 网关代码 + 已签名 APK（Release 资产）
                                          │
                          部署者 clone / 拉 tag  ──> 自己的服务器
                                          │
                     部署者自己的 APK 直链 ──> 推给自己站点的客户端
```

职责边界：

| 角色 | 负责 |
|---|---|
| 仓库维护者（你） | 发布网关版本、CI 构建 APK、写文档 |
| 部署者（任何人） | 跑网关、决定域名/端口、托管并分发 APK、向自己的用户推更新 |
| 网关 | 保存本站用户数据、给本站客户端广播 `app_update` |

## 界面

界面随版本迭代调整，首次启动需要先填写自己的服务器地址。

## 30 秒部署

```bash
git clone https://github.com/wey-gao/qunqi.git aichat && cd aichat && sudo bash scripts/install.sh
```

零交互（AI 代为部署）：`sudo PORT=8099 PUBLIC_BASE=http://1.2.3.4:8099 ADMIN_CODES=ABC123 APK_URL=<直链> bash scripts/install.sh --yes`

Docker：`cp gateway.env.example gateway.env && docker compose up -d`

部署后 `bash /opt/ai-chat-gateway/doctor.sh` 一键自检。详见 `QUICKSTART.md`。

## 一键部署（部署者视角）

```bash
git clone https://github.com/wey-gao/qunqi.git aichat && cd aichat
bash scripts/install.sh          # 交互问端口/公网地址/AI 名称/管理员码，自动建 venv + systemd + 自检
```

或 Docker：

```bash
cp gateway.env.example gateway.env && vim gateway.env
docker compose up -d
```

部署完你会得到：站点地址、管理员身份码、`BOT_TOKEN`（只存在 `gateway.env`，600 权限）。

## APK 从哪来

两种都行，部署者自己选：

1. **用 Release 里的通用 APK**（仓库维护者 CI 构建、签名）：直接下载 `AI-Chat-vX.Y.apk` 放到自己的对象存储/OSS，拿到直链。
2. **自己构建并签名**：见 `android/BUILD.md` 与 `scripts/make_keystore.sh`。签名证书自己保管，别丢，否则老用户无法覆盖升级。

拿到直链后，注册一个管理员设备，然后推给自己的用户：

```bash
bash scripts/push_update.sh 3.0 https://你的域名/AI-Chat-v30.apk "相机拍照、首启资料"
```

## 更新模型（两层，互不影响）

- **网关更新**：维护者在你 fork/部署的仓库打 tag；部署者 `bash scripts/update.sh` 拉代码重启（可挂 systemd timer 每日检查）。
- **客户端更新**：部署者把新 APK 放上自己的托管地址，调用 `/api/admin/push_update`；所有在线客户端收到提示条，`GET /api/version` 给冷启动页面兜底。
- 可选自动化：`scripts/check_update.sh` 读你仓库的 GitHub Release，发现新 APK 就自动广播。
- 全自动（可选，默认关闭）：填好 `/opt/ai-chat-gateway/update.env` 后启用 `aichat-autoupdate.timer`，每天检查 Release：有新 tag 就拉网关代码并重启，同时把 Release 里的 APK 广播给本站客户端。想完全掌控升级节奏就不要开。

## 目录

```
server/       FastAPI + WebSocket 网关，static/app.html 是单页客户端
android/      APK 源码（无 Gradle，aapt2 + d8 + apksigner）
scripts/      install / update / push_update / check_update / make_keystore / systemd
.github/      CI：打 tag 自动构建并签名 APK，上传 Release
releases/     预构建 APK（示例）
```

## 开放性（三种接入方式，按成本递增）

1. **纯 HTTP**：`GET /api/agent/poll` 拉消息 + `POST /api/agent/send` 发消息，任何语言都能接。
2. **Webhook**：`gateway.env` 配 `AGENT_WEBHOOK`，网关把每条新消息 POST 给你的 agent。
3. **插件钩子**：`plugins/*.py` 的 `on_message` 在入库前改写消息，不用改主程序。

## 接入其他 Agent

任何能发 HTTP/WebSocket 的智能体都能接入，协议见 `docs/AGENT_API.md`，最小示例见 `examples/agent_client.py`：
注册 `kind:"agent"` → 用邀请码入群 → WS 收发消息。也兼容 AstrBot / OneBot v11（网关作为 OneBot 客户端连接）。

## 接口要点

- 鉴权：除 `/api/device`、`/api/version`、`/api/health`、`/api/status`、`/api/restore`、`/api/me`，全部要 `X-Device-Secret`；`device` 为 bot/ai 时还要 `BOT_TOKEN`。
- 身份：`uid` + 唯一身份码；`POST /api/restore {code, device_id}` 换机继承昵称/头像/会话/消息。
- 资料：`POST /api/profile`；群管理只认 `is_admin(uid)`。
- Agent 接入：`POST /api/convs/join {device:"bot",nick,invite}` + `BOT_TOKEN`，以普通成员身份进任意群。
- 推送：`POST /api/admin/push_update {version, apk_url, notes}` → 写 setting + WS 广播 `app_update`。

## 兼容性

服务端 Ubuntu 20.04+/Python 3.10+；客户端 minSdk 21、targetSdk 34；数据库启动自动迁移，升级前建议备份 `data/chat.db`。详细矩阵见 `COMPATIBILITY.md`。

## 安全提示

纯 HTTP 下浏览器禁止摄像头/麦克风/Service Worker；要语音视频必须 HTTPS。生产部署请前面挂 Nginx/Caddy 并开 TLS，`gateway.env` 永远 600。

## License

MIT

## 构建带你自己服务器地址的 APK

```bash
DEFAULT_SERVER=http://你的域名:8099 VERSION=v0.43 bash scripts/build_apk.sh
```

- 传了 `DEFAULT_SERVER`，安装后首启直连该地址，用户不用填
- 不传则生成通用包，首次启动会要求用户填写服务器地址
- 密钥默认读 `android/build/aichat.keystore`，可用 `KEYSTORE=/path/to.keystore` 指定
