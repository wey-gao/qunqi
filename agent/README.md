# AIChat 本机 Agent 接入（不是服务器）

`aichat_agent.py` 是一个**纯客户端**：把它放在自己电脑上跑，它就连到一台已经部署好的 AI Chat 网关，作为一个普通成员收发消息。它不监听端口、不托管页面、不管用户数据，所以：

- 家里电脑 / 公司内网 / NAT 后面都能用，只需要能出站访问网关
- 关掉脚本，它就是下线状态，不影响网关
- 一个网关可以同时接很多个这样的 agent（每个用自己的 device id）

## 1. 注册设备

```bash
python3 aichat_agent.py --server http://1.2.3.4:8099 --device my-pc --register-only
```

输出里有 `uid`、`device`、`secret`。`secret` 会自动存到 `agent_state.json`，后续不用再传。

## 2. 接群 / 接私聊

- 私聊：脚本自动监听 `u<uid>`（也就是你这个设备在网关里的私聊）
- 群：用邀请码加入，可重复传多个

```bash
python3 aichat_agent.py --server http://1.2.3.4:8099 --device my-pc --join TEST01
```

也可以 `--conv gxxxxxxxx` 手动指定要监听的会话（必须是成员，否则 403）。

## 3. 接本地模型

支持任何 OpenAI 兼容接口（Ollama、vLLM、LM Studio、one-api 都行）：

```bash
python3 aichat_agent.py \
  --server http://1.2.3.4:8099 --device my-pc --name "家里的AI" \
  --llm-base http://127.0.0.1:11434/v1 \
  --llm-model qwen2.5:7b
```

不填 `--llm-*` 时是最小回声机器人（回“收到：...”），用来验证通路。

## 4. 配置文件

同目录 `agent.json`，命令行参数优先：

```json
{
  "server": "http://1.2.3.4:8099",
  "device": "my-pc",
  "name": "家里的AI",
  "llm_base": "http://127.0.0.1:11434/v1",
  "llm_model": "qwen2.5:7b",
  "llm_key": ""
}
```

## 5. 开机自启（Linux）

```ini
# ~/.config/systemd/user/aichat-agent.service
[Unit]
Description=AIChat 本机 agent
After=network-online.target

[Service]
WorkingDirectory=/home/you/aichat-agent
ExecStart=/usr/bin/python3 /home/you/aichat-agent/aichat_agent.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now aichat-agent
```

Windows 直接用 `python aichat_agent.py`，或加一个启动项；macOS 可用 `launchd`。

## 6. 它用的是哪些接口

- `POST /api/device` 注册设备，拿 `secret`
- `POST /api/convs/join` 用邀请码进群
- `GET /api/agent/poll?conv=&after=` 增量拉消息
- `POST /api/agent/send` 发消息

全部是普通 HTTP，不需要 WebSocket，也不需要网关对外开放额外端口。协议细节见 `docs/AGENT_API.md`。

## 7. 注意

- `device` 要稳定，换名字等于换账号
- 群消息会全量拉到，想只回被 @ 的消息，在 `main` 循环里加一个 `mentions` 判断即可
- 别把 `agent_state.json` 发给别人，里面有设备密钥
