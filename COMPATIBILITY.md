# 兼容性说明

## 服务端

| 项目 | 要求 / 说明 |
|---|---|
| 系统 | Ubuntu 20.04 / 22.04 已验证；Debian 11+ 可用；需 systemd 或 Docker |
| Python | 3.10+（Docker 镜像用 3.11） |
| 依赖 | fastapi、uvicorn、pillow、httpx、python-multipart；视频首帧需要 ffmpeg |
| 端口 | 默认 8099，可改；云厂商安全组需放行 |
| 数据 | 单文件 SQLite `data/chat.db`，启动自动迁移；升级前备份 |
| 并发 | 2C2G 可跑几十人小群；大群建议加 Nginx + 多 worker 前先压测 |
| 反向代理 | Nginx/Caddy 均可；WebSocket 必须转发 Upgrade 头 |

## 客户端（Android）

| 项目 | 要求 / 说明 |
|---|---|
| minSdk | 21（Android 5.0） |
| targetSdk | 34 |
| WebView | 用系统 WebView；建议 Android System WebView ≥ 100 |
| 相机拍照 | 走系统相机 Intent，纯 HTTP 也能用（不依赖 getUserMedia） |
| 语音/视频通话 | 已移除；如后续恢复需要 HTTPS 安全上下文 |
| 文件选择 / 下载 | 需存储权限；Android 13+ 用 READ_MEDIA_IMAGES |
| 签名 | 同一 keystore 才能覆盖安装；丢证书只能卸载重装（数据在服务端不受影响） |
| 版本号 | Manifest 必须显式 `versionCode`，否则部分 ROM 报“安装包损坏” |

## 版本间兼容

- 服务端：旧库自动补列（`conv`、`device` 允许原地加列）；不兼容表会改名留档，不会静默丢数据。
- 客户端：v25 起所有 `/api` 强制 `X-Device-Secret`，**≤v24 的旧 APK 会 403**，升级时需先让用户装新版。
- 身份：`uid + 身份码` 与设备解耦，换手机用恢复码即可继承；同一身份码只能属于一台设备，换机会迁移。
- 更新推送：`app_version` 存在 setting 表；客户端版本低于它才提示，不会反复弹。

## 已知限制

- 纯 HTTP 无法使用 Service Worker、摄像头流、WebRTC。
- SQLite 单机写入，超大规模（万级并发）不适合。
- 每会话保留 5000 条消息，超出按 seq 裁剪。
- QQ CDN 的过期图片链接可能裂图，建议机器人侧转存到本站 `/media`。
