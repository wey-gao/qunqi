# 插件目录

把 `*.py` 丢进 `/opt/ai-chat-gateway/plugins/`，重启网关即生效，不需要改 `server.py`。

约定：模块里定义 `on_message(msg)`，返回 `{"content": {...}}` 可改写消息内容；返回 `None` 表示不改。异常会被捕获，不影响聊天。

`msg` 结构：

```python
{"conv": "g1001", "sender": "1001", "role": "user",
 "type": "text", "content": {"text": "...", "mentions": [2001]}}
```

典型用法：敏感词过滤、指令路由、把某些消息转给外部 agent、给 AI 消息加前缀等。

示例见 `sample_filter.py.example`（改名去掉 `.example` 才加载）。
