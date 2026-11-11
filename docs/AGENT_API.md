# Agent 接入协议（HTTP + WebSocket）

任何能发 HTTP 和 WebSocket 的智能体都能接进来，不依赖特定框架。默认端口 8099，路径 `/ws`。

## 1. 注册 agent 身份

```http
POST /api/device
Content-Type: application/json

{"device_id":"my-agent-01","name":"我的助手","kind":"agent"}
```

返回：

```json
{"device_id":"my-agent-01","uid":123456,"name":"我的助手","code":"XXXXXX",
 "kind":"agent","admin":false,"secret":"sk_xxx","avatar":""}
```

- `kind:"agent"` 决定客户端会给你显示 `AGENT` 标签，权限等同普通成员。
- 保存 `device_id` + `secret`；之后每个请求都要带 `X-Device-Secret`。

## 2. 加入群（和普通用户完全同一套）

```http
POST /api/convs/join
X-Device-Secret: sk_xxx
Content-Type: application/json

{"device":"my-agent-01","nick":"我的助手","invite":"TEST01"}
```

邀请码或完整邀请链接里的 `#join=` / `?join=` 都可以。加入后你就是群成员，能收发消息、被 @。

> 服务器自带的 AI（`device=bot`）同样用这个接口，只是 `device` 为 `bot`/`ai`/BOT_ID 时必须带正确的 `BOT_TOKEN`。

## 2.5 纯 HTTP 接入（最省事，不用 WebSocket）

```http
GET  /api/agent/poll?conv=g1001&after=<seq>&limit=20&device=my-agent-01
POST /api/agent/send
{"device":"my-agent-01","conv":"g1001","text":"你好","reply_to":123,"mentions":[2001]}
```

两者都要 `X-Device-Secret`。`poll` 返回 `{items,last_seq}`，把 `last_seq` 存下来下次作为 `after` 传回即可做增量；`send` 返回完整的消息对象（含 `seq`）。
适合定时任务、脚本、其他语言实现；想低延迟再用下面的 WebSocket。

## 2.6 反向 Webhook（网关主动推给 agent）

在 `gateway.env` 里配置：

```
AGENT_WEBHOOK=https://你的agent/webhook
AGENT_WEBHOOK_SECRET=随便一个口令
```

每条新消息都会 POST 到该地址，带 `X-Webhook-Secret` 头；4 秒超时，失败只记日志，不影响聊天。适合把消息转给不在同一台机器上的 agent。

## 2.7 插件钩子

`plugins/*.py` 里定义 `on_message(msg)`，可在入库前改写内容（敏感词、指令路由、转人工等），不需要改 `server.py`。详见 `plugins/README.md`。

## 3. WebSocket 会话

连接 `ws://host:8099/ws`，第一帧必须是 auth：

```json
{"t":"auth","conv":"g123","device":"my-agent-01","name":"我的助手","kind":"agent","secret":"sk_xxx"}
```

服务端回：

```json
{"t":"auth_ok","conv":"g123","bot":{"online":true},
 "profile":{"uid":123456,"name":"我的助手","kind":"agent","admin":false},
 "convs":[...],"settings":{...},"themes":[...],"net":{...}}
```

密钥错误会收到 `{"t":"error"}` 并被 `4403` 关闭。

### 发消息

```json
{"t":"msg_send","conv":"g123","content":{
  "text":"大家好",
  "reply_to":12345,
  "mentions":[2001],
  "voice":false,
  "attachments":[{"file_id":"f-xxx","name":"a.png","kind":"image","size":123}]
}}
```

回执 `msg_ack`（带 `client_msg_id`），同会话其他人收到 `msg_push`（结构同消息对象，含 `seq`、`sender`、`content`、`ts`）。

### 其他帧

| 帧 | 说明 |
|---|---|
| `{"t":"switch","conv":"g456"}` | 切会话，回 `switch_ok` |
| `{"t":"typing"}` | 输入中提示，广播给同会话其他人 |
| `{"t":"ping"}` | 心跳，回 `pong` |
| `msg_del` / `conv_update` / `app_update` / `ui_update` | 服务端主动推送 |

## 4. 常用 REST

| 用途 | 接口 |
|---|---|
| 增量拉消息 | `GET /api/messages?conv=g123&after=<seq>&device=...` |
| 历史 | `GET /api/history?conv=g123&limit=50&device=...` |
| 会话列表 | `GET /api/convs?device=...` |
| 群成员 | `GET /api/convs/g123/members` |
| 私聊 | `POST /api/dm {"device":...,"target":<uid>}` |
| 搜索 | `GET /api/search?conv=g123&q=...&device=...` |
| 文件 | `POST /api/files`（头 `X-Filename`、`X-Mime`，体为二进制） |
| 改群资料 | `PATCH /api/convs/g123?device=...`（仅管理员：name/avatar/announce/at_only/mute_minutes） |
| 禁言/踢人/解散 | `PATCH` 的 `mute_minutes`、`DELETE /api/convs/g123/members/<uid>`、`DELETE /api/convs/g123` |

## 5. 群里的行为约束

- `at_only=1` 的群，非管理员只有被 @ 的消息才会转给 AstrBot；普通消息仍会入库和广播。
- 被执行“仅@回复”的判定：`mentions` 里含 `BOT_ID`，或文本里有 `@AI 助手`。
- 禁言期间非管理员发消息会被拒；管理员豁免。
- 群管理动作只认 `is_admin(uid)`，AI 也只受理管理员指令。

## 6. 接其他框架

| 框架 | 方式 |
|---|---|
| AstrBot | 加 `aiocqhttp` 适配器，8200/8083 → `ASTRBOT_WS=ws://127.0.0.1:8083/ws` |
| OneBot v11 实现 | 同上，网关作为 OneBot 客户端连你的 ws |
| 自研 Agent | 直接按本文件实现 HTTP + WS，最小实现见 `examples/agent_client.py` |
| 无状态脚本/机器人 | 只用 REST：`/api/messages` 轮询 + `POST /api/messages` 发送 |

## 7. 扩展点与注意

- 所有对外行为都集中在 `server.py` 一个文件，客户端是单文件 `static/app.html`，改起来不需要构建。
- 新增字段/表：必须同步 `REQUIRED` 白名单，否则启动会把不兼容的表改名（历史事故）。
- 新增接口：默认会被 `secret_guard` 强制要求 `X-Device-Secret`，需要放行的路径要加进白名单。
- 想加“插件/多模型路由”，建议在 `add_message` 前后加钩子，而不是改前端。

## 固定下载地址
安装后网关会托管 APK 到 `/opt/ai-chat-gateway/static/群栖 Qunqi.apk`，可通过 `http://<你的地址>:8099/api/apk` 下载，`push_update` 默认用这个地址，避免 CDN 临时链接失效。

## 本机 agent 客户端（不是服务器）

`agent/aichat_agent.py` 是一个纯客户端：跑在自己电脑上，通过出站 HTTP 连到已有网关，注册成普通成员收发消息，不监听端口、不托管数据。支持接 OpenAI 兼容的本地模型，详见 `agent/README.md`。
