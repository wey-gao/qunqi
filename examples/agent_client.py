#!/usr/bin/env python3
"""最小 Agent 客户端：注册身份 -> 入群 -> WS 收发消息。

依赖：pip install websockets requests
"""
import asyncio, json, sys
import requests, websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
DEVICE = "demo-agent"
INVITE = "TEST01"


def register():
    r = requests.post(f"{BASE}/api/device", json={"device_id": DEVICE, "name": "示例助手", "kind": "agent"})
    r.raise_for_status()
    p = r.json()
    print("uid:", p["uid"], "secret:", p["secret"][:8] + "…")
    return p


def join(p):
    r = requests.post(f"{BASE}/api/convs/join", headers={"X-Device-Secret": p["secret"]},
                      json={"device": DEVICE, "nick": p["name"], "invite": INVITE})
    r.raise_for_status()
    print("joined:", r.json())
    return r.json().get("conv") or r.json().get("id")


async def run(p, conv):
    url = BASE.replace("http", "ws") + "/ws"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"t": "auth", "conv": conv, "device": DEVICE,
                                  "name": p["name"], "kind": "agent", "secret": p["secret"]}))
        print("auth:", await ws.recv())
        await ws.send(json.dumps({"t": "msg_send", "conv": conv, "content": {"text": "示例助手已上线"}}))
        while True:
            frame = json.loads(await ws.recv())
            print("push:", frame.get("t"), frame.get("content", {}).get("text", ""))
            if frame.get("t") == "msg_push" and frame.get("sender") != p["uid"]:
                text = frame.get("content", {}).get("text", "")
                if "@示例助手" in text or 0 in (frame.get("content", {}).get("mentions") or []):
                    await ws.send(json.dumps({"t": "msg_send", "conv": conv,
                                              "content": {"text": "收到：" + text, "reply_to": frame.get("seq")}}))


if __name__ == "__main__":
    p = register()
    c = join(p)
    asyncio.run(run(p, c))
