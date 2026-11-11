#!/usr/bin/env python3
"""AIChat 轻量 agent 客户端。

只做一件事：把你本机上的 AI 接进一台已有的 AI Chat 网关，当一个普通成员聊天。
它不监听端口、不跑服务端，只需要能访问网关的 HTTP 地址（出站即可，适合家里电脑/内网/NAT 后面）。

用法示例：
  # 只注册设备，打印 uid 与 secret
  python aichat_agent.py --server http://1.2.3.4:8099 --device my-pc --register-only

  # 回声机器人，盯住自己的私聊 + 用邀请码加入的群
  python aichat_agent.py --server http://1.2.3.4:8099 --device my-pc --join TEST01

  # 接入本地大模型（任何 OpenAI 兼容接口：ollama / vLLM / one-api / LM Studio）
  python aichat_agent.py --server http://1.2.3.4:8099 --device my-pc \
      --llm-base http://127.0.0.1:11434/v1 --llm-model qwen2.5:7b --name "家里的AI"

配置文件也可以放在同目录 agent.json，命令行参数优先。
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_state.json")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Gateway:
    def __init__(self, base, device, secret=""):
        self.base = base.rstrip("/")
        self.device = device
        self.secret = secret

    def _req(self, method, path, body=None, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.secret:
            req.add_header("X-Device-Secret", self.secret)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            raise RuntimeError(f"{method} {path} -> HTTP {e.code} {detail}") from None

    def register(self, name="本机 agent"):
        return self._req("POST", "/api/device", {"device_id": self.device, "name": name, "kind": "agent"})

    def join(self, code):
        return self._req("POST", "/api/convs/join", {"device": self.device, "code": code})

    def convs(self):
        return self._req("GET", "/api/convs", query={"device": self.device})

    def poll(self, conv, after=0, limit=50):
        return self._req("GET", "/api/agent/poll",
                         query={"conv": conv, "device": self.device, "after": after, "limit": limit})

    def send(self, conv, text, reply_to=None, mentions=None):
        body = {"conv": conv, "device": self.device, "text": text}
        if reply_to:
            body["reply_to"] = int(reply_to)
        if mentions:
            body["mentions"] = [int(x) for x in mentions]
        return self._req("POST", "/api/agent/send", body)


class LLM:
    """任何 OpenAI 兼容 /chat/completions。"""

    def __init__(self, base, model, key="", system="你是接入聊天室的本机 AI，回答简洁、口语化，不超过 120 字。"):
        self.base = base.rstrip("/")
        self.model = model
        self.key = key
        self.system = system

    def reply(self, text, history=None):
        msgs = [{"role": "system", "content": self.system}]
        for m in (history or [])[-6:]:
            role = "user" if str(m.get("sender") or m.get("uid") or "") != str(uid) else "assistant"
            msgs.append({"role": role, "content": m.get("text") or ""})
        msgs.append({"role": "user", "content": text})
        req = urllib.request.Request(self.base + "/chat/completions",
                                     data=json.dumps({"model": self.model, "messages": msgs, "temperature": 0.6}).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        return (d["choices"][0]["message"]["content"] or "").strip()


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def text_of(msg):
    c = msg.get("content") or {}
    if isinstance(c, str):
        return c
    return c.get("text") or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="网关地址，如 http://1.2.3.4:8099")
    ap.add_argument("--device", help="设备 id，自己起一个稳定字符串")
    ap.add_argument("--name", default="本机 agent")
    ap.add_argument("--join", action="append", default=[], help="邀请码，可重复")
    ap.add_argument("--conv", action="append", default=[], help="额外监听的会话 id，可重复")
    ap.add_argument("--llm-base", default="")
    ap.add_argument("--llm-model", default="")
    ap.add_argument("--llm-key", default="")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--register-only", action="store_true")
    args = ap.parse_args()

    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.json")
    cfg = load_state(cfg_path) if os.path.exists(cfg_path) else {}
    for k, v in (("server", args.server), ("device", args.device), ("name", args.name),
                 ("llm_base", args.llm_base), ("llm_model", args.llm_model), ("llm_key", args.llm_key)):
        if v:
            cfg[k] = v
    server = (cfg.get("server") or "").rstrip("/")
    device = cfg.get("device") or ""
    if not server or not device:
        ap.error("至少需要 --server 和 --device（或写进 agent.json）")

    state = load_state(args.state)
    gw = Gateway(server, device, state.get("secret", ""))
    prof = gw.register(cfg.get("name") or args.name)
    if prof.get("secret"):
        state["secret"] = prof["secret"]
        state["uid"] = prof.get("uid")
        save_state(args.state, state)
        gw.secret = prof["secret"]
    uid = state.get("uid") or prof.get("uid")
    log(f"已接入 {server} · uid={uid} · device={device}")
    if args.register_only:
        print(json.dumps({"uid": uid, "device": device, "secret": state.get("secret")}, ensure_ascii=False))
        return

    watch = set(args.conv or [])
    watch.add("u" + str(uid))          # 自己与网关 AI 的私聊
    for code in (args.join or []):
        try:
            r = gw.join(code)
            cid = r.get("conv") or r.get("id") or r.get("cid")
            if cid:
                watch.add(cid)
                log(f"已加入 {cid}")
        except Exception as e:
            log("加入失败：", e)

    try:
        for c in (gw.convs() or {}).get("items", []):
            cid = c.get("id") or ""
            members = c.get("members") or []
            if cid.startswith("g") and str(uid) in [str(x) for x in members]:
                watch.add(cid)
    except Exception:
        pass
    log("监听会话：", ", ".join(sorted(watch)))

    llm = LLM(cfg["llm_base"], cfg.get("llm_model") or "", cfg.get("llm_key") or "") if cfg.get("llm_base") else None
    after = {c: int(state.get("seq", {}).get(c, 0)) for c in watch}
    seen = set()

    while True:
        for conv in sorted(watch):
            try:
                d = gw.poll(conv, after.get(conv, 0))
            except Exception as e:
                log(conv, "拉取失败：", e)
                if "403" in str(e):
                    watch.discard(conv)
                continue
            items = d.get("items") or []
            for m in items:
                after[conv] = max(after.get(conv, 0), int(m.get("seq") or 0))
                key = (conv, m.get("seq"))
                if key in seen:
                    continue
                seen.add(key)
                sender = str(m.get("sender") or m.get("uid") or "")
                if sender == str(uid) or m.get("type") in ("system",):
                    continue
                t = text_of(m).strip()
                if not t:
                    continue
                log(f"[{conv}] {m.get('name') or m.get('uid')}: {t[:80]}")
                try:
                    if llm:
                        ans = llm.reply(t)
                    else:
                        ans = f"收到：{t[:200]}"
                    if ans:
                        gw.send(conv, ans, reply_to=m.get("seq"))
                        log("已回复", len(ans), "字")
                except Exception as e:
                    log("回复失败：", e)
            state.setdefault("seq", {})[conv] = after.get(conv, 0)
        save_state(args.state, state)
        time.sleep(max(args.interval, 0.5) + random.random() * 0.3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
    except Exception as e:
        print("启动失败：", e)
        sys.exit(1)
