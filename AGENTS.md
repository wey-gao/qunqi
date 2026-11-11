# Agent 操作手册（给别人部署用）

目标：把本仓库部署成一台独立私域聊天服务器，并把 APK 交给部署者分发。**不要连接任何中心服务器**。

步骤：
1. 读 `README.md`；确认目标机 Ubuntu 20.04+ / Python 3.10+ / 可用端口 / sudo。
2. `bash scripts/install.sh`，保存输出的站点地址、管理员码、`BOT_TOKEN`。
3. 自检：`/api/health` 为 `{"ok":true}`；`POST /api/device` 能拿到 `secret`。
4. APK：优先用 `releases/` 里已签名的包；需要自建就用 `scripts/make_keystore.sh` + `android/BUILD.md`。
5. 部署者把 APK 传到自己的存储，运行 `scripts/push_update.sh <版本> <直链> <说明>` 给本站用户广播。
6. 网关升级：`scripts/update.sh`（拉仓库最新代码，保留 `gateway.env` 与 `data/`）。
7. 任何破坏性操作前备份 `data/chat.db`。

禁止：把 `BOT_TOKEN`、设备密钥、签名私钥写进仓库或日志；把某一个人的服务器地址硬编码进代码。
