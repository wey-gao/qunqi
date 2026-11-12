"""AI Chat Gateway v0.3.0
=======================

NapCat 式网关：与 agent（AstrBot 等）同机部署，对外提供一个公网可达的
HTTP + WebSocket 入口给 App 使用。App 不需要账号密码，只用本地生成的匿名
device id 识别身份；群聊可凭邀请码公开创建与加入；AI 可创建 / 改名 / 拉人 /
踢人 / 禁言 / 发公告。

适配层（AGENT_MODE）：
  - onebot  : 本进程扮演 OneBot v11 协议端，连 AstrBot 的反向 WS（默认）
  - openai  : 直接对接任意 OpenAI 兼容 /chat/completions
  - webhook : POST 到自建 agent，返回 {text, actions}

启动：
  python3 server.py            # 默认 0.0.0.0:8099
  AGENT_MODE=onebot ASTRBOT_WS=ws://127.0.0.1:6199/ws python3 server.py
"""

import asyncio
import hashlib
import json
import os
import pathlib
import re
import secrets
import threading
import socket
import importlib.util
import sqlite3
import string
import time
import uuid
import base64
import urllib.parse
from urllib.parse import quote, unquote

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse

BASE = pathlib.Path(__file__).resolve().parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
CHUNKS = DATA / "chunks"
THUMBS = DATA / "thumbs"
PLUGIN_PATH = BASE / "plugins"
STATIC = BASE / "static"
for p in (DATA, UPLOADS, THUMBS, CHUNKS, PLUGIN_PATH):
    p.mkdir(parents=True, exist_ok=True)
DB = DATA / "chat.db"

AGENT_MODE = os.getenv("AGENT_MODE", "onebot").lower()
ASTRBOT_WS = os.getenv("ASTRBOT_WS", "ws://127.0.0.1:6199/ws")
ASTRBOT_TOKEN = os.getenv("ASTRBOT_TOKEN", "")
BOT_ID = int(os.getenv("BOT_ID", "2001"))
ADMIN_UIDS = {int(x) for x in os.getenv("ADMIN_UIDS", "").replace(" ", "").split(",") if x.isdigit()}
ADMIN_CODES = {x.strip().upper() for x in os.getenv("ADMIN_CODES", "").split(",") if x.strip()}
BOT_NAME = os.getenv("BOT_NAME", "AI 助手")
OPENAI_BASE = os.getenv("OPENAI_BASE", "")
OPENAI_KEY = os.getenv("OPENAI_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
AGENT_WEBHOOK = os.getenv("AGENT_WEBHOOK", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "").rstrip("/")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
# 安全强度开关：自用默认宽松，对外公开时设 SECURITY_LEVEL=public 全开
SECURITY_LEVEL = os.getenv("SECURITY_LEVEL", "private").strip().lower()
PUBLIC_MODE = SECURITY_LEVEL == "public"
STRICT_MAGIC = os.getenv("STRICT_FILE_MAGIC", "1" if PUBLIC_MODE else "0") == "1"
ADMIN_CONFIRM = os.getenv("ADMIN_CONFIRM", "1" if PUBLIC_MODE else "0") == "1"
PUSH_REQUIRE_SECRET = os.getenv("PUSH_REQUIRE_SECRET", "1" if PUBLIC_MODE else "0") == "1"

MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "300")) * 1024 * 1024
MAX_TOTAL = int(os.getenv("MAX_TOTAL_MB", "2048")) * 1024 * 1024
UPLOAD_PER_MIN = int(os.getenv("UPLOAD_PER_MIN", "20"))
_up_rl: dict = {}

app = FastAPI(title="AI Chat Gateway")

_RST_GLOBAL = [0, 0]
SECRET_EXEMPT = {"/api/apk", "/api/device", "/api/version", "/api/health", "/api/status", "/api/restore"}
DOC_EXEMPT_PREFIX = "/api/docs/"


@app.middleware("http")
async def diag_log(request: Request, call_next):
    resp = await call_next(request)
    try:
        if os.getenv("DIAG_LOG", "0") == "1" and request.url.path in ("/api/me", "/api/restore", "/api/convs", "/api/messages"):
            dev = request.query_params.get("device", "")
            line = "%d %s dev=%s sec=%s -> %d\n" % (int(time.time()), request.url.path, dev,
                                                     "1" if request.headers.get("x-device-secret") else "0", resp.status_code)
            with open(DATA / "diag.log", "a") as f:
                f.write(line)
    except Exception:
        pass
    return resp


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    try:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self' data: blob:; img-src * data: blob:; media-src * data: blob:; "
                "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src *")
    except Exception:
        pass
    return resp


@app.middleware("http")
async def secret_guard(request: Request, call_next):
    """知道 device id 不够，还得有设备密钥；AI 的 device=bot 额外要 BOT_TOKEN。"""
    path = request.url.path
    if not path.startswith("/api") or path in SECRET_EXEMPT or path.startswith(DOC_EXEMPT_PREFIX):
        return await call_next(request)
    if path == "/api/me" and not PUBLIC_MODE:
        # 自用模式：缺密钥也能读资料，但接口不返回 secret
        return await call_next(request)
    device = request.query_params.get("device") or ""
    secret = request.headers.get("x-device-secret") or request.query_params.get("secret", "")
    raw = None
    if not device and request.method in ("POST", "PATCH", "DELETE"):
        raw = await request.body()
        try:
            device = str((json.loads(raw) or {}).get("device") or "")
        except Exception:
            device = ""
        async def replay(body=raw):
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = replay
        request._body = raw
    if device:
        touch_device(device)
        if device in ("bot", "BOT", "ai", "AI", str(BOT_ID)):
            if not BOT_TOKEN:
                return JSONResponse({"detail": "服务器未配置 BOT_TOKEN，禁止使用 AI 身份"}, status_code=403)
            if secret != BOT_TOKEN:
                return JSONResponse({"detail": "AI 口令不正确"}, status_code=403)
        else:
            stored = secret_of(device)
            if not stored:
                return JSONResponse({"detail": "设备未注册，请先调用 /api/device"}, status_code=403)
            if secret != stored:
                return JSONResponse({"detail": "设备密钥不正确"}, status_code=403)
    return await call_next(request)

from fastapi.middleware.cors import CORSMiddleware
_orig = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
if not _orig:
    _orig = [o for o in [PUBLIC_BASE, "http://127.0.0.1:8099", "http://localhost:8099"] if o]
app.add_middleware(CORSMiddleware, allow_origins=_orig,
                   allow_origin_regex=r"https://[a-z0-9-]+\.trycloudflare\.com",
                   allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                   allow_headers=["*"], allow_credentials=False)


@app.middleware("http")
async def token_guard(request: Request, call_next):
    """GATEWAY_TOKEN 留空=完全开放（默认）；设置了就要求所有接口带 token。"""
    if GATEWAY_TOKEN and request.url.path.startswith(("/api", "/media")) and not check_token(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "token required", "how": "?token= 或 X-Gateway-Token"}, status_code=401)
    return await call_next(request)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
             ".avif", ".jfif", ".ico"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".amr", ".aiff"}
DOC_EXT = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".markdown", ".csv", ".tsv",
           ".json", ".jsonl", ".log", ".xml", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
           ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".vue", ".html", ".css", ".scss",
           ".sql", ".sh", ".bash", ".bat", ".ps1", ".java", ".go", ".rs", ".php", ".rb",
           ".swift", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".r", ".lua", ".pl", ".tex"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
LEGACY_OFFICE = {".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods", ".odp", ".epub"}

THEMES = [
    {
        "id": "light_clean",
        "name": "纯白简约",
        "v": {
            "bg": "#f2f3f5",
            "panel": "#ffffff",
            "panel2": "#f4f5f7",
            "line": "#e8eaed",
            "ink": "#111318",
            "dim": "#828893",
            "accent": "#12b7f5",
            "user": "#e8f5ff",
            "ai": "#ffffff",
            "style": "solid",
            "tex": "none",
            "bubble": "round",
            "font": "sans",
            "frame": "hairline",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#0e92c4",
            "ubb2": "#269dca"
        }
    },
    {
        "id": "mono_bw",
        "name": "纯黑白",
        "v": {
            "bg": "#000000",
            "panel": "#0e0e0e",
            "panel2": "#1a1a1a",
            "line": "#2b2b2b",
            "ink": "#f5f5f5",
            "dim": "#9a9a9a",
            "accent": "#ffffff",
            "user": "#262626",
            "ai": "#111111",
            "style": "outline",
            "tex": "none",
            "bubble": "sharp",
            "font": "sans",
            "frame": "bold",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#858585",
            "ubb2": "#919191"
        }
    },
    {
        "id": "dark_ink",
        "name": "深色墨黑",
        "v": {
            "bg": "#121417",
            "panel": "#1b1f25",
            "panel2": "#232830",
            "line": "#2c323b",
            "ink": "#e8ecf1",
            "dim": "#8f98a4",
            "accent": "#3fb8a0",
            "user": "#2a3a3a",
            "ai": "rgba(35,40,48,.92)",
            "style": "solid",
            "tex": "grain",
            "bubble": "round",
            "font": "sans",
            "frame": "hairline",
            "deco": "ticks",
            "ubtext": "#ffffff",
            "ubb": "#359b86",
            "ubb2": "#49a592"
        }
    },
    {
        "id": "midnight",
        "name": "午夜蓝",
        "v": {
            "bg": "#0b1220",
            "panel": "#121b2b",
            "panel2": "#1a2740",
            "line": "#243754",
            "ink": "#e2ecff",
            "dim": "#8398bb",
            "accent": "#5b8cff",
            "user": "#1c3358",
            "ai": "rgba(18,27,43,.92)",
            "style": "solid",
            "tex": "aurora",
            "bubble": "round",
            "font": "sans",
            "frame": "neon",
            "deco": "ticks",
            "ubtext": "#ffffff",
            "ubb": "#5786f5",
            "ubb2": "#6892f6"
        }
    },
    {
        "id": "ocean",
        "name": "深海",
        "v": {
            "bg": "#071318",
            "panel": "#0c1d24",
            "panel2": "#12292f",
            "line": "#1d3b44",
            "ink": "#dbedf2",
            "dim": "#7fa3ad",
            "accent": "#38b6c4",
            "user": "#123a44",
            "ai": "rgba(12,29,36,.92)",
            "style": "glass",
            "tex": "aurora",
            "bubble": "round",
            "font": "sans",
            "frame": "neon",
            "deco": "corners",
            "ubtext": "#ffffff",
            "ubb": "#2f99a5",
            "ubb2": "#44a3ae"
        }
    },
    {
        "id": "forest",
        "name": "森林暗夜",
        "v": {
            "bg": "#0d1512",
            "panel": "#131f1a",
            "panel2": "#1b2b24",
            "line": "#273c33",
            "ink": "#dfeee6",
            "dim": "#87a394",
            "accent": "#4fbf82",
            "user": "#1e3a2c",
            "ai": "rgba(22,36,30,.92)",
            "style": "solid",
            "tex": "grain",
            "bubble": "round",
            "font": "serif",
            "frame": "hairline",
            "deco": "ticks",
            "ubtext": "#ffffff",
            "ubb": "#3f9968",
            "ubb2": "#52a377"
        }
    },
    {
        "id": "mint",
        "name": "薄荷轻氧",
        "v": {
            "bg": "#eef6f2",
            "panel": "#ffffff",
            "panel2": "#e6f2ec",
            "line": "#d6e6de",
            "ink": "#1d2b26",
            "dim": "#728a80",
            "accent": "#28c08a",
            "user": "#d9f5ea",
            "ai": "#ffffff",
            "style": "solid",
            "tex": "dots",
            "bubble": "round",
            "font": "round",
            "frame": "hairline",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#209a6e",
            "ubb2": "#36a47c"
        }
    },
    {
        "id": "cyber_blue",
        "name": "赛博霓虹",
        "v": {
            "bg": "#070b16",
            "panel": "#0d1526",
            "panel2": "#14203a",
            "line": "#1e3356",
            "ink": "#d8e6ff",
            "dim": "#7d92b8",
            "accent": "#31c7ff",
            "user": "#163a5a",
            "ai": "rgba(14,26,46,.92)",
            "style": "neon",
            "tex": "grid",
            "bubble": "glow",
            "font": "mono",
            "frame": "neon",
            "deco": "hud",
            "ubtext": "#ffffff",
            "ubb": "#2597c2",
            "ubb2": "#3ba1c8"
        }
    },
    {
        "id": "retro_crt",
        "name": "复古CRT",
        "v": {
            "bg": "#0a0a0a",
            "panel": "#101010",
            "panel2": "#181818",
            "line": "#25331f",
            "ink": "#b9ffb0",
            "dim": "#6f8f68",
            "accent": "#39ff14",
            "user": "#1d2a18",
            "ai": "rgba(10,14,10,.94)",
            "style": "neon",
            "tex": "scan",
            "bubble": "sharp",
            "font": "mono",
            "frame": "neon",
            "deco": "scan",
            "ubtext": "#ffffff",
            "ubb": "#24a30d",
            "ubb2": "#3aac25"
        }
    },
    {
        "id": "terminal",
        "name": "终端绿屏",
        "v": {
            "bg": "#050805",
            "panel": "#0a120a",
            "panel2": "#0f1a0f",
            "line": "#1b2f1b",
            "ink": "#9dffa8",
            "dim": "#5f8a63",
            "accent": "#24d16a",
            "user": "#12251a",
            "ai": "rgba(8,16,9,.94)",
            "style": "outline",
            "tex": "scan",
            "bubble": "sharp",
            "font": "mono",
            "frame": "hairline",
            "deco": "ticks",
            "ubtext": "#ffffff",
            "ubb": "#1b9f51",
            "ubb2": "#32a962"
        }
    },
    {
        "id": "glass",
        "name": "玻璃拟态",
        "v": {
            "bg": "#eef1f7",
            "panel": "rgba(255,255,255,.62)",
            "panel2": "rgba(255,255,255,.44)",
            "line": "rgba(120,140,180,.36)",
            "ink": "#1b2331",
            "dim": "#6c7a90",
            "accent": "#6a8dff",
            "user": "rgba(106,141,255,.22)",
            "ai": "rgba(255,255,255,.76)",
            "style": "glass",
            "tex": "aurora",
            "bubble": "round",
            "font": "sans",
            "frame": "glass",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#6687f5",
            "ubb2": "#7593f6"
        }
    },
    {
        "id": "paper",
        "name": "纸感手作",
        "v": {
            "bg": "#f3eee2",
            "panel": "#faf7ee",
            "panel2": "#f0e9d8",
            "line": "#e0d5bd",
            "ink": "#3a3226",
            "dim": "#8b7f6a",
            "accent": "#b5763c",
            "user": "#efe0c8",
            "ai": "#fffdf6",
            "style": "paper",
            "tex": "paper",
            "bubble": "round",
            "font": "serif",
            "frame": "stitch",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#b5763c",
            "ubb2": "#bc8450"
        }
    },
    {
        "id": "sepia",
        "name": "旧照片",
        "v": {
            "bg": "#efe4cf",
            "panel": "#f7eeda",
            "panel2": "#e8dbc4",
            "line": "#d6c3a4",
            "ink": "#4a3a26",
            "dim": "#8d7a5e",
            "accent": "#96703a",
            "user": "#e3cfae",
            "ai": "#fbf4e6",
            "style": "paper",
            "tex": "grain",
            "bubble": "round",
            "font": "serif",
            "frame": "stitch",
            "deco": "seal",
            "ubtext": "#ffffff",
            "ubb": "#96703a",
            "ubb2": "#a07e4e"
        }
    },
    {
        "id": "lavender",
        "name": "薰衣草",
        "v": {
            "bg": "#f3f0fb",
            "panel": "#ffffff",
            "panel2": "#ece6f9",
            "line": "#ded4f2",
            "ink": "#2c2444",
            "dim": "#7b7197",
            "accent": "#8b6fd4",
            "user": "#e9e0fb",
            "ai": "#ffffff",
            "style": "solid",
            "tex": "dots",
            "bubble": "round",
            "font": "round",
            "frame": "hairline",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#8b6fd4",
            "ubb2": "#977dd8"
        }
    },
    {
        "id": "pastel",
        "name": "马卡龙",
        "v": {
            "bg": "#f6f1f8",
            "panel": "#ffffff",
            "panel2": "#f1e6f5",
            "line": "#e3d3ee",
            "ink": "#3a2a44",
            "dim": "#8a7695",
            "accent": "#e58fc4",
            "user": "#fbe1f1",
            "ai": "#ffffff",
            "style": "solid",
            "tex": "dots",
            "bubble": "round",
            "font": "round",
            "frame": "hairline",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#b7729d",
            "ubb2": "#be80a7"
        }
    },
    {
        "id": "sakura",
        "name": "樱花粉",
        "v": {
            "bg": "#fdf1f4",
            "panel": "#ffffff",
            "panel2": "#fae3e9",
            "line": "#f2ccd6",
            "ink": "#40232b",
            "dim": "#946e78",
            "accent": "#e0738c",
            "user": "#fbe0e6",
            "ai": "#ffffff",
            "style": "solid",
            "tex": "none",
            "bubble": "round",
            "font": "round",
            "frame": "hairline",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#ce6a81",
            "ubb2": "#d3798e"
        }
    },
    {
        "id": "sand",
        "name": "沙丘米",
        "v": {
            "bg": "#f5f1e8",
            "panel": "#fdfbf5",
            "panel2": "#eee8da",
            "line": "#ddd6c6",
            "ink": "#2e2a24",
            "dim": "#8a8172",
            "accent": "#c08a3e",
            "user": "#efe2c9",
            "ai": "#ffffff",
            "style": "solid",
            "tex": "paper",
            "bubble": "round",
            "font": "sans",
            "frame": "hairline",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#b17f39",
            "ubb2": "#b98c4d"
        }
    },
    {
        "id": "nord",
        "name": "北欧灰",
        "v": {
            "bg": "#eceff2",
            "panel": "#ffffff",
            "panel2": "#e5eaee",
            "line": "#d7dee4",
            "ink": "#232a30",
            "dim": "#7a858f",
            "accent": "#5a879e",
            "user": "#e2eef4",
            "ai": "#ffffff",
            "style": "outline",
            "tex": "none",
            "bubble": "sharp",
            "font": "sans",
            "frame": "bold",
            "deco": "none",
            "ubtext": "#ffffff",
            "ubb": "#5a879e",
            "ubb2": "#6a93a8"
        }
    },
    {
        "id": "coffee",
        "name": "咖啡馆",
        "v": {
            "bg": "#241a14",
            "panel": "#2f231b",
            "panel2": "#3a2b21",
            "line": "#4a372a",
            "ink": "#f2e6d6",
            "dim": "#b09a85",
            "accent": "#c98a4b",
            "user": "#42302a",
            "ai": "rgba(47,35,27,.94)",
            "style": "paper",
            "tex": "grain",
            "bubble": "round",
            "font": "serif",
            "frame": "stitch",
            "deco": "seal",
            "ubtext": "#ffffff",
            "ubb": "#b97f45",
            "ubb2": "#c08c58"
        }
    },
    {
        "id": "ink_wash",
        "name": "水墨",
        "v": {
            "bg": "#f1f0ec",
            "panel": "#fbfaf7",
            "panel2": "#eceae4",
            "line": "#dcd9d1",
            "ink": "#1a1a18",
            "dim": "#7c7b76",
            "accent": "#3b4a4a",
            "user": "#e7e5de",
            "ai": "#ffffff",
            "style": "paper",
            "tex": "paper",
            "bubble": "sharp",
            "font": "serif",
            "frame": "double",
            "deco": "seal",
            "ubtext": "#ffffff",
            "ubb": "#3b4a4a",
            "ubb2": "#4f5c5c"
        }
    },
    {
        "id": "sunset",
        "name": "日落橙",
        "v": {
            "bg": "#24140f",
            "panel": "#33201a",
            "panel2": "#432a22",
            "line": "#5a3930",
            "ink": "#ffe9dc",
            "dim": "#c1998a",
            "accent": "#ff7a45",
            "user": "#4d2a20",
            "ai": "rgba(51,32,26,.94)",
            "style": "neon",
            "tex": "aurora",
            "bubble": "glow",
            "font": "sans",
            "frame": "neon",
            "deco": "corners",
            "ubtext": "#ffffff",
            "ubb": "#e06b3d",
            "ubb2": "#e37a50"
        }
    },
    {
        "id": "aurora",
        "name": "极光",
        "v": {
            "bg": "#061019",
            "panel": "#0b1a26",
            "panel2": "#122634",
            "line": "#1d3a4c",
            "ink": "#dcf5ff",
            "dim": "#7aa6b9",
            "accent": "#57e0c0",
            "user": "#10353a",
            "ai": "rgba(11,26,38,.93)",
            "style": "glass",
            "tex": "aurora",
            "bubble": "glow",
            "font": "sans",
            "frame": "neon",
            "deco": "corners",
            "ubtext": "#ffffff",
            "ubb": "#3b9883",
            "ubb2": "#4fa28f"
        }
    },
    {
        "id": "comic",
        "name": "漫画黑白",
        "v": {
            "bg": "#ffffff",
            "panel": "#ffffff",
            "panel2": "#f4f4f4",
            "line": "#111111",
            "ink": "#000000",
            "dim": "#666666",
            "accent": "#000000",
            "user": "#ffe26a",
            "ai": "#ffffff",
            "style": "outline",
            "tex": "dots",
            "bubble": "sharp",
            "font": "round",
            "frame": "bold",
            "deco": "hud",
            "ubtext": "#ffffff",
            "ubb": "#000000",
            "ubb2": "#1a1a1a"
        }
    }
]


DEFAULT_UI = {
    "theme": "light_clean",
    "bg": {"url": "", "brightness": 1.0, "overlay": 0.15, "blur": 0,
           "x": 0, "y": 0, "zoom": 1.02, "rotate": 0},
    "avatar": {"url": "", "shape": "circle", "size": 44, "removeBg": False,
               "border": 2, "borderColor": "#12b7f5", "px": 50, "py": 50, "scale": 1.0},
    "bubble": {"radius": 14, "opacity": 1.0},
    "font": {"size": 15, "color": ""},
}


def log(msg: str) -> None:
    print(f"[gw] {msg}", flush=True)


# ---------------------------------------------------------------- 存储
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


REQUIRED = {
    "conv": {"id", "type", "name", "avatar", "members", "created_at", "invite", "owner",
             "mute_until", "announce", "pin", "num", "at_only", "deleted_at", "bg"},
    "message": {"id", "conv", "sender", "role", "msg_type", "content", "seq", "ts"},
    "file": {"id", "name", "mime", "kind", "size", "path", "text_len", "note", "ts"},
    "setting": {"k", "v"},
    "device": {"device_id", "uid", "name", "created_at", "secret", "avatar", "last_seen"},
}


def migrate(conn) -> None:
    """旧表结构不兼容则改名留档；兼容但缺列则补列（仅 conv 允许原地补）。"""
    stamp = int(time.time())
    for table, cols in REQUIRED.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue
        have = {r[1] for r in rows}
        if cols.issuperset(have):
            missing = cols - have
            if not missing:
                continue
            if table in ("conv", "device"):
                ddl = {"invite": "TEXT", "owner": "INTEGER DEFAULT 0",
                       "mute_until": "INTEGER DEFAULT 0", "announce": "TEXT", "at_only": "INTEGER DEFAULT 0",
                       "pin": "TEXT", "num": "INTEGER DEFAULT 0", "deleted_at": "INTEGER DEFAULT 0",
                       "avatar": "TEXT", "secret": "TEXT", "bg": "TEXT", "last_seen": "INTEGER DEFAULT 0"}
                for col in missing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl.get(col, 'TEXT')}")
                continue
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy_{stamp}")
        log(f"表 {table} 结构不兼容，已改名保留为 {table}_legacy_{stamp}")


def purge_deleted() -> None:
    conn = db()
    cut = int(time.time()) - 7 * 86400
    rows = conn.execute("SELECT id FROM conv WHERE deleted_at>0 AND deleted_at<?", (cut,)).fetchall()
    for r in rows:
        conn.execute("DELETE FROM message WHERE conv=?", (r["id"],))
        conn.execute("DELETE FROM conv WHERE id=?", (r["id"],))
    conn.commit(); conn.close()


def init_db() -> None:
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conv (
            id TEXT PRIMARY KEY, type TEXT, name TEXT, avatar TEXT, members TEXT, created_at INTEGER,
            invite TEXT, owner INTEGER DEFAULT 0, mute_until INTEGER DEFAULT 0, deleted_at INTEGER DEFAULT 0,
            announce TEXT, pin TEXT, num INTEGER DEFAULT 0, bg TEXT
        );
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY, conv TEXT, sender TEXT, role TEXT, msg_type TEXT,
            content TEXT, seq INTEGER, ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv_seq ON message(conv, seq);
        CREATE TABLE IF NOT EXISTS file (
            id TEXT PRIMARY KEY, name TEXT, mime TEXT, kind TEXT, size INTEGER,
            path TEXT, text_len INTEGER, note TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS setting (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS device (
            device_id TEXT PRIMARY KEY, uid INTEGER, name TEXT, created_at INTEGER, secret TEXT, avatar TEXT
        );
        CREATE TABLE IF NOT EXISTS ident (
            device_id TEXT PRIMARY KEY, code TEXT UNIQUE, kind TEXT, created_at INTEGER
        );
        """
    )
    migrate(conn)
    have = {r[1] for r in conn.execute("PRAGMA table_info(device)")}
    if "secret" not in have:
        conn.execute("ALTER TABLE device ADD COLUMN secret TEXT")
    if "avatar" not in have:
        conn.execute("ALTER TABLE device ADD COLUMN avatar TEXT")
    conn.commit()
    if not conn.execute("SELECT COUNT(*) c FROM conv").fetchone()["c"]:
        now = int(time.time())
        conn.executemany(
            "INSERT INTO conv (id,type,name,avatar,members,created_at,invite,owner) VALUES (?,?,?,?,?,?,?,?)",
            [("u1001", "private", BOT_NAME, "", "[]", now, None, 1001),
             ("g1001", "group", "测试群", "", json.dumps([1001, 2001]), now,
              "TEST01", 1001)],
        )
    for row in conn.execute("SELECT id FROM conv WHERE type='group' AND (invite IS NULL OR invite='')"):
        conn.execute("UPDATE conv SET invite=? WHERE id=?", (new_invite(), row["id"]))
    for row in conn.execute("SELECT id FROM conv WHERE type='group' AND (num IS NULL OR num=0)"):
        conn.execute("UPDATE conv SET num=? WHERE id=?", (group_num(row["id"]), row["id"]))
    conn.commit()
    conn.close()


def get_setting(key: str, default):
    conn = db()
    row = conn.execute("SELECT v FROM setting WHERE k=?", (key,)).fetchone()
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return default


def set_setting(key: str, value) -> None:
    conn = db()
    conn.execute("INSERT INTO setting (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
    conn.close()


def ui_state() -> dict:
    state = get_setting("ui", None) or json.loads(json.dumps(DEFAULT_UI))
    for k, v in DEFAULT_UI.items():
        if k not in state:
            state[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict):
            for kk, vv in v.items():
                state[k].setdefault(kk, vv)
    return state


# ---------------------------------------------------------------- 匿名身份
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_identity_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def is_admin(uid: int) -> bool:
    """服务器管理员 = BOT_ID，或 ADMIN_UIDS / ADMIN_CODES 解析出的账号，或 setting 里热加的管理员。"""
    if uid == BOT_ID or uid in ADMIN_UIDS:
        return True
    try:
        return int(uid) in [int(x) for x in (get_setting("admin_uids", []) or [])]
    except Exception:
        return False


def resolve_admin_codes() -> None:
    """启动时把 ADMIN_CODES 解析成 uid 并入 ADMIN_UIDS。"""
    if not ADMIN_CODES:
        return
    conn = db()
    marks = ",".join("?" * len(ADMIN_CODES))
    rows = conn.execute(f"SELECT device_id, code FROM ident WHERE code IN ({marks})",
                        tuple(ADMIN_CODES)).fetchall()
    for r in rows:
        d = conn.execute("SELECT uid FROM device WHERE device_id=?", (r["device_id"],)).fetchone()
        if d:
            ADMIN_UIDS.add(int(d["uid"]))
            log(f"管理员身份码 {r['code']} -> uid {d['uid']}")
    conn.close()


def ident_of(device_id: str, kind: str = "user") -> tuple:
    """给每台设备一个 6 位身份码；agent 与用户共用同一套协议，只是 kind 不同。"""
    conn = db()
    row = conn.execute("SELECT code,kind FROM ident WHERE device_id=?", (device_id,)).fetchone()
    if row:
        code = row["code"]
        if kind == "agent" and (row["kind"] or "") != "agent":
            conn.execute("UPDATE ident SET kind='agent' WHERE device_id=?", (device_id,))
            conn.commit()
        conn.close()
        return code, (kind if kind == "agent" else (row["kind"] or "user"))
    for _ in range(10):
        code = new_identity_code()
        try:
            conn.execute("INSERT INTO ident (device_id,code,kind,created_at) VALUES (?,?,?,?)",
                         (device_id, code, kind, int(time.time())))
            conn.commit(); conn.close()
            return code, kind
        except Exception:
            continue
    conn.close()
    return "", kind


BOT_TOKEN = os.getenv("BOT_TOKEN", "")


def ensure_secret(device_id: str) -> str:
    """每台设备一个随机密钥，防止知道 device id 就能冒充。"""
    conn = db()
    row = conn.execute("SELECT secret FROM device WHERE device_id=?", (device_id,)).fetchone()
    sec = (row["secret"] if row else "") or ""
    if not sec:
        sec = "sk_" + secrets.token_urlsafe(24)
        conn.execute("UPDATE device SET secret=? WHERE device_id=?", (sec, device_id))
        conn.commit()
    conn.close()
    return sec


def secret_of(device_id: str) -> str:
    conn = db()
    row = conn.execute("SELECT secret FROM device WHERE device_id=?", (device_id,)).fetchone()
    conn.close()
    return (row["secret"] if row else "") or ""


def device_profile(device_id: str, name: str = "", kind: str = "user", with_secret: bool = False) -> dict:
    """没有账号密码：device id 决定一个稳定的数字 uid，昵称可改。"""
    device_id = (device_id or "").strip()[:64]
    if device_id in ("bot", "BOT", "ai", "AI", str(BOT_ID)):
        conn = db()
        row = conn.execute("SELECT * FROM device WHERE device_id='bot'").fetchone()
        if row is None:
            conn.execute("INSERT INTO device (device_id,uid,name,created_at) VALUES ('bot',?,?,?)",
                         (BOT_ID, BOT_NAME, int(time.time())))
            conn.commit()
        conn.close()
        name_out = BOT_NAME
        return {"device_id": "bot", "uid": BOT_ID, "name": name_out, "code": "", "kind": "agent",
                "admin": True, "secret": BOT_TOKEN}
    if not device_id:
        device_id = "anon-" + secrets.token_hex(8)
    conn = db()
    def _new_uid():
        while True:
            cand = secrets.randbelow(800000) + 100000
            if conn.execute("SELECT 1 FROM device WHERE uid=?", (cand,)).fetchone() is None:
                return cand
    uid = _new_uid()
    row = conn.execute("SELECT * FROM device WHERE device_id=?", (device_id,)).fetchone()
    existed = row is not None
    if row:
        if name and name != row["name"]:
            conn.execute("UPDATE device SET name=? WHERE device_id=?", (name[:16], device_id))
            conn.commit()
        uid, nick = row["uid"], (name[:16] or row["name"])
    else:
        nick = (name[:16] or ("访客" + device_id[-4:]))
        conn.execute("INSERT INTO device (device_id,uid,name,created_at) VALUES (?,?,?,?)",
                     (device_id, uid, nick, int(time.time())))
        conn.commit()
    conn.close()
    if uid in banned_uids():
        raise HTTPException(403, "该账号已被移出服务器")
    av = ""
    try:
        conn2 = db()
        r2 = conn2.execute("SELECT avatar FROM device WHERE device_id=?", (device_id,)).fetchone()
        conn2.close()
        av = (r2["avatar"] or "") if r2 else ""
    except Exception:
        av = ""
    code, kind2 = ident_of(device_id, kind)
    out = {"device_id": device_id, "uid": uid, "name": nick, "code": code, "kind": kind2,
           "admin": is_admin(uid), "avatar": av}
    if with_secret and not existed:
        out["secret"] = ensure_secret(device_id)
    return out


def uid_name(uid: int) -> str:
    if uid == BOT_ID:
        return BOT_NAME
    conn = db()
    row = conn.execute("SELECT name FROM device WHERE uid=? ORDER BY created_at LIMIT 1", (uid,)).fetchone()
    conn.close()
    return row["name"] if row else ("用户" + str(uid)[-4:])


def check_token(request_or_ws) -> bool:
    if not GATEWAY_TOKEN:
        return True
    header = ""
    try:
        header = request_or_ws.headers.get("x-gateway-token", "")
    except Exception:
        header = ""
    if header and header == GATEWAY_TOKEN:
        return True
    token = ""
    try:
        token = request_or_ws.query_params.get("token", "")
    except Exception:
        token = ""
    return token == GATEWAY_TOKEN


# ---------------------------------------------------------------- 会话 / 群
def group_num(cid: str) -> int:
    if cid == "g1001":
        return 1001
    return 1000000 + int(hashlib.sha1(cid.encode()).hexdigest()[:6], 16) % 9000000


def conv_by_num(num: int) -> str | None:
    conn = db()
    row = conn.execute("SELECT id FROM conv WHERE num=?", (num,)).fetchone()
    conn.close()
    return row["id"] if row else None


def new_invite() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def conv_row(cid: str):
    conn = db()
    row = conn.execute("SELECT * FROM conv WHERE id=?", (cid,)).fetchone()
    conn.close()
    return row


def members_of(cid: str) -> list:
    row = conv_row(cid)
    if row is None:
        return []
    try:
        return [int(x) for x in json.loads(row["members"] or "[]")]
    except Exception:
        return []


def set_members(cid: str, members: list) -> None:
    conn = db()
    conn.execute("UPDATE conv SET members=? WHERE id=?",
                 (json.dumps(sorted(set(int(m) for m in members)), ensure_ascii=False), cid))
    conn.commit()
    conn.close()


def create_group(name: str, owner: int, members: list | None = None, announce: bool = True) -> dict:
    cid = "g" + uuid.uuid4().hex[:8]
    invite = new_invite()
    ms = sorted(set([owner, BOT_ID] + (members or [])))
    conn = db()
    conn.execute("INSERT INTO conv (id,type,name,avatar,members,created_at,invite,owner,mute_until,"
                 "announce,pin,num) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, "group", (name or "新群聊")[:24], "", json.dumps(ms), int(time.time()),
                  invite, owner, 0, "", "", group_num(cid)))
    conn.commit()
    conn.close()
    log(f"建群 {cid} / {name} / 邀请码 {invite}")
    # 由 AI 发起的建群不再灌一条系统播报：群里只需要 AI 说话，避免“系统+AI”各跑一遍
    if announce:
        add_message(cid, "system", "sys", "sys",
                    {"text": f"群已创建，邀请码 {invite}。把邀请链接发给朋友即可加入，无需账号。"})
    return {"id": cid, "name": name, "invite": invite, "members": ms, "owner": owner}


def join_group(invite: str, uid: int) -> dict:
    conn = db()
    row = conn.execute("SELECT * FROM conv WHERE invite=?", ((invite or "").upper(),)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, "邀请码无效")
    ms = members_of(row["id"])
    if uid not in ms:
        ms.append(uid)
        set_members(row["id"], ms)
    return {"id": row["id"], "name": row["name"], "members": ms}


def add_member(cid: str, uid: int) -> bool:
    ms = members_of(cid)
    if uid in ms:
        return False
    ms.append(uid)
    set_members(cid, ms)
    return True


def remove_member(cid: str, uid: int) -> bool:
    ms = members_of(cid)
    if uid not in ms:
        return False
    ms = [m for m in ms if m != uid]
    set_members(cid, ms)
    return True


def ensure_access(cid: str, uid: int) -> bool:
    """公开群（演示群 g1001）首次进入自动成为成员。"""
    row = conv_row(cid)
    if row is not None and (row["deleted_at"] or 0):
        return False
    if cid.startswith("u"):
        try:
            return int(cid[1:]) in (uid, BOT_ID)
        except Exception:
            return False
    if cid.startswith("d"):
        try:
            a, b = cid[1:].split("-")
            return uid in {int(a), int(b)}
        except Exception:
            return False
    if not cid.startswith("g"):
        return False
    if uid in members_of(cid):
        return True
    if cid == "g1001":
        add_member(cid, uid)
        return True
    return False


def ensure_private(uid: int) -> None:
    """每个设备进来自动拥有自己的私聊 u<uid>，不再共用演示会话 u1001。"""
    if not uid:
        return
    cid = f"u{uid}"
    conn = db()
    row = conn.execute("SELECT id FROM conv WHERE id=?", (cid,)).fetchone()
    if row is None:
        conn.execute("INSERT OR REPLACE INTO conv (id,type,name,avatar,members,created_at,invite,owner,"
                     "mute_until,announce,pin,num) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, "private", BOT_NAME, "", json.dumps([uid, BOT_ID]), int(time.time()),
                      "", uid, 0, "", "", ""))
        conn.commit()
    conn.close()


def conv_list(uid: int = 0) -> list:
    conn = db()
    rows = [r for r in conn.execute("SELECT * FROM conv ORDER BY type DESC, created_at").fetchall()
            if not (r["deleted_at"] or 0)]
    seqs = {r["conv"]: r["s"] for r in conn.execute(
        "SELECT conv, MAX(seq) s FROM message GROUP BY conv").fetchall()}
    conn.close()
    out = []
    for r in rows:
        members = []
        try:
            members = [int(x) for x in json.loads(r["members"] or "[]")]
        except Exception:
            pass
        if r["type"] == "private" and uid and r["id"] != f"u{uid}":
            # 别人的私聊不下发
            continue
        if (r["type"] == "private" and uid and r["id"] == f"u{uid}" and int(uid) != BOT_ID
                and not get_setting("sys", {}).get("show_ai_all", True) and not is_admin(int(uid))):
            # 管理员关闭「所有人可见 AI 助手」：非管理员列表里不再出现与 AI 的私聊
            continue
        if r["type"] == "dm" and BOT_ID in members:
            continue
        if r["type"] == "dm" and uid and uid not in members:
            continue
        if r["type"] == "group" and uid and uid not in members and r["id"] != "g1001":
            # 未加入的群不下发（保留演示群）
            continue
        cname = r["name"]
        if r["type"] == "dm" and uid:
            other = [m for m in members if m != int(uid)]
            if other:
                cname = uid_name(other[0])
        out.append({"id": r["id"], "type": r["type"], "name": cname, "avatar": r["avatar"],
                    "members": members, "invite": r["invite"], "owner": r["owner"],
                    "mute_until": r["mute_until"] or 0, "announce": r["announce"] or "",
                    "pin": r["pin"] or "", "created_at": r["created_at"],
                    "bg": json.loads(r["bg"]) if (r["bg"] or "").strip() else None,
                    "last_seq": seqs.get(r["id"], 0)})
    return out


def conv_public(cid: str) -> dict | None:
    backends = net_info()
    for c in conv_list(0):
        if c["id"] == cid:
            c = dict(c)
            c["join_url"] = f"{backends['base']}/j/{c.get('invite') or ''}"
            return c
    return None


def rename_conv(cid: str, name: str) -> None:
    if not cid or not name:
        return
    conn = db()
    conn.execute("UPDATE conv SET name=? WHERE id=?", (name[:24], cid))
    conn.commit()
    conn.close()


def announce(cid: str, text: str) -> None:
    conn = db()
    conn.execute("UPDATE conv SET announce=? WHERE id=?", (text[:200], cid))
    conn.commit()
    conn.close()


def mute_minutes(cid: str, minutes: int) -> None:
    mute_conv(cid, minutes)


def banned_uids() -> list:
    return [int(x) for x in (get_setting("banned_uids", []) or [])]


def set_banned(uids: list) -> None:
    set_setting("banned_uids", uids)


def mute_conv(cid: str, minutes: int) -> None:
    conn = db()
    conn.execute("UPDATE conv SET mute_until=? WHERE id=?",
                 (int(time.time()) + max(0, minutes) * 60 if minutes > 0 else 0, cid))
    conn.commit()
    conn.close()


def pin_message(cid: str, msg_id: str) -> None:
    conn = db()
    conn.execute("UPDATE conv SET pin=? WHERE id=?", (msg_id or "", cid))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 消息
def next_seq(conv: str) -> int:
    conn = db()
    row = conn.execute("SELECT COALESCE(MAX(seq),0) s FROM message WHERE conv=?", (conv,)).fetchone()
    conn.close()
    return int(row["s"]) + 1


def msg_quote(conv: str, seq: int) -> dict:
    conn = db()
    row = conn.execute("SELECT * FROM message WHERE conv=? AND seq=?", (conv, seq)).fetchone()
    conn.close()
    if not row:
        return {}
    c = json.loads(row["content"] or "{}")
    return {"seq": seq, "sender": row["sender"], "role": row["role"],
            "name": c.get("name") or ("AI" if row["role"] == "bot" else "成员"),
            "text": (c.get("text") or "")[:120]}


def prune_messages(conv: str, keep: int = 5000) -> None:
    """服务器保留每会话最近 N 条（默认 5000），保证检索能查到更多历史。"""
    conn = db()
    mx = conn.execute("SELECT MAX(seq) m FROM message WHERE conv=?", (conv,)).fetchone()["m"] or 0
    if mx > keep:
        conn.execute("DELETE FROM message WHERE conv=? AND seq<=?", (conv, mx - keep))
        conn.commit()
    conn.close()


PLUGINS: list = []
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent / "plugins"


def load_plugins() -> None:
    """加载 plugins/*.py，每个模块可定义 on_message(msg) -> dict|None。

    约定：只允许改文本类字段，抛异常不影响主流程。"""
    PLUGINS.clear()
    if not PLUGIN_DIR.exists():
        return
    for f in sorted(PLUGIN_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("gwplug_" + f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "on_message", None)
            if callable(fn):
                PLUGINS.append((f.stem, fn))
                log(f"插件已加载: {f.stem}")
        except Exception as e:
            log(f"插件 {f.name} 加载失败: {e}")


def run_message_plugins(msg: dict) -> dict:
    for name, fn in PLUGINS:
        try:
            out = fn(dict(msg))
            if isinstance(out, dict) and out.get("content"):
                msg["content"] = out["content"]
        except Exception as e:
            log(f"插件 {name} 执行异常: {e}")
    return msg


def fire_webhook(msg: dict) -> None:
    """把新消息 POST 给外部 agent（AGENT_WEBHOOK），失败不影响聊天。"""
    url = os.environ.get("AGENT_WEBHOOK", "").strip()
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    sec = os.environ.get("AGENT_WEBHOOK_SECRET", "").strip()
    if sec:
        headers["X-Webhook-Secret"] = sec

    async def _post():
        try:
            async with httpx.AsyncClient(timeout=4) as cli:
                await cli.post(url, json=msg, headers=headers)
        except Exception as e:
            log(f"webhook 失败: {e}")

    try:
        asyncio.get_running_loop().create_task(_post())
    except RuntimeError:
        pass


def add_message(conv: str, sender: str, role: str, msg_type: str, content: dict) -> dict:
    content = run_message_plugins({"conv": conv, "sender": sender, "role": role,
                                   "type": msg_type, "content": content})["content"]
    conn = db()
    seq = next_seq(conv)
    mid = "m-" + uuid.uuid4().hex[:12]
    ts = int(time.time() * 1000)
    conn.execute("INSERT INTO message (id,conv,sender,role,msg_type,content,seq,ts) VALUES (?,?,?,?,?,?,?,?)",
                 (mid, conv, sender, role, msg_type, json.dumps(content, ensure_ascii=False), seq, ts))
    conn.commit()
    conn.close()
    if seq % 20 == 0:
        prune_messages(conv)
    out = {"id": mid, "conv": conv, "sender": sender, "role": role, "type": msg_type,
           "content": content, "seq": seq, "ts": ts}
    fire_webhook(out)
    return out


def history(conv: str, limit: int = 60, after: int = 0) -> list:
    conn = db()
    if after:
        rows = conn.execute("SELECT * FROM message WHERE conv=? AND seq>? ORDER BY seq LIMIT ?",
                            (conv, after, limit)).fetchall()
        rows = list(rows)
    else:
        rows = conn.execute("SELECT * FROM message WHERE conv=? ORDER BY seq DESC LIMIT ?",
                            (conv, limit)).fetchall()
        rows = list(reversed(rows))
    conn.close()
    out = []
    for r in rows:
        out.append({"id": r["id"], "conv": r["conv"], "sender": r["sender"], "role": r["role"],
                    "type": r["msg_type"], "content": json.loads(r["content"]),
                    "seq": r["seq"], "ts": r["ts"]})
    return out


# ---------------------------------------------------------------- 文档解析
def extract_text(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            parts = []
            for i, page in enumerate(PdfReader(str(path)).pages, 1):
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t.strip():
                    parts.append(f"[第{i}页]\n{t.strip()}")
            return "\n".join(parts)
        if ext == ".docx":
            from docx import Document
            doc = Document(str(path))
            parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            for ti, table in enumerate(doc.tables, 1):
                rows = ["| " + " | ".join(c.text.strip().replace("\n", " ") for c in r.cells) + " |"
                        for r in table.rows[:60]]
                if rows:
                    parts.append(f"[表{ti}]\n" + "\n".join(rows))
            return "\n".join(parts)
        if ext in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(str(path), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets[:5]:
                parts.append(f"[工作表：{ws.title}]")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i > 80:
                        parts.append("…(截断)")
                        break
                    cells = ["" if v is None else str(v) for v in row[:12]]
                    if any(c.strip() for c in cells):
                        parts.append("| " + " | ".join(cells) + " |")
            wb.close()
            return "\n".join(parts)
        if ext == ".pptx":
            from pptx import Presentation
            parts = []
            for i, slide in enumerate(Presentation(str(path)).slides, 1):
                buf = [s.text_frame.text.strip() for s in slide.shapes
                       if getattr(s, "has_text_frame", False) and s.text_frame.text.strip()]
                if buf:
                    parts.append(f"[第{i}页]\n" + "\n".join(buf))
            return "\n".join(parts)
        with open(path, "rb") as f:
            head = f.read(2 * 1024 * 1024)
        body = head.decode("utf-8", errors="replace")
        if path.stat().st_size > len(head):
            body += "\n…（文件较大，仅解析前 2MB）"
        return body
    except Exception as exc:
        return f"[解析失败：{type(exc).__name__}]"


MAGIC_RULES = {
    "png": (b"\x89PNG",), "jpg": (b"\xff\xd8\xff",), "gif": (b"GIF8",),
    "bmp": (b"BM",), "tiff": (b"II*\x00", b"MM\x00*"),
    "zip": (b"PK\x03\x04",), "rar": (b"Rar!",), "7z": (b"7z\xbc\xaf",),
    "gz": (b"\x1f\x8b",), "bz2": (b"BZh",), "xz": (b"\xfd7zXZ",),
    "pdf": (b"%PDF",), "ole": (b"\xd0\xcf\x11\xe0",),
    "mkv": (b"\x1aE\xdf\xa3",), "ogg": (b"OggS",), "flac": (b"fLaC",),
    "mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3"), "amr": (b"#!AMR",),
    "aac": (b"\xff\xf1", b"\xff\xf9"), "wav": (b"RIFF",), "webp": (b"RIFF",),
    "aiff": (b"FORM",), "ico": (b"\x00\x00\x01\x00",),
    "rtf": (b"{\\rtf",),
}
MAGIC_BY_EXT = {
    ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".jfif": "jpg", ".gif": "gif",
    ".bmp": "bmp", ".tif": "tiff", ".tiff": "tiff", ".webp": "webp", ".ico": "ico",
    ".pdf": "pdf", ".zip": "zip", ".docx": "zip", ".xlsx": "zip", ".pptx": "zip",
    ".odt": "zip", ".ods": "zip", ".odp": "zip", ".epub": "zip",
    ".rar": "rar", ".7z": "7z", ".gz": "gz", ".tgz": "gz", ".bz2": "bz2", ".xz": "xz",
    ".doc": "ole", ".xls": "ole", ".ppt": "ole", ".rtf": "rtf",
    ".mkv": "mkv", ".webm": "mkv", ".ogg": "ogg", ".opus": "ogg", ".flac": "flac",
    ".mp3": "mp3", ".amr": "amr", ".aac": "aac", ".wav": "wav", ".aiff": "aiff",
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mp4", ".3gp": "mp4",
}


def magic_mismatch(ext: str, head: bytes) -> bool:
    """按文件头判断扩展名是否在造假；文本、未知类型一律放行。"""
    key = MAGIC_BY_EXT.get(ext)
    if not key:
        return False
    if key == "mp4":
        return not (len(head) >= 12 and head[4:8] == b"ftyp")
    if key == "tiff":
        return not (head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"))
    if key == "wav":
        return not (head.startswith(b"RIFF") and head[8:12] == b"WAVE")
    if key == "webp":
        return not (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    if key == "aiff":
        return not (head.startswith(b"FORM") and head[8:12] == b"AIFF")
    return not any(head.startswith(sig) for sig in MAGIC_RULES[key])


def classify(ext: str) -> str:
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in DOC_EXT:
        return "doc"
    return "other"


DANGEROUS_INLINE = {".html", ".htm", ".svg", ".xml", ".xhtml", ".js", ".mjs", ".css"}


def archive_listing(path: pathlib.Path) -> str:
    """只列文件名，不解压，避免 zip 炸弹与路径穿越。"""
    if path.suffix.lower() != ".zip":
        return ""
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        head = names[:30]
        more = "" if len(names) <= 30 else f"…（共 {len(names)} 项）"
        return "压缩包内文件：\n" + "\n".join(head) + ("\n" + more if more else "")
    except Exception:
        return ""


def convert_heic(path: pathlib.Path) -> pathlib.Path | None:
    """HEIC/HEIF 转 JPEG；没有 pillow-heif 时返回 None。"""
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
        register_heif_opener()
        out = path.with_suffix(".jpg")
        Image.open(path).convert("RGB").save(out, "JPEG", quality=88)
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- 图像
def remove_background(src: pathlib.Path, dst: pathlib.Path, tolerance: int = 46) -> bool:
    try:
        from PIL import Image
        img = Image.open(src).convert("RGBA")
        w, h = img.size
        px = img.load()
        corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
        base = corners[0]
        for c in corners:
            if sum(abs(a - b) for a, b in zip(c[:3], base[:3])) > tolerance:
                return False
        tol2 = tolerance * tolerance * 3
        stack = ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] +
                 [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)])
        seen = bytearray(w * h)
        while stack:
            x, y = stack.pop()
            if x < 0 or y < 0 or x >= w or y >= h:
                continue
            idx = y * w + x
            if seen[idx]:
                continue
            p = px[x, y]
            if p[3] == 0:
                seen[idx] = 1
                continue
            d = (p[0] - base[0]) ** 2 + (p[1] - base[1]) ** 2 + (p[2] - base[2]) ** 2
            if d > tol2:
                continue
            seen[idx] = 1
            px[x, y] = (p[0], p[1], p[2], 0)
            stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img.save(dst, "PNG")
        return True
    except Exception as exc:
        log("去背失败：" + type(exc).__name__)
        return False


def palette_from_image(path: pathlib.Path) -> dict:
    from PIL import Image
    img = Image.open(path).convert("RGB").resize((64, 64))
    q = img.quantize(colors=8, method=Image.MEDIANCUT).convert("RGB")
    counts = {}
    for p in q.getdata():
        counts[p] = counts.get(p, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    total = sum(counts.values())

    def neutral(c):
        return max(c) - min(c) < 18

    accent = ordered[0][0]
    for c, n in ordered:
        if not neutral(c) and n / total > 0.05:
            accent = c
            break
    lum = 0.299 * accent[0] + 0.587 * accent[1] + 0.114 * accent[2]
    dark = lum < 140
    return {"v": {
        "bg": "#101216" if dark else "#f6f7f9",
        "panel": "#101317" if dark else "#ffffff",
        "panel2": "#1b1f25" if dark else "#eef0f3",
        "line": "#2c323b" if dark else "#dfe3e8",
        "ink": "#e8ecf1" if dark else "#1c2128",
        "dim": "#8f98a4" if dark else "#6b7480",
        "accent": "#%02x%02x%02x" % accent,
        "user": "#%02x%02x%02x" % tuple(min(255, int(c * 0.55 + (0 if dark else 190))) for c in accent),
        "ai": "rgba(27,31,37,.92)" if dark else "#ffffff",
    }}


# ---------------------------------------------------------------- 模糊意图
def parse_intent(text: str, has_image: bool = False) -> list:
    t = (text or "").strip()
    acts = []
    if not t:
        return acts

    if has_image and any(k in t for k in ("这张图", "根据图", "按这张", "配个主题", "配色", "这图")):
        acts.append({"type": "theme.from_image"})

    if any(k in t for k in ("太亮", "好亮", "刺眼", "晃眼")):
        acts.append({"type": "bg.set", "payload": {"brightness": 0.55, "overlay": 0.42}})
    elif any(k in t for k in ("太暗", "好黑", "看不清")):
        acts.append({"type": "bg.set", "payload": {"brightness": 1.18, "overlay": 0.06}})

    if any(k in t for k in ("太花", "太乱", "太杂")):
        acts.append({"type": "bg.set", "payload": {"blur": 10, "overlay": 0.35}})
    if any(k in t for k in ("太糊", "清楚一点", "清晰")):
        acts.append({"type": "bg.set", "payload": {"blur": 0}})

    if "头像" in t and any(k in t for k in ("大", "放大")):
        acts.append({"type": "avatar.set", "payload": {"size": 56}})
    if "头像" in t and any(k in t for k in ("小", "缩小")):
        acts.append({"type": "avatar.set", "payload": {"size": 32}})

    if any(k in t for k in ("圆形", "圆的", "圆头像")):
        acts.append({"type": "avatar.set", "payload": {"shape": "circle"}})
    if any(k in t for k in ("方形", "方的", "方头像", "圆角方")):
        acts.append({"type": "avatar.set", "payload": {"shape": "square"}})

    if any(k in t for k in ("去背", "抠掉背景", "透明底", "去掉背景", "只要主体")):
        acts.append({"type": "avatar.set", "payload": {"removeBg": True}})

    # 主题切换只走显式指令：AI 输出 <ui>{"actions":[{"type":"theme.apply",...}]}</ui>
    # 或在设置页手动选。不再按聊天文本里的“蓝色/简约/樱花”等关键词猜主题——
    # 聊天内容里出现颜色词会被误判为主题切换，行为不可控。

    if any(k in t for k in ("换壁纸", "换背景", "换个背景", "随机背景")):
        acts.append({"type": "bg.random"})

    if any(k in t for k in ("字大", "字体大")):
        acts.append({"type": "font.set", "payload": {"size": 17}})
    if any(k in t for k in ("字小", "字体小")):
        acts.append({"type": "font.set", "payload": {"size": 14}})

    # ---- 群管理意图（AI 只有拿到 group 语境才执行，见 ws 处理）
    m = re.search(r"(?:建|开|拉)(?:个|一个)?群(?:叫|名叫|：|:)?\s*[「“\"]?(.{1,16}?)[」”\"]?$", t)
    if m:
        acts.append({"type": "chat.create", "payload": {"name": m.group(1)}})
    m = re.search(r"(?:把)?群名(?:改成|改为|叫)\s*[「“\"]?(.{1,16}?)[」”\"]?$", t)
    if m:
        acts.append({"type": "chat.rename", "payload": {"name": m.group(1)}})
    m = re.search(r"禁言\s*(\d+)\s*(分钟|小时)", t)
    if m:
        mins = int(m.group(1)) * (60 if m.group(2) == "小时" else 1)
        acts.append({"type": "chat.mute", "payload": {"minutes": mins}})
    m = re.search(r"(?:发个?|设置?)?公告(?:说|：|:)?\s*(.{1,60})$", t)
    if m:
        acts.append({"type": "chat.announce", "payload": {"text": m.group(1)}})
    return acts


WALLPAPERS = [["#1b2735", "#090a0f"], ["#2b1a2f", "#0d0611"], ["#10231f", "#050c0a"],
              ["#2a2118", "#0c0906"], ["#16233a", "#070b14"]]
_wp = {"i": 0}


def set_conv_bg(cid: str, data: dict) -> dict:
    """群背景：整群统一，存 conv.bg。空 url 表示恢复默认。"""
    cur = {}
    conn = db()
    row = conn.execute("SELECT bg FROM conv WHERE id=?", (cid,)).fetchone()
    if row and (row["bg"] or "").strip():
        try:
            cur = json.loads(row["bg"])
        except Exception:
            cur = {}
    for k in ("url", "gradient"):
        if k in data:
            cur[k] = data.get(k) or ""
    for k, lo, hi in (("brightness", 0.2, 1.3), ("overlay", 0, 0.9)):
        if k in data:
            try:
                cur[k] = max(lo, min(hi, float(data[k])))
            except Exception:
                pass
    if "blur" in data:
        try:
            cur["blur"] = max(0, min(24, int(data["blur"])))
        except Exception:
            pass
    conn.execute("UPDATE conv SET bg=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), cid))
    conn.commit(); conn.close()
    return cur


def apply_actions(actions: list, conv: str | None = None, actor: int = BOT_ID) -> tuple:
    """返回 (ui_state, 已执行的群操作说明列表)。群操作只对群会话生效。"""
    st = ui_state()
    notes = []
    for a in actions or []:
        t = a.get("type")
        p = a.get("payload") or a
        try:
            if t == "theme.apply":
                tid = p.get("theme_id")
                all_themes = THEMES + get_setting("custom_themes", [])
                if any(x["id"] == tid for x in all_themes):
                    st["theme"] = tid
            elif t == "theme.from_image":
                fid = p.get("file_id")
                row = _file_row(fid) if fid else None
                if row and row["kind"] == "image":
                    theme = palette_from_image(pathlib.Path(row["path"]))
                    theme["id"] = "theme_img_" + fid[-6:]
                    theme["name"] = "取自图片"
                    themes = [x for x in get_setting("custom_themes", []) if x["id"] != theme["id"]]
                    themes.append(theme)
                    set_setting("custom_themes", themes)
                    st["theme"] = theme["id"]
            elif t == "bg.set":
                if conv and str(conv).startswith("g") and not (is_admin(actor) or actor == BOT_ID):
                    notes.append("只有管理员可以更换群背景")
                    continue
                if conv and str(conv).startswith("g"):
                    set_conv_bg(conv, p)
                    notes.append("群背景已更新")
                    continue
                if "brightness" in p:
                    st["bg"]["brightness"] = max(0.2, min(1.3, float(p["brightness"])))
                if "overlay" in p:
                    st["bg"]["overlay"] = max(0, min(0.9, float(p["overlay"])))
                if "blur" in p:
                    st["bg"]["blur"] = max(0, min(24, int(p["blur"])))
                if p.get("url"):
                    st["bg"]["url"] = p["url"]
            elif t == "bg.random":
                if conv and str(conv).startswith("g") and not (is_admin(actor) or actor == BOT_ID):
                    notes.append("只有管理员可以更换群背景")
                    continue
                wp = WALLPAPERS[_wp["i"] % len(WALLPAPERS)]
                _wp["i"] += 1
                if conv and str(conv).startswith("g"):
                    set_conv_bg(conv, {"url": "", "gradient": wp})
                    notes.append("群背景已更新")
                    continue
                st["bg"]["url"] = ""
                st["bg"]["gradient"] = wp
            elif t == "avatar.set":
                if p.get("url"):
                    st["avatar"]["url"] = p["url"]
                if p.get("shape") in ("circle", "square"):
                    st["avatar"]["shape"] = p["shape"]
                if "size" in p:
                    st["avatar"]["size"] = max(28, min(64, int(p["size"])))
                if "removeBg" in p:
                    st["avatar"]["removeBg"] = bool(p["removeBg"])
                if p.get("borderColor"):
                    st["avatar"]["borderColor"] = p["borderColor"]
                if "border" in p:
                    st["avatar"]["border"] = max(0, min(6, int(p["border"])))
            elif t == "font.set":
                if "size" in p:
                    st["font"]["size"] = max(13, min(20, int(p["size"])))
            elif t == "bubble.set":
                if "radius" in p:
                    st["bubble"]["radius"] = max(4, min(24, int(p["radius"])))
                if "opacity" in p:
                    st["bubble"]["opacity"] = max(0.35, min(1.0, float(p["opacity"])))
            # ---- 群管理（服务端真正落库，不再只改前端）
            elif t == "chat.create":
                # 群主固定为 AI，发起人作为成员加入；创建只能走服务端 AI 通路
                g = create_group(p.get("name") or "AI 建的群", BOT_ID, members=[actor], announce=False)
                notes.append(f"已建群「{g['name']}」，邀请码 {g['invite']}，你已在群里")
            elif t == "chat.join":
                try:
                    if p.get("invite"):
                        join_group(p["invite"], actor)
                        notes.append("已加入群聊")
                except HTTPException:
                    notes.append("邀请码无效")
            elif t == "chat.rename":
                cid = p.get("conv_id") or conv
                if cid and (cid.startswith("g")):
                    rename_conv(cid, p.get("name") or "")
                    notes.append(f"群名已改为「{p.get('name')}」")
            elif t in ("chat.kick", "chat.remove_member"):
                cid = p.get("conv_id") or conv
                if cid and p.get("uid"):
                    remove_member(cid, int(p["uid"]))
                    notes.append(f"已移出 {uid_name(int(p['uid']))}")
            elif t == "chat.add_member":
                cid = p.get("conv_id") or conv
                if cid and p.get("uid"):
                    add_member(cid, int(p["uid"]))
                    notes.append(f"已拉入 {uid_name(int(p['uid']))}")
            elif t == "chat.mute":
                cid = p.get("conv_id") or conv
                if cid:
                    mute_conv(cid, int(p.get("minutes") or 0))
                    notes.append(f"已禁言 {p.get('minutes')} 分钟")
            elif t == "chat.announce":
                cid = p.get("conv_id") or conv
                if cid:
                    announce(cid, p.get("text") or "")
                    notes.append("公告已更新")
            elif t == "chat.pin":
                cid = p.get("conv_id") or conv
                if cid:
                    pin_message(cid, p.get("message_id") or "")
                    notes.append("已置顶消息")
        except Exception as exc:
            log(f"动作失败 {t}: {type(exc).__name__}")
    set_setting("ui", st)
    return st, notes


# ---------------------------------------------------------------- 文件
def _file_row(fid: str):
    conn = db()
    row = conn.execute("SELECT * FROM file WHERE id=?", (fid,)).fetchone()
    conn.close()
    return row


def file_meta(row) -> dict:
    return {"file_id": row["id"], "name": row["name"], "mime": row["mime"], "kind": row["kind"],
            "size": row["size"], "url": f"/api/files/{row['id']}", "media": f"/media/{row['id']}",
            "text_len": row["text_len"], "note": row["note"] or ""}


# ---------------------------------------------------------------- 适配层
LAST_HOST = {"v": ""}


def remember_host(request) -> None:
    """App 从哪个地址连进来，就把这个地址当作对外地址（公网 IP:端口）。"""
    try:
        host = request.headers.get("host", "")
        if host:
            scheme = request.headers.get("x-forwarded-proto") or (
                "https" if request.url.scheme == "https" else "http")
            LAST_HOST["v"] = f"{scheme}://{host}"
    except Exception:
        pass


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.6)
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def net_info() -> dict:
    public = PUBLIC_BASE or LAST_HOST["v"] or f"http://{local_ip()}:{os.getenv('PORT', '8099')}"
    return {"base": public, "lan_ip": local_ip(), "port": int(os.getenv("PORT", "8099")),
            "mode": AGENT_MODE, "token_required": bool(GATEWAY_TOKEN)}


class AgentLink:
    """统一出口：onebot 走反向 WS，openai 直连兼容接口，webhook 打自建 agent。"""

    def __init__(self):
        self.ws = None
        self.online = False
        self.self_id = BOT_ID

    async def start(self):
        if AGENT_MODE == "onebot":
            asyncio.create_task(self._onebot_loop())
        else:
            self.online = True
            log(f"适配模式 {AGENT_MODE}，就绪")

    # ---------- OneBot ----------
    async def _onebot_loop(self):
        while True:
            # OneBot v11 反向 WS 握手必须带这两个头，AstrBot 会校验
            headers = {"X-Self-ID": str(self.self_id), "X-Client-Role": "Universal"}
            if ASTRBOT_TOKEN:
                headers["Authorization"] = f"Bearer {ASTRBOT_TOKEN}"
            try:
                async with websockets.connect(ASTRBOT_WS, additional_headers=headers, ping_interval=15,
                                              max_size=None, max_queue=256) as ws:
                    self.ws = ws
                    self.online = True
                    log("已连接 AstrBot：" + ASTRBOT_WS)
                    await self._send({"post_type": "meta_event", "meta_event_type": "lifecycle",
                                      "sub_type": "connect", "time": int(time.time()),
                                      "self_id": self.self_id})
                    await self._onebot_read(ws)
            except Exception as exc:
                if self.online:
                    _detail = ""
                    _rcvd = getattr(exc, "rcvd", None)
                    if _rcvd is not None:
                        _detail = f" code={getattr(_rcvd,'code','?')} reason={getattr(_rcvd,'reason','')}"
                    log("与 AstrBot 断开：" + type(exc).__name__ + _detail)
                self.online = False
                self.ws = None
            await asyncio.sleep(3)

    async def _onebot_read(self, ws):
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
            except asyncio.TimeoutError:
                await self._send({"post_type": "meta_event", "meta_event_type": "heartbeat",
                                  "time": int(time.time()), "self_id": self.self_id,
                                  "status": {"online": True, "good": True}, "interval": 15000})
                continue
            await self._onebot_action(json.loads(raw))

    async def _send(self, payload):
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            self.online = False
            self.ws = None

    async def _onebot_action(self, payload):
        action = payload.get("action")
        params = payload.get("params") or {}
        echo = payload.get("echo")
        ok = {"status": "ok", "retcode": 0, "data": {}, "echo": echo}

        if action in ("send_msg", "send_private_msg", "send_group_msg"):
            gid = params.get("group_id")
            uid = params.get("user_id") or 1001
            conv = (conv_by_num(int(gid)) or f"g{gid}") if gid else f"u{uid}"
            msg = params.get("message")
            if isinstance(msg, str):
                msg = [{"type": "text", "data": {"text": msg}}]
            text, images = parse_chain(msg or [])
            images = [x for x in (materialize_image(i) for i in images) if x]
            actions = extract_actions(text)
            clean = strip_actions(text)
            notes = []
            if actions:
                st, notes = apply_actions(actions, conv, BOT_ID)
                await broadcast_all({"t": "ui_update", "settings": st, "actions": actions,
                                     "themes": THEMES + get_setting("custom_themes", [])})
            saved = add_message(conv, "bot", "bot", "bot",
                                {"text": clean, "images": images, "attachments": [], "actions": actions})
            await broadcast(conv, {"t": "msg_push", **saved})
            for n in notes:
                sysmsg = add_message(conv, "system", "sys", "sys", {"text": n})
                await broadcast(conv, {"t": "msg_push", **sysmsg})
            ok["data"] = {"message_id": saved["seq"]}
        elif action == "get_login_info":
            ok["data"] = {"user_id": self.self_id, "nickname": BOT_NAME}
        elif action == "get_version_info":
            ok["data"] = {"app_name": "ai-chat-gateway", "app_version": "0.3.0", "protocol_version": "v11"}
        elif action == "get_status":
            ok["data"] = {"online": True, "good": True}
        elif action in ("can_send_image", "can_send_record"):
            ok["data"] = {"yes": True}
        elif action == "get_group_member_info":
            gid2 = int(params.get("group_id") or 0)
            uid2 = int(params.get("user_id") or 0)
            conv2 = conv_by_num(gid2) or ""
            nick2 = "群成员"
            conn = db()
            row2 = conn.execute("SELECT name FROM device WHERE uid=? ORDER BY rowid DESC LIMIT 1", (uid2,)).fetchone()
            if row2 and row2["name"]:
                nick2 = row2["name"]
            elif uid2 == BOT_ID:
                nick2 = BOT_NAME
            ok["data"] = {"user_id": uid2, "nickname": nick2, "card": nick2,
                          "role": "owner" if conv2 and conv_row(conv2) and conv_row(conv2)["owner"] == uid2 else "member"}
        elif action in ("get_group_list", "get_group_info"):
            items = []
            conn = db()
            for r in conn.execute("SELECT id, name, num FROM conv WHERE id LIKE 'g%'").fetchall():
                items.append({"group_id": int(r["num"] or group_num(r["id"])), "group_name": r["name"], "member_count": len(members_of(r["id"]))})
            ok["data"] = items if action == "get_group_list" else (items[0] if items else {})
        elif action == "get_friend_list":
            ok["data"] = []
        elif action == "get_msg":
            ok["data"] = {"message_id": 0}
        elif action == "delete_msg":
            ok["data"] = None
        else:
            ok["status"] = "failed"
            ok["retcode"] = 1404
            log("未实现接口：" + str(action))
        await self._send(ok)

    async def push(self, conv: str, user_id: int, name: str, segments: list, seq: int, group: bool):
        # 私聊的投递地址跟会话一致（u1001），sender 里保留真实 device uid
        target = user_id
        if not group and conv[1:].isdigit():
            target = int(conv[1:])
        event = {"post_type": "message", "message_type": "group" if group else "private",
                 "sub_type": "normal" if group else "friend", "message_id": seq,
                 "user_id": target, "self_id": self.self_id, "time": int(time.time()),
                 "target": target,
                 "sender": {"user_id": user_id, "nickname": name, "role": "member"},
                 "message": segments,
                 "raw_message": "".join(s.get("data", {}).get("text", "") for s in segments
                                        if s.get("type") == "text")}
        if group:
            row = conv_row(conv)
            event["group_id"] = int(row["num"] or group_num(conv)) if row else group_num(conv)
            # 群里 AstrBot 默认要 @ 才回应，这里自动补一个 @机器人 消息段
            _first = "".join(x.get("data", {}).get("text", "") for x in segments if x.get("type") == "text")
            if not any(x.get("type") == "at" for x in segments) and not _first.strip().startswith("/"):
                event["message"] = [{"type": "at", "data": {"qq": str(self.self_id)}}] + event["message"]
        await self._send(event)

    # ---------- 兼容 /chat/completions ----------
    async def _openai_reply(self, text: str) -> dict:
        import urllib.request
        body = json.dumps({"model": OPENAI_MODEL, "messages": [
            {"role": "system", "content": "你是聊天应用里的 AI，需要改界面时输出 <ui>{\"actions\":[...]}</ui>。"},
            {"role": "user", "content": text}]}).encode()
        req = urllib.request.Request(OPENAI_BASE.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {OPENAI_KEY}"})
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60).read())
        return {"text": json.loads(res)["choices"][0]["message"]["content"], "actions": []}

    async def _webhook_reply(self, conv: str, text: str, meta: dict) -> dict:
        import urllib.request
        body = json.dumps({"conv": conv, "text": text, "meta": meta}).encode()
        req = urllib.request.Request(AGENT_WEBHOOK, data=body, headers={"Content-Type": "application/json"})
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60).read())
        data = json.loads(res)
        if isinstance(data, str):
            return {"text": data, "actions": []}
        return {"text": data.get("text", ""), "actions": data.get("actions") or []}

    async def direct_reply(self, conv: str, text: str, meta: dict) -> dict:
        if AGENT_MODE == "openai":
            return await self._openai_reply(text)
        if AGENT_MODE == "webhook":
            return await self._webhook_reply(conv, text, meta)
        return {"text": "", "actions": []}


bot = AgentLink()


def materialize_image(ref: str) -> str:
    """AstrBot 常发 base64:// 或 file:// 的图片，转成本地 /media/<fid>，否则客户端显示不出来。"""
    ref = (ref or "").strip()
    if not ref:
        return ref
    try:
        if ref.startswith("base64://"):
            b64 = ref[len("base64://"):]
            raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
            ext = ".gif" if raw[:6] in (b"GIF87a", b"GIF89a") else ".png" if raw[:8].startswith(b"\x89PNG") else ".jpg"
            fid = "f-" + uuid.uuid4().hex[:12]
            target = UPLOADS / (fid + ext)
            target.write_bytes(raw)
            kind, mime = "image", {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg"}[ext[1:]]
            conn = db()
            conn.execute("INSERT INTO file (id,name,mime,kind,size,path,text_len,note,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                         (fid, "sticker" + ext, mime, kind, len(raw), str(target), 0, "", int(time.time() * 1000)))
            conn.commit(); conn.close()
            return f"/media/{fid}"
        if ref.startswith("file://"):
            src = pathlib.Path(urllib.parse.unquote(ref[len("file://"):]))
            if src.exists():
                ext = src.suffix.lower()[:6] or ".jpg"
                fid = "f-" + uuid.uuid4().hex[:12]
                target = UPLOADS / (fid + ext)
                target.write_bytes(src.read_bytes())
                mime = "image/gif" if ext == ".gif" else f"image/{ext[1:] if ext[1:] != 'jpg' else 'jpeg'}"
                conn = db()
                conn.execute("INSERT INTO file (id,name,mime,kind,size,path,text_len,note,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                             (fid, src.name, mime, "image", target.stat().st_size, str(target), 0, "", int(time.time() * 1000)))
                conn.commit(); conn.close()
                return f"/media/{fid}"
        return ref
    except Exception as exc:
        log("图片转存失败：" + type(exc).__name__)
        return ""


def parse_chain(chain: list):
    texts, images = [], []
    for seg in chain:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            texts.append(d.get("text", ""))
        elif t == "image":
            images.append(d.get("url") or d.get("file", ""))
        elif t == "record":
            texts.append("[语音]")
    return "".join(texts).strip(), images


def extract_actions(text: str) -> list:
    m = re.search(r"<ui>(.*?)</ui>", text or "", re.S)
    if not m:
        m = re.search(r"```ui\s*(.*?)```", text or "", re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(1).strip())
    except Exception:
        return []
    if isinstance(obj, list):
        return obj
    return obj.get("actions", [])


def strip_actions(text: str) -> str:
    text = re.sub(r"<ui>.*?</ui>", "", text or "", flags=re.S)
    text = re.sub(r"```ui\s*.*?```", "", text, flags=re.S)
    return text.strip()


# ---------------------------------------------------------------- 连接池
clients: dict[str, set] = {}


async def broadcast(conv: str, payload: dict) -> None:
    for ws in list(clients.get(conv, set())):
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            clients.get(conv, set()).discard(ws)


async def broadcast_conv_update() -> None:
    """会话列表按接收者身份裁剪后再下发，避免把别人的私聊（多个“AI 助手”）广播出去。"""
    seen = set()
    for wss in list(clients.values()):
        for ws in list(wss):
            if id(ws) in seen:
                continue
            seen.add(id(ws))
            try:
                await ws.send_text(json.dumps({"t": "conv_update",
                                               "items": conv_list(getattr(ws, "uid", 0))},
                                              ensure_ascii=False))
            except Exception:
                wss.discard(ws)


async def broadcast_all(payload: dict) -> None:
    """并发下发并设置超时，避免某个卡死的连接拖住整个请求。"""
    msg = json.dumps(payload, ensure_ascii=False)
    jobs = []
    for wss in list(clients.values()):
        for ws in list(wss):
            jobs.append((wss, ws, asyncio.wait_for(ws.send_text(msg), timeout=2)))
    if not jobs:
        return
    results = await asyncio.gather(*[j[2] for j in jobs], return_exceptions=True)
    for (wss, ws, _), res in zip(jobs, results):
        if isinstance(res, Exception):
            wss.discard(ws)


# ---------------------------------------------------------------- 路由
async def retention_loop():
    days = int(os.getenv("MSG_RETENTION_DAYS", "0") or 0)
    if days <= 0:
        return
    while True:
        try:
            cutoff = int(time.time() * 1000) - days * 86400000
            conn = db()
            n = conn.execute("DELETE FROM message WHERE ts < ?", (cutoff,)).rowcount
            conn.commit()
            conn.close()
            if n:
                log(f"过期清理：删除 {n} 条超过 {days} 天的消息")
        except Exception as exc:
            log(f"过期清理失败：{type(exc).__name__}")
        await asyncio.sleep(6 * 3600)


async def bot_watchdog():
    if os.getenv("ASTRBOT_WATCHDOG", "1") != "1":
        return
    fail = 0
    while True:
        await asyncio.sleep(30)
        try:
            if getattr(bot, "online", False):
                fail = 0
                continue
            fail += 1
            if fail < 3:
                continue
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-u", os.getenv("ASTRBOT_USER", "ecs-user"), "env",
                "XDG_RUNTIME_DIR=/run/user/1000", "systemctl", "--user", "restart", "astrbot",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            log("看门狗：AstrBot 失联超过 90 秒，已尝试重启")
            fail = 0
        except Exception as exc:
            log(f"看门狗异常：{type(exc).__name__}")


@app.on_event("startup")
async def startup():
    init_db()
    try:
        now0 = time.time()
        for f in list(UPLOADS.glob(".thumb-*")):
            f.unlink()
        for f in list(THUMBS.glob("*.jpg")):
            if now0 - f.stat().st_mtime > 30 * 86400:
                f.unlink()
    except Exception:
        pass
    try:
        logp = DATA / "diag.log"
        if logp.exists() and logp.stat().st_size > 1024 * 1024:
            logp.unlink()
    except Exception:
        pass
    # 历史设备可能没有密钥，会让鉴权中间件放行；启动时补齐
    try:
        conn = db()
        rows = conn.execute("SELECT device_id FROM device WHERE device_id!='bot' AND (secret IS NULL OR secret='')").fetchall()
        conn.close()
        for r in rows:
            ensure_secret(r["device_id"])
        if rows:
            log(f"已为 {len(rows)} 台历史设备补齐密钥")
    except Exception as exc:
        log(f"补齐设备密钥失败：{type(exc).__name__}")
    resolve_admin_codes()
    try:
        load_plugins()
    except Exception as e:
        log(f"插件加载跳过: {e}")
    try:
        purge_deleted()
    except Exception:
        pass
    # 默认改为白色界面：历史深色默认一次性迁移，用户自己选的其它主题不动
    try:
        st0 = ui_state()
        if st0.get("theme") == "dark_ink" and not get_setting("theme_light_migrated", False):
            st0["theme"] = "light_clean"
            set_setting("ui", st0)
            set_setting("theme_light_migrated", True)
            log("默认主题已切换为浅色")
    except Exception:
        pass
    # 消息过期清理（默认关闭，MSG_RETENTION_DAYS=30 开启）
    try:
        asyncio.create_task(retention_loop())
    except Exception:
        pass
    # AstrBot 失联看门狗（ASTRBOT_WATCHDOG=0 可关闭）
    try:
        asyncio.create_task(bot_watchdog())
    except Exception:
        pass
    await bot.start()
    info = net_info()
    log(f"网关就绪 base={info['base']} mode={info['mode']} token={info['token_required']}")



LANDING_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>加入群栖</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1218;color:#e8ecf2;font:16px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:24px}
.card{width:100%;max-width:380px;background:#161b24;border:1px solid #232a36;border-radius:18px;padding:26px 22px;text-align:center}
.logo{width:64px;height:64px;border-radius:18px;margin:0 auto 16px;background:#1fb6a0;display:flex;align-items:center;
justify-content:center;font-size:26px;font-weight:700;color:#fff}
h1{font-size:19px;margin:0 0 6px;font-weight:600}p{color:#93a0b3;font-size:13px;margin:0 0 18px}
code{display:block;background:#0f1218;border:1px solid #232a36;border-radius:10px;padding:11px;margin:0 0 18px;
font:700 16px/1 ui-monospace,Menlo,monospace;letter-spacing:2px;color:#4fd6bd;word-break:break-all}
a{display:block;text-decoration:none;border-radius:12px;padding:13px;font-size:15px;font-weight:600;margin-bottom:10px}
.a1{background:#1fb6a0;color:#fff}.a2{background:#1d2430;color:#dfe6f0;border:1px solid #2c3444}
.tip{font-size:12px;color:#6d7a8d;margin-top:8px}
</style></head><body><div class="card">
<div class="logo">群</div><h1>邀请你加入群聊</h1>
<p>邀请码 <b style="color:#4fd6bd">{{CODE}}</b>　正在尝试打开「群栖」…</p>
<code>{{CODE}}</code>
<a class="a1" id="open" href="{{INTENT}}">在「群栖」中打开并进群</a>
<a class="a2" href="{{APK}}">还没装？下载安装包</a>
<div class="tip" id="tip">装好之后回到本页点上面按钮，会自动进群</div>
</div>
<script>
(function(){
  var fallback = "{{APK}}";
  var intentUrl = "{{INTENT}}";
  function go(){ try{ location.href = intentUrl; }catch(e){} setTimeout(function(){
    var t=document.getElementById('tip'); if(t) t.textContent='没反应？点上面的按钮，或先下载安装包';
  }, 1500); }
  go();
})();
</script></body></html>"""


@app.get("/j/{code}")
async def join_landing(code: str, request: Request):
    """邀请落地页：有 App 直接拉起并进群，没装 App 自动下载安装包。"""
    safe = "".join(ch for ch in code if ch.isalnum() or ch in "-_")[:32]
    apk = str(request.base_url).rstrip("/") + "/api/apk"
    intent = ("intent://join/" + safe + "#Intent;scheme=qunqi;package=com.aichat.gateway;"
              "S.browser_fallback_url=" + urllib.parse.quote(apk, safe="") + ";end")
    html = (LANDING_HTML.replace("{{CODE}}", safe)
            .replace("{{INTENT}}", intent)
            .replace("{{APK}}", apk))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})

@app.get("/")
async def index(join: str = ""):
    return FileResponse(STATIC / "app.html")


@app.get("/manifest.webmanifest")
async def manifest():
    info = net_info()
    return JSONResponse({
        "name": "AI Chat 网关",
        "short_name": "AI Chat",
        "description": "自建 AI 聊天与群聊，网关直连 AstrBot",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#121417",
        "theme_color": "#121417",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.get("/builtin/{name}")
async def builtin_bg(name: str):
    """内置动图背景：APK 内自带一份，浏览器端从服务器读同名文件兜底。"""
    if not re.match(r"^bg[0-9]{1,2}(-thumb)?\.webp$", name or ""):
        raise HTTPException(404, "not found")
    p = STATIC / "builtin" / name
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=2592000"})


@app.get("/sw.js")
async def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/icon-192.png")
async def icon192():
    return FileResponse(STATIC / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def icon512():
    return FileResponse(STATIC / "icon-512.png", media_type="image/png")


def public_ui() -> dict:
    """匿名可见的外观：保留主题/尺寸等参数，去掉头像与背景的图片地址。"""
    st = json.loads(json.dumps(ui_state()))
    try:
        if isinstance(st.get("avatar"), dict):
            st["avatar"]["url"] = ""
            st["avatar"]["me"] = ""
        if isinstance(st.get("bg"), dict):
            st["bg"]["url"] = ""
            st["bg"]["gradient"] = ["", ""] if st["bg"].get("gradient") else st["bg"].get("gradient")
    except Exception:
        pass
    return st


@app.get("/api/net")
async def api_net(request: Request):
    remember_host(request)
    info = net_info()
    # 未带有效密钥时不下发内网地址，避免匿名探到服务器拓扑
    sec = request.headers.get("x-device-secret") or request.query_params.get("secret", "")
    if not sec:
        info.pop("lan_ip", None)
        info.pop("base", None)
    return info


def require_device(device: str) -> int:
    if not device:
        raise HTTPException(403, "缺少设备标识")
    conn = db()
    row = conn.execute("SELECT uid FROM device WHERE device_id=?", (device,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(403, "设备未注册")
    return int(row["uid"])


def require_req_device(request: Request, device: str = "") -> tuple:
    """从请求头/查询串取设备号与密钥并校验；缺设备号时按密钥反查。"""
    dev = device or request.headers.get("x-device") or request.query_params.get("device", "")
    sec = request.headers.get("x-device-secret") or request.query_params.get("secret", "")
    if not dev and sec:
        conn0 = db()
        r0 = conn0.execute("SELECT device_id FROM device WHERE secret=?", (sec,)).fetchone()
        conn0.close()
        if r0:
            dev = r0["device_id"]
    uid = require_device(dev)
    stored = secret_of(dev) or ""
    if not sec or not stored or not secrets.compare_digest(sec, stored):
        raise HTTPException(403, "设备密钥不正确")
    return uid, dev


def _read_ver_file(name: str) -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name)) as f:
            return f.read().strip()
    except Exception:
        return ""


# 后端版本与前端/安装包版本分开：后端 0.53.1，前端 0.53.0，中间位保持一致
BACKEND_VERSION = os.getenv("BACKEND_VERSION") or _read_ver_file("VERSION") or "0.1"
APP_VERSION = os.getenv("APP_VERSION") or _read_ver_file("APP_VERSION") or BACKEND_VERSION


@app.get("/api/apk")
async def api_apk():
    """固定下载地址，避免 CDN 临时链接过期导致更新失败。
    文件名带版本号并禁用缓存，浏览器/下载器才不会拿旧包当新包装上。"""
    p = STATIC / "AI-Chat.apk"
    if not p.exists():
        raise HTTPException(404, "APK 未上传")
    ver = str(get_setting("app_version", APP_VERSION) or APP_VERSION).strip() or "latest"
    ver = "".join(ch for ch in ver if ch.isalnum() or ch in "._-")
    return FileResponse(p, media_type="application/vnd.android.package-archive",
                        filename=f"Qunqi-v{ver}.apk",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                                 "Pragma": "no-cache"})


@app.get("/api/version")
async def api_version():
    av = get_setting("app_version", APP_VERSION)
    apk_url = get_setting("apk_url", "")
    if apk_url and "?" not in apk_url:
        # 带版本号查询串，绕过客户端和中间缓存
        apk_url = apk_url + "?v=" + str(av)
    return {"version": BACKEND_VERSION,
            "app_version": av,
            "apk_url": apk_url,
            "notes": get_setting("apk_notes", ""),
            "pushed_at": get_setting("apk_pushed_at", 0),
            **_apk_sha()}



@app.post("/api/admin/ai_visibility")
async def admin_ai_visibility(request: Request):
    body = await request.json()
    require_admin("g1001", body.get("device", ""))
    cur = get_setting("sys", {})
    cur["show_ai_all"] = bool(body.get("value", True))
    set_setting("sys", cur)
    await broadcast_conv_update()
    return {"ok": True, "show_ai_all": cur["show_ai_all"]}


@app.post("/api/admin/push_update")
async def api_push_update(request: Request):
    body = await request.json()
    require_admin("g1001", body.get("device", ""))
    ver = str(body.get("version") or "").strip()
    if not ver:
        raise HTTPException(400, "缺少版本号")
    set_setting("apk_url", body.get("apk_url", ""))
    set_setting("apk_notes", body.get("notes", ""))
    set_setting("apk_pushed_at", int(time.time()))
    global APP_VERSION
    APP_VERSION = ver
    set_setting("app_version", ver)
    payload = {"t": "app_update", "version": ver, "apk_url": body.get("apk_url", ""),
               "notes": body.get("notes", "")}
    await broadcast_all(payload)
    return {"ok": True, **payload}


@app.get("/api/read")
async def api_read(device: str = ""):
    uid = require_device(device)
    data = get_setting("read_seq", {}) or {}
    return {"reads": data.get(str(uid), {})}


@app.post("/api/read")
async def api_read_set(request: Request):
    body = await request.json()
    uid = require_device(body.get("device", ""))
    conv = str(body.get("conv") or "")
    seq = int(body.get("seq") or 0)
    if not conv:
        raise HTTPException(400, "缺少会话")
    data = get_setting("read_seq", {}) or {}
    per = data.get(str(uid), {})
    if seq > int(per.get(conv, 0)):
        per[conv] = seq
        data[str(uid)] = per
        set_setting("read_seq", data)
    return {"ok": True}


@app.post("/api/admin/grant")
async def api_grant(request: Request):
    body = await request.json()
    require_admin("g1001", body.get("device", ""))
    uid = int(body.get("uid") or 0)
    if not uid:
        raise HTTPException(400, "缺少 uid")
    lst = [int(x) for x in (get_setting("admin_uids", []) or [])]
    if uid not in lst:
        lst.append(uid); set_setting("admin_uids", lst)
    await broadcast_conv_update()
    return {"ok": True, "admin_uids": lst}


@app.post("/api/admin/revoke")
async def api_revoke(request: Request):
    """撤销由 AI/管理员授予的管理员（BOT_ID 与 ADMIN_CODES 初始管理员不可撤销）。"""
    body = await request.json()
    require_admin("g1001", body.get("device", ""))
    uid = int(body.get("uid") or 0)
    if not uid:
        raise HTTPException(400, "缺少 uid")
    if uid == BOT_ID:
        raise HTTPException(400, "不能撤销服务器 AI")
    lst = [int(x) for x in (get_setting("admin_uids", []) or [])]
    if uid in lst:
        lst.remove(uid)
        set_setting("admin_uids", lst)
    await broadcast_conv_update()
    return {"ok": True, "admin_uids": lst}


@app.get("/api/health")
async def health():
    ok_db = True
    try:
        conn = db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception:
        ok_db = False
    return {"ok": ok_db and bot.online, "db": ok_db, "agent": bot.online,
            "mode": AGENT_MODE, "time": int(time.time())}


@app.get("/api/status")
async def status():
    net = net_info()
    net.pop("lan_ip", None)
    return {"agent": bot.online, "mode": AGENT_MODE, "settings": public_ui(),
            "themes": THEMES + get_setting("custom_themes", []), "net": net}


@app.post("/api/device")
async def api_device(request: Request):
    ip = request.client.host if request.client else "?"
    key = "dev:" + ip
    now = int(time.time())
    win = _up_rl.get(key) or (0, 0)
    if now - win[0] >= 60:
        win = (now, 0)
    if win[1] >= 15:
        raise HTTPException(429, "注册过于频繁")
    _up_rl[key] = (win[0], win[1] + 1)
    body = await request.json()
    prof = device_profile(body.get("device_id", ""), body.get("name", ""), body.get("kind", "user"), with_secret=True)
    return prof


@app.get("/api/settings")
async def get_settings():
    return {"settings": public_ui(), "themes": THEMES + get_setting("custom_themes", [])}


@app.get("/api/ui")
async def get_ui(request: Request, device: str = ""):
    """管理员读取服端外观，用于「恢复默认背景」把服务器默认同步回本机。"""
    require_admin("", device)
    return {"settings": ui_state()}


@app.put("/api/settings")
async def put_settings(request: Request, device: str = ""):
    body = await request.json()
    uid0, _ = require_req_device(request, device)
    if not is_admin(int(uid0)):
        raise HTTPException(403, "仅管理员可修改全站外观")
    st = ui_state()

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    # AI 头像属于全站身份，只有管理员能改；普通用户的改动被忽略
    avatar_ignored = False
    incoming_avatar = (body.get("avatar") or {})
    if incoming_avatar and "me" in incoming_avatar:
        # avatar.me 是个人的头像，存在各自 device 记录里，不能写进全站设置
        incoming_avatar = dict(incoming_avatar)
        incoming_avatar.pop("me", None)
        body = dict(body)
        body["avatar"] = incoming_avatar
    if incoming_avatar and "url" in incoming_avatar:
        uid = uid_by_device(device) if device else None
        if not (uid and is_admin(int(uid))) and incoming_avatar["url"] != (st.get("avatar") or {}).get("url"):
            incoming_avatar = dict(incoming_avatar)
            incoming_avatar.pop("url", None)
            body = dict(body)
            body["avatar"] = incoming_avatar
            avatar_ignored = True
    merge(st, body)
    st["avatar"]["size"] = max(28, min(64, int(st["avatar"]["size"])))
    st["avatar"]["shape"] = st["avatar"]["shape"] if st["avatar"]["shape"] in ("circle", "square") else "circle"
    av = st["avatar"]
    av["px"] = max(0, min(100, int(av.get("px", 50) or 50)))
    av["py"] = max(0, min(100, int(av.get("py", 50) or 50)))
    av["scale"] = max(1.0, min(2.4, float(av.get("scale", 1.0) or 1.0)))
    st["bg"]["brightness"] = max(0.2, min(1.3, float(st["bg"]["brightness"])))
    st["bg"]["overlay"] = max(0, min(0.9, float(st["bg"]["overlay"])))
    st["bg"]["blur"] = max(0, min(24, int(st["bg"]["blur"])))
    st["bg"]["rotate"] = max(0, min(180, int(st["bg"].get("rotate") or 0)))
    st["font"]["size"] = max(13, min(20, int(st["font"]["size"])))
    fcol = str(st["font"].get("color") or "").strip()
    st["font"]["color"] = fcol if re.match(r"^#[0-9a-fA-F]{6}$", fcol) else ""
    if json.dumps(st, sort_keys=True, ensure_ascii=False) == json.dumps(ui_state(), sort_keys=True, ensure_ascii=False):
        return {"ok": True, "settings": st, "unchanged": True, "avatar_ignored": avatar_ignored}
    set_setting("ui", st)
    await broadcast_all({"t": "ui_update", "settings": st,
                         "themes": THEMES + get_setting("custom_themes", [])})
    return {"ok": True, "settings": st}


@app.post("/api/themes")
async def save_theme(request: Request):
    body = await request.json()
    device = str(body.get("device") or "").strip()
    secret = request.headers.get("X-Device-Secret", "")
    if not device:
        raise HTTPException(403, "缺少设备标识")
    stored = secret_of(device)
    if not stored or secret != stored:
        raise HTTPException(403, "设备密钥不正确")
    if PUBLIC_MODE:
        require_admin("", device)
    name = (body.get("name") or "我的主题")[:20]
    themes = get_setting("custom_themes", [])
    # 同名覆盖，避免「副本」无限繁殖
    for t in themes:
        if t.get("name") == name:
            t["v"] = body.get("v") or t.get("v") or {}
            set_setting("custom_themes", themes)
            return {"ok": True, "theme": t, "updated": True}
    if len(themes) >= 20:
        raise HTTPException(409, "自定义主题已达上限 20 个")
    theme = {"id": "custom_" + uuid.uuid4().hex[:6], "name": name,
             "v": body.get("v") or {}}
    themes.append(theme)
    set_setting("custom_themes", themes)
    # 只把新主题加进列表，不改变任何人的当前主题
    await broadcast_all({"t": "themes_update", "themes": THEMES + themes})
    return {"ok": True, "theme": theme}


@app.delete("/api/themes/{tid}")
async def del_theme(tid: str, device: str = "", request: Request = None):
    if not device:
        raise HTTPException(403, "缺少设备标识")
    stored = secret_of(device)
    if not stored or (request is not None and request.headers.get("X-Device-Secret", "") != stored):
        raise HTTPException(403, "设备密钥不正确")
    if PUBLIC_MODE:
        require_admin("", device)
    themes = [x for x in get_setting("custom_themes", []) if x["id"] != tid]
    set_setting("custom_themes", themes)
    st = ui_state()
    if st["theme"] == tid:
        st["theme"] = "dark_ink"
        set_setting("ui", st)
    await broadcast_all({"t": "ui_update", "settings": st, "themes": THEMES + themes})
    return {"ok": True}


@app.get("/api/convs")
async def get_convs(request: Request, device: str = ""):
    remember_host(request)
    uid, _ = require_req_device(request, device)
    ensure_private(uid)
    return {"items": conv_list(uid), "net": net_info()}


@app.post("/api/convs")
async def create_conv(request: Request):
    """建群只允许 AI 在服务器内部完成；前端/外部接口一律拒绝。"""
    raise HTTPException(403, "群聊只能由 AI 创建：请在聊天里对我说「建个群叫XXX」")


@app.post("/api/convs/join")
async def join_conv(request: Request):
    ip = request.client.host if request.client else "?"
    key = "join:" + ip
    now = int(time.time())
    win = _up_rl.get(key) or (0, 0)
    if now - win[0] >= 3600:
        win = (now, 0)
    if win[1] >= 30:
        raise HTTPException(429, "加入太频繁，请稍后再试")
    _up_rl[key] = (win[0], win[1] + 1)
    body = await request.json()
    prof = device_profile(body.get("device", ""), body.get("nick", ""))
    g = join_group(body.get("invite", ""), prof["uid"])
    await broadcast_conv_update()
    return {"ok": True, **g}


def require_admin(cid: str, device: str) -> int:
    """群管理动作：只有服务器 AI、管理员、群主能做。"""
    if device in ("bot", "BOT", "ai", "AI", str(BOT_ID)):
        return BOT_ID
    uid = uid_by_device(device)
    if uid is None:
        raise HTTPException(403, "需要 device 参数")
    if is_admin(uid):
        return uid
    # 群创建者：群内没有其他人类管理员时，可以改群名/群头像
    row = conv_row(cid) if cid else None
    if row and row["type"] == "group" and int(row["owner"] or 0) == uid:
        others = [m for m in json.loads(row["members"] or "[]") if int(m) != uid and int(m) != BOT_ID]
        if not any(is_admin(int(m)) for m in others):
            return uid
    raise HTTPException(403, "只有管理员或群创建者能改群共用内容")


@app.patch("/api/convs/{cid}")
async def patch_conv(cid: str, request: Request, device: str = ""):
    body = await request.json()
    require_admin(cid, device or body.get("device", ""))
    if "name" in body:
        rename_conv(cid, (body["name"] or "")[:24])
    if "avatar" in body:
        conn = db()
        conn.execute("UPDATE conv SET avatar=? WHERE id=?", (body["avatar"], cid))
        conn.commit()
        conn.close()
    if "announce" in body:
        announce(cid, body["announce"] or "")
    if "mute" in body:
        mute_conv(cid, int(body["mute"] or 0))
    if "mute_minutes" in body:
        mute_minutes(cid, int(body["mute_minutes"] or 0))
    if "at_only" in body:
        conn = db()
        conn.execute("UPDATE conv SET at_only=? WHERE id=?", (1 if body["at_only"] else 0, cid))
        conn.commit(); conn.close()
    await broadcast_conv_update()
    return {"ok": True}


@app.delete("/api/convs/{cid}")
async def delete_conv(cid: str, device: str = ""):
    require_admin(cid, device)
    conn = db()
    conn.execute("UPDATE conv SET deleted_at=? WHERE id=?", (int(time.time()), cid))
    conn.commit()
    conn.close()
    await broadcast_conv_update()
    return {"ok": True, "deleted": True, "restore_days": 7}


@app.get("/api/convs/deleted")
async def deleted_convs(device: str = ""):
    require_admin("g1001", device)
    conn = db()
    cut = int(time.time()) - 7 * 86400
    rows = conn.execute("SELECT id,type,name,deleted_at FROM conv WHERE deleted_at>? ORDER BY deleted_at DESC",
                        (cut,)).fetchall()
    conn.close()
    return {"items": [{"id": r["id"], "type": r["type"], "name": r["name"], "deleted_at": r["deleted_at"]} for r in rows]}


@app.post("/api/convs/{cid}/restore")
async def restore_conv(cid: str, request: Request):
    body = await request.json()
    require_admin(cid, body.get("device", ""))
    conn = db()
    conn.execute("UPDATE conv SET deleted_at=0 WHERE id=?", (cid,))
    conn.commit(); conn.close()
    await broadcast_conv_update()
    return {"ok": True}


@app.post("/api/dm")
async def api_dm(request: Request):
    body = await request.json()
    device = body.get("device", "")
    me = device_profile(device).get("uid")
    target = int(body.get("target") or 0)
    if not target:
        raise HTTPException(400, "缺少目标")
    if target == BOT_ID:
        ensure_private(int(me))
        return {"ok": True, "conv": {"id": f"u{me}", "type": "private", "name": BOT_NAME,
                                     "avatar": "", "members": [int(me), BOT_ID], "last_seq": 0}}
    a, b = sorted([int(me), target])
    cid = f"d{a}-{b}"
    conn = db()
    row = conn.execute("SELECT * FROM conv WHERE id=?", (cid,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO conv (id,type,name,avatar,members,created_at,invite,owner) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, "dm", "", "", json.dumps([a, b]), int(time.time()), "", 0))
        conn.commit()
    conn.close()
    return {"ok": True, "conv": {"id": cid, "type": "dm", "name": uid_name(target),
                                 "avatar": "", "members": [a, b], "last_seq": 0}}


@app.delete("/api/messages/{cid}/{seq}")
async def api_delete_message(cid: str, seq: int, device: str = ""):
    require_member(cid, device)
    conn = db()
    conn.execute("DELETE FROM message WHERE conv=? AND seq=?", (cid, seq))
    conn.commit()
    conn.close()
    await broadcast(cid, {"t": "msg_del", "conv": cid, "seq": seq})
    return {"ok": True}



@app.post("/api/convs/{cid}/bg")
async def api_conv_bg(cid: str, request: Request):
    body = await request.json()
    uid = uid_by_device(body.get("device", ""))
    if uid is None or not is_admin(uid):
        raise HTTPException(403, "只有管理员可以更换群背景")
    bg = set_conv_bg(cid, body)
    await broadcast(cid, {"t": "conv_bg", "conv": cid, "bg": bg})
    await broadcast_conv_update()
    return {"ok": True, "bg": bg}



@app.get("/api/push/poll")
async def push_poll(request: Request, device: str = "", since: int = 0):
    """给终端做后台消息轮询：只返回该设备用户所在会话里、别人发的新消息。"""
    uid, _ = require_req_device(request, device)
    conn = db()
    rows = conn.execute("SELECT * FROM conv WHERE deleted_at=0").fetchall()
    convs = {}
    for r in rows:
        try:
            members = [int(x) for x in json.loads(r["members"] or "[]")]
        except Exception:
            members = []
        if r["type"] == "private":
            if r["id"] != f"u{uid}":
                continue
        elif r["type"] == "dm":
            if uid not in members or BOT_ID in members:
                continue
        elif r["type"] == "group":
            if uid not in members and r["id"] != "g1001":
                continue
        convs[r["id"]] = r
    items = []
    mx = since
    for cid, r in convs.items():
        msgs = conn.execute("SELECT * FROM message WHERE conv=? AND ts>? ORDER BY ts LIMIT 40", (cid, since)).fetchall()
        for m in msgs:
            mx = max(mx, int(m["ts"] or 0))
            if str(m["sender"]) == str(uid) or m["role"] == "user" and str(m["sender"]) == str(uid):
                continue
            try:
                c = json.loads(m["content"] or "{}")
            except Exception:
                c = {}
            text = (c.get("text") or "").strip()
            if not text:
                text = "[图片]" if (c.get("images") or c.get("image")) else "[附件]"
            items.append({"conv": cid,
                          "conv_name": r["name"] or ("AI 助手" if r["type"] == "private" else "会话"),
                          "from": uid_name(int(m["sender"])) if str(m["sender"]).isdigit() else "AI 助手",
                          "text": text[:120], "ts": int(m["ts"] or 0), "seq": int(m["seq"] or 0)})
    conn.close()
    items.sort(key=lambda x: x["ts"])
    return {"items": items[-20:], "max_ts": mx}


@app.get("/api/convs/{cid}/members")
async def get_members(cid: str):
    ms = members_of(cid)
    row = conv_row(cid)
    owner = row["owner"] if row else 0
    return {"items": [{"uid": m, "name": uid_name(m), "bot": m == BOT_ID,
                       "admin": is_admin(m)} for m in ms],
            "invite": row["invite"] if row else "", "owner": owner,
            "mute_until": (row["mute_until"] if row else 0) or 0,
            "announce": (row["announce"] if row else "") or "",
            "pin": (row["pin"] if row else "") or "",
            "at_only": (row["at_only"] if row else 0) or 0}


@app.delete("/api/convs/{cid}/members/{uid}")
async def kick_member(cid: str, uid: int, device: str = ""):
    me = require_admin(cid, device)
    if int(uid) == BOT_ID:
        raise HTTPException(403, "不能移出服务器 AI")
    remove_member(cid, uid)
    await broadcast_conv_update()
    return {"ok": True}


@app.post("/api/convs/{cid}/leave")
async def leave_conv(cid: str, request: Request):
    body = await request.json()
    me = device_profile(body.get("device", "")).get("uid")
    if not me:
        raise HTTPException(403, "缺少 device")
    row = conv_row(cid)
    if row is None:
        raise HTTPException(404, "会话不存在")
    if row["type"] == "private":
        raise HTTPException(400, "私聊不能退出")
    remove_member(cid, int(me))
    await broadcast_conv_update()
    return {"ok": True}


@app.post("/api/admin/ban")
async def api_ban(request: Request):
    body = await request.json()
    require_admin("g1001", body.get("device", ""))
    uid = int(body.get("uid") or 0)
    if not uid:
        raise HTTPException(400, "缺少 uid")
    lst = banned_uids()
    if uid not in lst:
        lst.append(uid); set_banned(lst)
    # 从所有群里移除
    conn = db()
    for r in conn.execute("SELECT id, members FROM conv WHERE type IN ('group','dm')").fetchall():
        try:
            ms = [int(x) for x in json.loads(r["members"] or "[]")]
        except Exception:
            continue
        if uid in ms:
            conn.execute("UPDATE conv SET members=? WHERE id=?",
                         (json.dumps([m for m in ms if m != uid]), r["id"]))
    conn.commit(); conn.close()
    await broadcast_conv_update()
    return {"ok": True, "banned": lst}


@app.get("/api/me")
async def api_me(request: Request, device: str = "", kind: str = "user"):
    """已注册设备必须带密钥；未注册设备允许首次自助注册并下发密钥。"""
    if not device:
        raise HTTPException(400, "缺少 device")
    if secret_of(device):
        require_req_device(request, device)
        return device_profile(device, "", kind)
    # 设备号在服务端不存在：首次启动自动注册并下发一次密钥。
    # 与 /api/device 一样按 IP 限流，避免匿名刷库建号。
    ip = request.client.host if request.client else "?"
    now = int(time.time())
    key = "me:" + ip
    win = _up_rl.get(key) or (0, 0)
    if now - win[0] >= 60:
        win = (now, 0)
    if win[1] >= 15:
        raise HTTPException(429, "注册过于频繁")
    _up_rl[key] = (now, win[1] + 1)
    return device_profile(device, "", kind, with_secret=True)


@app.post("/api/restore")
async def api_restore(request: Request):
    """恢复码：把当前新设备绑到旧账号的 uid，数据、头像、昵称一起继承。"""
    ip = request.client.host if request.client else "?"
    key = "rst:" + ip
    now = int(time.time())
    gwin = _RST_GLOBAL
    if now - gwin[0] >= 600:
        _RST_GLOBAL[0], _RST_GLOBAL[1] = now, 0
    if _RST_GLOBAL[1] >= 15:
        raise HTTPException(429, "恢复码尝试次数过多，请十分钟后再试")
    win = _up_rl.get(key) or (0, 0)
    if now - win[0] >= 600:
        win = (now, 0)
    if win[1] >= 10:
        raise HTTPException(429, "尝试次数过多，请十分钟后再试")
    _up_rl[key] = (win[0], win[1] + 1)
    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "缺少身份码")
    conn = db()
    old = conn.execute("SELECT device_id FROM ident WHERE code=?", (code,)).fetchone()
    if old is None:
        conn.close()
        _RST_GLOBAL[1] += 1
        raise HTTPException(404, "身份码不存在")
    old_dev = old["device_id"]
    old_row = conn.execute("SELECT * FROM device WHERE device_id=?", (old_dev,)).fetchone()
    if old_row is None:
        conn.close()
        raise HTTPException(404, "身份码没有对应设备")
    new_dev = (body.get("device_id") or "").strip()[:64]
    prof = device_profile(old_dev, "", "user", with_secret=True)
    if new_dev and new_dev != old_dev:
        ex = conn.execute("SELECT * FROM device WHERE device_id=?", (new_dev,)).fetchone()
        if ex:
            conn.execute("UPDATE device SET uid=?, name=?, avatar=? WHERE device_id=?",
                         (old_row["uid"], old_row["name"], old_row["avatar"] if "avatar" in old_row.keys() else "", new_dev))
        else:
            conn.execute("INSERT INTO device (device_id,uid,name,created_at,secret,avatar) VALUES (?,?,?,?,?,?)",
                         (new_dev, old_row["uid"], old_row["name"], int(time.time()),
                          ensure_secret(new_dev), old_row["avatar"] if "avatar" in old_row.keys() else ""))
        conn.commit()
        # 换机迁移：只保留当前这台设备，同 uid 的旧设备一并清掉，避免多台互相踢密钥
        for r0 in conn.execute("SELECT device_id FROM device WHERE uid=? AND device_id<>?",
                               (old_row["uid"], new_dev)).fetchall():
            conn.execute("DELETE FROM device WHERE device_id=?", (r0["device_id"],))
            conn.execute("DELETE FROM ident WHERE device_id=?", (r0["device_id"],))
        conn.commit()
        # 身份码是唯一的：换机后把它从旧设备迁到新设备
        conn.execute("DELETE FROM ident WHERE device_id=? OR code=?", (new_dev, code))
        conn.execute("INSERT INTO ident (device_id,code,kind,created_at) VALUES (?,?,?,?)",
                     (new_dev, code, "user", int(time.time())))
        conn.commit()
        conn.close()
        prof = device_profile(new_dev, "", "user", with_secret=True)
        prof["code"] = code
        # 恢复后必须把新设备当前的密钥发回去，否则客户端会继续拿旧密钥重连
        prof["secret"] = ensure_secret(new_dev)
    else:
        conn.close()
    conn = db()
    conn.close()
    _RST_GLOBAL[1] = 0
    return {"ok": True, **prof}


@app.post("/api/profile")
async def api_profile(request: Request):
    body = await request.json()
    device = (body.get("device") or "").strip()[:64]
    if not device:
        raise HTTPException(400, "缺少 device")
    name = (body.get("name") or "").strip()[:16]
    avatar = (body.get("avatar") or "").strip()[:400]
    uid = uid_by_device(device)
    conn = db()
    row = conn.execute("SELECT * FROM device WHERE device_id=?", (device,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "设备不存在")
    if uid in banned_uids():
        conn.close()
        raise HTTPException(403, "该账号已被移出服务器")
    if name:
        conn.execute("UPDATE device SET name=? WHERE device_id=?", (name, device))
    if "avatar" in row.keys() or True:
        conn.execute("UPDATE device SET avatar=? WHERE device_id=?", (avatar, device))
    conn.commit()
    conn.close()
    return {"ok": True, "name": name or row["name"], "avatar": avatar}


def uid_by_device(device_id: str) -> int | None:
    if not device_id:
        return None
    conn = db()
    row = conn.execute("SELECT uid FROM device WHERE device_id=?", (device_id,)).fetchone()
    conn.close()
    if row:
        return int(row["uid"])
    h = hashlib.sha1(device_id.encode()).hexdigest()
    return 100000 + int(h[:7], 16) % 800000


def require_member(conv: str, device: str):
    """群会话必须带 device，且必须是成员；私聊按 uid 会话归属校验。"""
    uid = uid_by_device(device)
    if uid is None:
        raise HTTPException(403, "需要 device 参数")
    if conv.startswith("g"):
        if uid not in members_of(conv):
            raise HTTPException(403, "不是该群成员")
    elif conv.startswith("u"):
        try:
            other = int(conv[1:])
        except Exception:
            raise HTTPException(403, "会话标识不正确")
        if uid != other and other != BOT_ID:
            raise HTTPException(403, "无权访问该会话")
    elif conv.startswith("d"):
        try:
            a, b = conv[1:].split("-")
            pair = {int(a), int(b)}
        except Exception:
            raise HTTPException(403, "会话标识不正确")
        if uid not in pair:
            raise HTTPException(403, "无权访问该会话")
    else:
        raise HTTPException(403, "会话标识不正确")


@app.get("/api/messages")
async def api_messages(conv: str = "u1001", after: int = 0, limit: int = 100, device: str = ""):
    require_member(conv, device)
    if conv.startswith("g") and not conv_row(conv):
        raise HTTPException(404, "会话不存在")
    items = history(conv, min(limit, 300), after)
    last = items[-1]["seq"] if items else after
    return {"items": items, "last_seq": last}


@app.get("/api/agent/poll")
async def api_agent_poll(conv: str = "", after: int = 0, limit: int = 50, device: str = ""):
    """给外部 agent 的轻量拉取：HTTP 轮询即可，无需 WebSocket。"""
    if not conv or not device:
        raise HTTPException(400, "需要 conv 和 device")
    require_member(conv, device)
    items = history(conv, min(max(limit, 1), 200), max(after, 0))
    return {"items": items, "last_seq": items[-1]["seq"] if items else after}


@app.post("/api/agent/send")
async def api_agent_send(request: Request):
    """外部 agent 用一条 HTTP 请求发消息；不需要实现 WebSocket 协议。"""
    body = await request.json()
    conv = (body.get("conv") or "").strip()
    device = (body.get("device") or "").strip()
    if not conv or not device:
        raise HTTPException(400, "需要 conv 和 device")
    require_member(conv, device)
    uid = uid_by_device(device)
    if uid in banned_uids():
        raise HTTPException(403, "该账号已被移出服务器")
    row = conv_row(conv)
    if row and row["mute_until"] and int(row["mute_until"]) > int(time.time()) and not is_admin(uid):
        raise HTTPException(403, "你已被禁言")
    text = (body.get("text") or "").strip()[:4000]
    if not text:
        raise HTTPException(400, "text 不能为空")
    content = {"text": text}
    if body.get("reply_to"):
        content["reply_to"] = int(body["reply_to"])
    if body.get("mentions"):
        content["mentions"] = [int(x) for x in body["mentions"]][:20]
    msg = add_message(conv, str(uid), "agent", "text", content)
    await broadcast(conv, {"t": "msg_push", **msg})
    return {"ok": True, **msg}


@app.get("/api/search")
async def api_search(conv: str = "u1001", q: str = "", device: str = "", limit: int = 60,
                     who: str = "", kind: str = "", days: str = "", offset: int = 0):
    require_member(conv, device)
    uid = uid_by_device(device) or 0
    q = (q or "").strip()
    if not q and not who and not kind and not days:
        return {"items": [], "total": 0}
    conn = db()
    if q:
        rows = conn.execute(
            "SELECT * FROM message WHERE conv=? AND content LIKE ? ORDER BY seq DESC LIMIT ?",
            (conv, "%" + q + "%", 1000)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM message WHERE conv=? ORDER BY seq DESC LIMIT 1000",
                            (conv,)).fetchall()
    conn.close()
    try:
        days_n = int(str(days or "0").strip() or 0)
    except Exception:
        days_n = 0
    since = int(time.time()) - days_n * 86400 if days_n else 0
    items = []
    me = int(uid)
    def _snum(x):
        try:
            return int(x)
        except Exception:
            return -1
    for r in rows:
        if since and _snum(r["ts"]) < since:
            continue
        if who == "me" and _snum(r["sender"]) != me:
            continue
        if who and who != "me" and _snum(r["sender"]) != int(who):
            continue
        try:
            c = json.loads(r["content"] or "{}")
        except Exception:
            c = {}
        has_img = bool(c.get("images")) or any((a or {}).get("kind") == "image" for a in (c.get("attachments") or []))
        has_file = bool(c.get("attachments")) or bool(c.get("images"))
        if kind == "image" and not has_img:
            continue
        if kind == "file" and not has_file:
            continue
        if kind == "text" and not (c.get("text") or "").strip():
            continue
        items.append(r)
        if len(items) >= 1000:
            break
    total = len(items)
    rows = items[offset:offset + min(limit, 100)]
    items = []
    for r in rows:
        try:
            c = json.loads(r["content"] or "{}")
        except Exception:
            c = {}
        items.append({"seq": r["seq"], "sender": r["sender"], "role": r["role"],
                      "name": c.get("name") or (BOT_NAME if str(r["sender"]) == str(BOT_ID) else uid_name(str(r["sender"]))),
                      "text": (c.get("text") or "")[:200], "ts": r["ts"]})
    return {"items": items, "total": total}


def _copy_payload(src: dict) -> dict:
    """转发时只带走内容，不带原引用与 @。"""
    out = {}
    for k in ("text", "attachments", "voice", "dur"):
        if src.get(k):
            out[k] = src[k]
    return out


@app.post("/api/forward")
async def api_forward(request: Request):
    body = await request.json()
    src_conv = body.get("conv", "")
    to_conv = body.get("to", "")
    device = body.get("device", "")
    require_member(src_conv, device)
    require_member(to_conv, device)
    row = db().execute("SELECT * FROM message WHERE conv=? AND seq=?", (src_conv, int(body.get("seq") or 0))).fetchone()
    if row is None:
        raise HTTPException(404, "原消息不存在")
    src = json.loads(row["content"] or "{}")
    uid = uid_by_device(device)
    content = _copy_payload(src)
    content["name"] = uid_name(uid)
    content["forwarded_from"] = {"name": src.get("name") or uid_name(row["sender"]),
                                 "conv": src_conv}
    msg = add_message(to_conv, str(uid), "user", "text", content)
    await broadcast(to_conv, {"t": "msg_push", **msg})
    await broadcast_conv_update()
    return {"ok": True, "msg": msg}


@app.post("/api/merge_forward")
async def api_merge_forward(request: Request):
    body = await request.json()
    src_conv, to_conv, device = body.get("conv", ""), body.get("to", ""), body.get("device", "")
    require_member(src_conv, device)
    require_member(to_conv, device)
    seqs = [int(x) for x in (body.get("seqs") or [])][:50]
    if not seqs:
        raise HTTPException(400, "没有选中消息")
    conn = db()
    rows = conn.execute("SELECT * FROM message WHERE conv=? AND seq IN (%s) ORDER BY seq"
                        % ",".join("?" * len(seqs)), [src_conv] + seqs).fetchall()
    conn.close()
    items = []
    for r in rows:
        c = json.loads(r["content"] or "{}")
        items.append({"seq": r["seq"], "name": c.get("name") or (BOT_NAME if str(r["sender"]) == str(BOT_ID) else uid_name(str(r["sender"]))),
                      "text": c.get("text") or ("[附件] " + ("、".join(a.get("name", "") for a in (c.get("attachments") or [])))),
                      "ts": r["ts"]})
    uid = uid_by_device(device)
    content = {"name": uid_name(uid), "merged": items, "title": f"{src_conv} 的聊天记录"}
    msg = add_message(to_conv, str(uid), "user", "text", content)
    await broadcast(to_conv, {"t": "msg_push", **msg})
    return {"ok": True, "msg": msg}


@app.get("/api/history")
async def get_history(conv: str = "u1001", limit: int = 60, device: str = ""):
    require_member(conv, device)
    return {"items": history(conv, min(limit, 200))}


_TEXT_CACHE: dict = {}


def _apk_sha() -> dict:
    """返回安装包哈希与大小，按 mtime 缓存，避免每次请求都算一遍。"""
    p = STATIC / "AI-Chat.apk"
    try:
        mtime = p.stat().st_mtime
    except Exception:
        return {"apk_sha256": "", "apk_size": 0}
    cache = _TEXT_CACHE.get("__apk__")
    if cache and cache[0] == mtime:
        return cache[1]
    try:
        import hashlib
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        info = {"apk_sha256": h, "apk_size": p.stat().st_size}
    except Exception:
        info = {"apk_sha256": "", "apk_size": 0}
    _TEXT_CACHE["__apk__"] = (mtime, info)
    return info


def store_file(raw: bytes, name: str, mime: str, is_admin_user: bool, warn: str):
    raw_name = pathlib.Path(name).name
    ext = pathlib.Path(raw_name).suffix.lower()[:10]
    if STRICT_MAGIC and magic_mismatch(ext, raw[:16]):
        raise HTTPException(415, "文件内容与扩展名不符，请检查后重试")
    fid = "f-" + uuid.uuid4().hex[:12]
    target = UPLOADS / (fid + ext)
    target.write_bytes(raw)
    if ext in (".heic", ".heif"):
        conv = convert_heic(target)
        if conv is not None:
            try:
                target.unlink()
            except Exception:
                pass
            target = conv
            ext = ".jpg"
            mime = "image/jpeg"
            raw_name = pathlib.Path(raw_name).stem + ".jpg"
    kind = classify(ext)
    text, note = "", ""
    if ext in LEGACY_OFFICE:
        kind = "doc"
        note = "旧版/特殊 Office 格式，可下载后用 Office 或 WPS 打开；另存为 docx/xlsx/pptx 可在线预览"
    elif kind == "doc":
        text = extract_text(target)
        if not text.strip():
            note = "未提取到文本，可下载查看"
    elif kind == "archive":
        text = archive_listing(target)
        note = "压缩包仅列文件名，不解压；下载后本地解压"
        if not text:
            note = "该压缩格式不支持在线查看，请下载后解压"
    elif kind == "other":
        note = "仅支持预览与下载"
    conn = db()
    conn.execute("INSERT INTO file (id,name,mime,kind,size,path,text_len,note,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 (fid, raw_name, mime, kind, len(raw), str(target), len(text), note, int(time.time() * 1000)))
    conn.commit()
    conn.close()
    meta = file_meta(_file_row(fid))
    meta["text"] = text[:6000]
    meta["admin"] = is_admin_user
    if warn:
        meta["warn"] = warn
    return meta


@app.post("/api/files")
async def upload(request: Request):
    device = request.headers.get("x-device") or request.query_params.get("device", "")
    secret = request.headers.get("x-device-secret") or request.query_params.get("secret", "")
    if not device and secret:
        # 兼容旧客户端：只带密钥不带设备号时，按密钥反查设备
        conn0 = db()
        r0 = conn0.execute("SELECT device_id FROM device WHERE secret=?", (secret,)).fetchone()
        conn0.close()
        if r0:
            device = r0["device_id"]
    uid = require_device(device)
    stored = secret_of(device) or ""
    if not secret or not stored or not secrets.compare_digest(secret, stored):
        raise HTTPException(403, "设备密钥不正确")
    is_admin_user = is_admin(uid)
    ip = request.client.host if request.client else "?"
    now = int(time.time())
    # 定期清理过期的限流记录，避免字典只增不减
    if len(_up_rl) > 500:
        for k, v in list(_up_rl.items()):
            if now - v[0] > 120:
                _up_rl.pop(k, None)
    if not is_admin_user:
        win = _up_rl.get(ip) or (0, 0)
        if now - win[0] >= 60:
            win = (now, 0)
        if win[1] >= UPLOAD_PER_MIN:
            raise HTTPException(429, "上传太频繁，稍后再试")
        _up_rl[ip] = (win[0], win[1] + 1)
    try:
        used = sum(f.stat().st_size for f in UPLOADS.glob("*") if f.is_file())
    except Exception:
        used = 0
    name = unquote(request.headers.get("x-filename", "file.bin"))
    mime = request.headers.get("x-mime", "application/octet-stream")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "空文件")
    warn = ""
    if len(raw) > MAX_UPLOAD:
        if not is_admin_user:
            raise HTTPException(413, "超过大小上限 %d MB" % (MAX_UPLOAD // 1024 // 1024))
        if ADMIN_CONFIRM and request.headers.get("x-admin-confirm") != "1":
            raise HTTPException(409, "该文件 %.1f MB，超过普通上限 %d MB。管理员可继续上传，请确认后再发一次"
                                % (len(raw) / 1024 / 1024, MAX_UPLOAD // 1024 // 1024))
        warn = "管理员越权上传：%.1f MB 已超过普通上限 %d MB" % (len(raw) / 1024 / 1024, MAX_UPLOAD // 1024 // 1024)
    if not is_admin_user and used + len(raw) >= MAX_TOTAL:
        raise HTTPException(507, "服务器存储已满")
    if is_admin_user and used + len(raw) >= MAX_TOTAL:
        if ADMIN_CONFIRM and request.headers.get("x-admin-confirm") != "1":
            raise HTTPException(409, "存储已用 %.0f MB，本次上传会超出总配额，管理员可继续，请确认"
                                % (used / 1024 / 1024))
        warn = (warn + "；" if warn else "") + "管理员越权：总存储将超出 %d MB 配额" % (MAX_TOTAL // 1024 // 1024)

    return store_file(raw, name, mime, is_admin_user, warn)


_CHUNK_LOCK = threading.Lock()
_CHUNK_META = {}


@app.post("/api/files/chunk/init")
async def chunk_init(request: Request):
    body = await request.json()
    uid = require_device(request.headers.get("x-device") or request.query_params.get("device") or body.get("device", ""))
    name = str(body.get("name") or "file.bin")
    mime = str(body.get("mime") or "application/octet-stream")
    size = int(body.get("size") or 0)
    if size <= 0:
        raise HTTPException(400, "文件大小不正确")
    if not is_admin(uid) and size > MAX_UPLOAD:
        raise HTTPException(413, "超过大小上限 %d MB" % (MAX_UPLOAD // 1024 // 1024))
    # 清掉超过 2 小时的残留分片，避免断网任务堆积占盘
    try:
        now_ts = time.time()
        for f in CHUNKS.glob("*.part"):
            if now_ts - f.stat().st_mtime > 7200:
                f.unlink(missing_ok=True)
        for k, m in list(_CHUNK_META.items()):
            if now_ts - pathlib.Path(m["path"]).stat().st_mtime > 7200:
                _CHUNK_META.pop(k, None)
    except Exception:
        pass
    up_id = "u-" + uuid.uuid4().hex[:12]
    part = CHUNKS / (up_id + ".part")
    with _CHUNK_LOCK:
        _CHUNK_META[up_id] = {"name": name, "mime": mime, "size": size, "received": 0,
                              "bytes": 0, "path": str(part), "uid": int(uid)}
    return {"upload_id": up_id, "received": 0}


@app.put("/api/files/chunk/{up_id}/{idx}")
async def chunk_put(up_id: str, idx: int, request: Request):
    require_device(request.headers.get("x-device") or request.query_params.get("device", ""))
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "空分片")
    with _CHUNK_LOCK:
        meta = _CHUNK_META.get(up_id)
        if meta is None:
            raise HTTPException(404, "上传任务不存在或已过期")
        if idx != meta["received"]:
            return {"received": meta["received"], "retry": True}
        with open(meta["path"], "ab") as f:
            f.write(raw)
        meta["received"] += 1
        meta["bytes"] += len(raw)
        return {"received": meta["received"], "bytes": meta["bytes"]}


@app.post("/api/files/chunk/{up_id}/finish")
async def chunk_finish(up_id: str, request: Request):
    device = request.headers.get("x-device") or request.query_params.get("device", "")
    uid = require_device(device)
    with _CHUNK_LOCK:
        meta = _CHUNK_META.pop(up_id, None)
    if meta is None:
        raise HTTPException(404, "上传任务不存在或已过期")
    part = pathlib.Path(meta["path"])
    if not part.exists():
        raise HTTPException(404, "分片文件丢失，请重新上传")
    raw = part.read_bytes()
    try:
        part.unlink()
    except Exception:
        pass
    if len(raw) != meta["size"]:
        raise HTTPException(400, "分片未传完：%d/%d 字节" % (len(raw), meta["size"]))
    if not is_admin(uid) and meta["size"] > MAX_UPLOAD:
        raise HTTPException(413, "超过大小上限")
    try:
        used = sum(f.stat().st_size for f in UPLOADS.glob("*") if f.is_file())
    except Exception:
        used = 0
    warn = ""
    if used + len(raw) >= MAX_TOTAL:
        raise HTTPException(507, "服务器存储已满")
    return store_file(raw, meta["name"], meta["mime"], is_admin(uid), warn)


@app.get("/api/files/{fid}")
async def download(fid: str, inline: int = 0):
    row = _file_row(fid)
    if row is None:
        raise HTTPException(404, "不存在")
    mime = row["mime"] or "application/octet-stream"
    ext = pathlib.Path(row["name"]).suffix.lower()
    allow_inline = bool(inline) and mime == "application/pdf" and ext == ".pdf"
    if not allow_inline:
        mime = "application/octet-stream"
    return FileResponse(row["path"], media_type=mime, filename=row["name"],
                        content_disposition_type="inline" if allow_inline else "attachment",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/files/{fid}/text")
async def file_text(fid: str):
    row = _file_row(fid)
    if row is None:
        raise HTTPException(404, "不存在")
    cached = _TEXT_CACHE.get(fid)
    try:
        mtime = pathlib.Path(row["path"]).stat().st_mtime
    except Exception:
        mtime = 0
    if cached and cached[0] == mtime:
        body = cached[1]
    else:
        body = ""
        if row["kind"] == "doc":
            body = extract_text(pathlib.Path(row["path"]))
        elif row["kind"] == "archive":
            body = archive_listing(pathlib.Path(row["path"]))
        if len(_TEXT_CACHE) > 200:
            _TEXT_CACHE.clear()
        _TEXT_CACHE[fid] = (mtime, body)
    return {"name": row["name"], "kind": row["kind"], "text": body[:8000],
            "truncated": len(body) > 8000}


POSTERS = DATA / "posters"


@app.get("/poster/{fid}")
async def poster(fid: str):
    """视频封面：取第一帧，客户端未播放时也能看到画面。"""
    row = _file_row(fid)
    if row is None or row["kind"] != "video":
        raise HTTPException(404, "不存在")
    POSTERS.mkdir(parents=True, exist_ok=True)
    cache = POSTERS / (fid + ".jpg")
    if not cache.exists() or cache.stat().st_size == 0:
        import subprocess
        src = row["path"]
        for ts in ("0.1", "1", "0"):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", ts, "-i", src,
                            "-frames:v", "1", "-vf", "scale=720:-2", str(cache)],
                           timeout=25, check=False)
            if cache.exists() and cache.stat().st_size > 0:
                break
    if not cache.exists() or cache.stat().st_size == 0:
        raise HTTPException(404, "无法生成封面")
    return FileResponse(cache, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/thumb/{fid}")
async def thumb(fid: str, w: int = 128):
    """头像/列表用的小图，解决首屏头像慢和流量大的问题。"""
    row = _file_row(fid)
    if row is None:
        raise HTTPException(404, "不存在")
    if not str(row["mime"]).startswith("image/"):
        return FileResponse(row["path"], media_type="application/octet-stream",
                            filename=row["name"] or fid, content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})
    w = max(32, min(int(w), 512))
    src = pathlib.Path(row["path"])
    THUMBS.mkdir(parents=True, exist_ok=True)
    # 先判断源图是否带透明通道：带就出 PNG 缩略图，别把透明背景压成黑/白底
    alpha = False
    try:
        from PIL import Image
        probe = Image.open(src)
        alpha = probe.mode in ("RGBA", "LA") or (probe.mode == "P" and "transparency" in probe.info)
        probe.close()
    except Exception:
        alpha = str(row["mime"]) == "image/png"
    ext = "png" if alpha else "jpg"
    cache = THUMBS / f"{fid}-{w}.{ext}"
    if not cache.exists():
        try:
            from PIL import Image
            im = Image.open(src)
            im = im.convert("RGBA" if alpha else "RGB")
            im.thumbnail((w, w))
            if alpha:
                im.save(cache, "PNG", optimize=True)
            else:
                im.save(cache, "JPEG", quality=84, optimize=True)
        except Exception:
            return FileResponse(src)
    return FileResponse(cache, media_type=("image/png" if alpha else "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=604800"})


def _still_frame(path: pathlib.Path, fid: str) -> pathlib.Path:
    """动图（gif/webp 多帧）取第一帧存成 jpg，作为静态背景使用。"""
    cache = path.parent / (fid + ".still.jpg")
    try:
        if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
            return cache
        from PIL import Image
        im = Image.open(path)
        if getattr(im, "n_frames", 1) <= 1:
            return path
        im.seek(0)
        im.convert("RGB").save(cache, "JPEG", quality=88, optimize=True)
        return cache
    except Exception:
        return path


@app.get("/media/{fid}")
async def media(fid: str, request: Request, still: int = 0):
    row = _file_row(fid)
    if row is None:
        raise HTTPException(404, "不存在")
    if still:
        sp = _still_frame(pathlib.Path(row["path"]), fid)
        if sp.suffix.lower() in (".jpg", ".jpeg"):
            return FileResponse(sp, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=604800"})
    if row["kind"] not in ("image", "video", "audio"):
        raise HTTPException(404, "该文件请用下载方式获取")
    path = pathlib.Path(row["path"])
    size = path.stat().st_size
    mime = row["mime"] or "application/octet-stream"
    if not mime.startswith(("image/", "video/", "audio/")):
        mime = "application/octet-stream"
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type=mime,
                            headers={"Cache-Control": "public, max-age=604800"})
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    start = int(m.group(1) or 0) if m else 0
    end = int(m.group(2)) if (m and m.group(2)) else size - 1
    end = min(end, size - 1)
    length = max(end - start + 1, 0)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(length)
    return Response(content=data, status_code=206, media_type=mime,
                    headers={"Content-Range": f"bytes {start}-{end}/{size}",
                             "Accept-Ranges": "bytes", "Content-Length": str(length)})


@app.post("/api/files/{fid}/removebg")
async def do_removebg(fid: str, request: Request):
    require_req_device(request)
    ip = request.client.host if request.client else "?"
    key = "nobg:" + ip
    now = int(time.time())
    win = _up_rl.get(key) or (0, 0)
    if now - win[0] >= 3600:
        win = (now, 0)
    if win[1] >= 30:
        raise HTTPException(429, "去背请求过于频繁")
    _up_rl[key] = (win[0], win[1] + 1)
    row = _file_row(fid)
    if row is None or row["kind"] != "image":
        raise HTTPException(400, "仅图片支持去背")
    src = pathlib.Path(row["path"])
    dst = src.with_name(src.stem + "_nobg.png")
    if not dst.exists() and not remove_background(src, dst):
        raise HTTPException(422, "背景不纯，自动去背失败")
    new_id = "f-" + uuid.uuid4().hex[:12]
    conn = db()
    conn.execute("INSERT INTO file (id,name,mime,kind,size,path,text_len,note,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 (new_id, pathlib.Path(row["name"]).stem + "-无背景.png", "image/png", "image",
                  dst.stat().st_size, str(dst), 0, "已去背", int(time.time() * 1000)))
    conn.commit()
    conn.close()
    return file_meta(_file_row(new_id))


_TOUCH_CACHE = {}


def touch_device(dev: str, force: bool = False) -> None:
    if not dev:
        return
    now = int(time.time())
    if not force and now - _TOUCH_CACHE.get(dev, 0) < 60:
        return
    _TOUCH_CACHE[dev] = now
    if len(_TOUCH_CACHE) > 2000:
        for k, v in list(_TOUCH_CACHE.items()):
            if now - v > 3600:
                _TOUCH_CACHE.pop(k, None)
    try:
        conn = db()
        conn.execute("UPDATE device SET last_seen=? WHERE device_id=?", (now, dev))
        conn.commit(); conn.close()
    except Exception:
        pass


@app.get("/api/devices")
async def my_devices(device: str = ""):
    uid = require_device(device)
    conn = db()
    rows = conn.execute("SELECT device_id,name,created_at,last_seen FROM device WHERE uid=? ORDER BY created_at", (uid,)).fetchall()
    conn.close()
    online = set()
    for socks in clients.values():
        for sock in socks:
            if getattr(sock, "device", ""):
                online.add(sock.device)
    items = [{"device_id": r["device_id"], "name": r["name"], "created_at": r["created_at"],
              "last_seen": r["last_seen"] or 0, "online": r["device_id"] in online,
              "current": r["device_id"] == device} for r in rows]
    return {"items": items}


@app.post("/api/devices/{dev}/kick")
async def kick_device(dev: str, request: Request, device: str = ""):
    uid, _ = require_req_device(request, device)
    conn = db()
    row = conn.execute("SELECT uid FROM device WHERE device_id=?", (dev,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "设备不存在")
    if int(row["uid"]) != int(uid):
        conn.close()
        raise HTTPException(403, "只能下线自己的设备")
    if dev == device:
        conn.close()
        raise HTTPException(400, "不能下线当前设备")
    sec = "sk_" + secrets.token_urlsafe(24)
    conn.execute("UPDATE device SET secret=? WHERE device_id=?", (sec, dev))
    conn.commit(); conn.close()
    # 密钥已轮换，同时断开该设备已建立的连接，否则下线要等它自己重连才生效
    for key, socks in list(clients.items()):
        for sock in list(socks):
            if getattr(sock, "device", "") == dev:
                socks.discard(sock)
                try:
                    asyncio.create_task(sock.close(code=4403))
                except Exception:
                    pass
        if not socks:
            clients.pop(key, None)
    return {"ok": True}


@app.get("/api/storage")
async def storage_info(device: str = ""):
    require_device(device)
    try:
        used = sum(f.stat().st_size for f in UPLOADS.glob("*") if f.is_file())
        thumbs = sum(f.stat().st_size for f in THUMBS.glob("*") if f.is_file())
    except Exception:
        used = thumbs = 0
    conn = db()
    files = conn.execute("SELECT COUNT(*) c FROM file").fetchone()["c"]
    msgs = conn.execute("SELECT COUNT(*) c FROM message").fetchone()["c"]
    conn.close()
    return {"uploads_bytes": used, "thumbs_bytes": thumbs, "files": files, "messages": msgs,
            "quota_bytes": MAX_TOTAL}


_EXPORT_TOKENS = {}


@app.post("/api/export/token")
async def export_token(request: Request):
    body = await request.json()
    conv = str(body.get("conv") or "")
    device = str(body.get("device") or request.query_params.get("device", ""))
    require_member(conv, device)
    tok = secrets.token_urlsafe(18)
    _EXPORT_TOKENS[tok] = (conv, int(time.time()) + 300)
    if len(_EXPORT_TOKENS) > 500:
        for k, v in list(_EXPORT_TOKENS.items()):
            if v[1] < time.time():
                _EXPORT_TOKENS.pop(k, None)
    return {"token": tok, "expires_in": 300}


@app.get("/export/{token}")
async def export_by_token(token: str):
    item = _EXPORT_TOKENS.get(token)
    if not item or item[1] < time.time():
        raise HTTPException(404, "导出链接已过期")
    conv, _ = item
    conn = db()
    rows = conn.execute("SELECT * FROM message WHERE conv=? ORDER BY seq LIMIT 20000", (conv,)).fetchall()
    conn.close()
    items = []
    for r in rows:
        try:
            c = json.loads(r["content"] or "{}")
        except Exception:
            c = {}
        items.append({"seq": r["seq"], "ts": r["ts"], "sender": r["sender"],
                      "role": r["role"], "content": c})
    body = json.dumps({"conv": conv, "exported_at": int(time.time()), "count": len(items),
                       "items": items}, ensure_ascii=False, indent=1)
    from fastapi.responses import Response
    return Response(content=body.encode("utf-8"), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=qunqi-%s.json" % conv})


@app.get("/api/export")
async def export_conv(conv: str = "", device: str = "", limit: int = 5000):
    require_member(conv, device)
    conn = db()
    rows = conn.execute("SELECT * FROM message WHERE conv=? ORDER BY seq LIMIT ?",
                        (conv, max(1, min(limit, 20000)))).fetchall()
    conn.close()
    items = []
    for r in rows:
        try:
            c = json.loads(r["content"] or "{}")
        except Exception:
            c = {}
        items.append({"seq": r["seq"], "ts": r["ts"], "sender": r["sender"],
                      "role": r["role"], "content": c})
    body = json.dumps({"conv": conv, "exported_at": int(time.time()), "count": len(items),
                       "items": items}, ensure_ascii=False, indent=1)
    from fastapi.responses import Response
    return Response(content=body.encode("utf-8"), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=qunqi-%s.json" % conv})


def _age_seconds(ts) -> int:
    """消息 ts 有的按毫秒存、有的按秒存，统一换算成“距今多少秒”。"""
    try:
        v = float(ts or 0)
    except Exception:
        return 0
    now = time.time()
    if v > 1e12:
        return int(now - v / 1000)
    return int(now - v)


@app.post("/api/messages/{cid}/{seq}/recall")
async def recall_message(cid: str, seq: int, request: Request, device: str = ""):
    uid = require_member(cid, device)
    conn = db()
    row = conn.execute("SELECT * FROM message WHERE conv=? AND seq=?", (cid, seq)).fetchone()
    if row is None:
        conn.close(); raise HTTPException(404, "消息不存在")
    if int(row["sender"] or 0) != int(uid) and not is_admin(uid):
        conn.close(); raise HTTPException(403, "只能撤回自己的消息")
    if not is_admin(uid) and _age_seconds(row["ts"]) > 120:
        conn.close(); raise HTTPException(400, "超过 2 分钟不能撤回")
    try:
        c = json.loads(row["content"] or "{}")
    except Exception:
        c = {}
    c = {"text": "该消息已被撤回", "recalled": 1}
    conn.execute("UPDATE message SET content=? WHERE conv=? AND seq=?",
                 (json.dumps(c, ensure_ascii=False), cid, seq))
    conn.commit(); conn.close()
    await broadcast(cid, {"t": "msg_recall", "conv": cid, "seq": seq})
    return {"ok": True}


@app.patch("/api/messages/{cid}/{seq}")
async def edit_message(cid: str, seq: int, request: Request, device: str = ""):
    uid = require_member(cid, device)
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    conn = db()
    row = conn.execute("SELECT * FROM message WHERE conv=? AND seq=?", (cid, seq)).fetchone()
    if row is None:
        conn.close(); raise HTTPException(404, "消息不存在")
    if int(row["sender"] or 0) != int(uid):
        conn.close(); raise HTTPException(403, "只能编辑自己的消息")
    if _age_seconds(row["ts"]) > 300:
        conn.close(); raise HTTPException(400, "超过 5 分钟不能编辑")
    try:
        c = json.loads(row["content"] or "{}")
    except Exception:
        c = {}
    c["text"] = text[:2000]
    c["edited"] = 1
    conn.execute("UPDATE message SET content=? WHERE conv=? AND seq=?",
                 (json.dumps(c, ensure_ascii=False), cid, seq))
    conn.commit(); conn.close()
    await broadcast(cid, {"t": "msg_edit", "conv": cid, "seq": seq, "text": text[:2000]})
    return {"ok": True}


@app.get("/api/convs/{cid}/notice")
async def get_notice(cid: str, device: str = ""):
    try:
        require_member(cid, device)
    except HTTPException:
        if not is_admin(uid_by_device(device) or 0):
            raise
    notes = get_setting("conv_notice", {}) or {}
    return {"notice": notes.get(cid, "")}


@app.post("/api/convs/{cid}/notice")
async def set_notice(cid: str, request: Request, device: str = ""):
    require_admin(cid, device)
    body = await request.json()
    text = str(body.get("notice") or "").strip()[:200]
    notes = get_setting("conv_notice", {}) or {}
    if text:
        notes[cid] = text
    else:
        notes.pop(cid, None)
    set_setting("conv_notice", notes)
    await broadcast_all({"t": "notice", "conv": cid, "notice": text})
    return {"ok": True, "notice": text}


DOCS_DIR = BASE / "docs"
_DOC_WHITELIST = {"OPS.md", "GUIDE.md", "AGENT_API.md", "FILE_FORMATS.md"}


@app.get("/api/docs/{name}")
async def get_doc(name: str):
    if name not in _DOC_WHITELIST:
        raise HTTPException(404, "文档不存在")
    path = DOCS_DIR / name
    if not path.exists():
        raise HTTPException(404, "文档不存在")
    return {"name": name, "updated_at": int(path.stat().st_mtime), "text": path.read_text(encoding="utf-8")}


@app.get("/api/plugins")
async def plugins(request: Request):
    require_req_device(request)
    items = []
    for f in sorted(PLUGIN_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        data["enabled"] = get_setting("plugin_" + data.get("plugin_id", f.stem), True)
        items.append(data)
    return {"items": items}


@app.post("/api/plugins/{pid}/toggle")
async def toggle_plugin(pid: str, request: Request):
    body = await request.json()
    require_admin(body.get("conv") or "", body.get("device", ""))
    enable = bool(body.get("enable", True))
    set_setting("plugin_" + pid, enable)
    return {"ok": True, "plugin_id": pid, "enabled": enable}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    remember_host(ws)
    await ws.accept()
    conv = "u1001"
    uid, nick = 1001, "我"
    try:
        first = json.loads(await ws.receive_text())
        if GATEWAY_TOKEN and not check_token(ws):
            await ws.close(code=4401)
            return
        conv = first.get("conv") or conv
        prof = device_profile(first.get("device", ""), first.get("name", ""),
                              first.get("kind", "user"))
        uid, nick = prof["uid"], prof["name"]
        _dev = first.get("device", "") or ""
        _sec = first.get("secret", "") or ""
        _exp = secret_of(_dev)
        if not _exp and _dev in ("bot", "BOT", "ai", "AI", str(BOT_ID)):
            _exp = BOT_TOKEN
        if not _exp:
            await ws.send_text(json.dumps({"t": "error", "msg": "设备未注册，请重新登录"}, ensure_ascii=False))
            await ws.close(code=4403)
            return
        if _sec != _exp:
            await ws.send_text(json.dumps({"t": "error", "msg": "设备密钥不正确，请重新登录"}, ensure_ascii=False))
            await ws.close(code=4403)
            return
        is_agent = prof.get("kind") == "agent"
        # 私聊会话必须落在自己的 u<uid>，否则 AI 回复会投到别人的会话
        if conv.startswith("u") and conv[1:].isdigit() and int(conv[1:]) != uid:
            conv = f"u{uid}"
        if conv.startswith("g") and uid not in members_of(conv) and conv != "g1001":
            conv = f"u{uid}"
        ws.uid = uid
        ws.device = _dev
        touch_device(_dev, force=True)
        clients.setdefault(conv, set()).add(ws)
        await ws.send_text(json.dumps({"t": "auth_ok", "bot": {"online": bot.online}, "conv": conv,
                                       "profile": {"uid": uid, "name": nick, "kind": prof.get("kind"),
                                                   "code": prof.get("code"), "admin": is_admin(uid)},
                                       "settings": ui_state(),
                                       "themes": THEMES + get_setting("custom_themes", []),
                                       "convs": conv_list(uid), "net": net_info(),
                                       "sys": {"show_ai_all": get_setting("sys", {}).get("show_ai_all", True), "ai_hidden": (not is_admin(uid) and not get_setting("sys", {}).get("show_ai_all", True))}}, ensure_ascii=False))

        while True:
            frame = json.loads(await ws.receive_text())
            if frame.get("t") == "ping":
                await ws.send_text(json.dumps({"t": "pong"}))
                continue
            if frame.get("t") == "call":
                # 1:1 语音通话信令，只做转发不落库
                to = int(frame.get("to") or 0)
                for sock in list(clients.get(f"u{to}", set())):
                    try:
                        await sock.send_text(json.dumps({"t": "call", "from": uid, "name": nick,
                                                         "data": frame.get("data") or {}},
                                                        ensure_ascii=False))
                    except Exception:
                        pass
                continue
            if frame.get("t") == "typing":
                for sock in list(clients.get(conv, set())):
                    if sock is ws:
                        continue
                    try:
                        await sock.send_text(json.dumps({"t": "typing", "conv": conv,
                                                         "uid": uid, "name": nick}))
                    except Exception:
                        pass
                continue
            if frame.get("t") == "switch":
                target = frame.get("conv") or conv
                if not ensure_access(target, uid):
                    await ws.send_text(json.dumps({"t": "error", "msg": "你不在这个群里"}))
                    continue
                clients.get(conv, set()).discard(ws)
                conv = target
                clients.setdefault(conv, set()).add(ws)
                await ws.send_text(json.dumps({"t": "switch_ok", "conv": conv}, ensure_ascii=False))
                continue
            if frame.get("t") == "profile":
                prof = device_profile(first.get("device", ""), frame.get("name", ""))
                nick = prof["name"]
                await ws.send_text(json.dumps({"t": "profile_ok", "name": nick}))
                continue
            if frame.get("t") != "msg_send":
                continue

            if frame.get("conv") and frame["conv"] != conv:
                target = frame["conv"]
                if not ensure_access(target, uid):
                    await ws.send_text(json.dumps({"t": "error", "msg": "你不在这个群里"}))
                    continue
                clients.get(conv, set()).discard(ws)
                conv = target
                clients.setdefault(conv, set()).add(ws)

            payload = frame.get("content") or {}
            ftype = frame.get("type", "text")
            cid = frame.get("client_msg_id", "")
            is_group = conv.startswith("g")
            row = conv_row(conv)
            if is_group:
                if not ensure_access(conv, uid):
                    await ws.send_text(json.dumps({"t": "error", "msg": "你不在这个群里"}))
                    continue
                if (row["mute_until"] or 0) > int(time.time()) and not is_admin(uid):
                    await ws.send_text(json.dumps({"t": "error", "msg": "群已禁言"}))
                    continue

            text = payload.get("text", "")
            attachments, images, segments = [], [], []
            has_image = False

            for fid in payload.get("file_ids", [])[:6]:
                frow = _file_row(fid)
                if frow is None:
                    continue
                attachments.append(file_meta(frow))
                if frow["kind"] == "image":
                    has_image = True
                    images.append(f"/media/{fid}")
                    segments.append({"type": "image",
                                     "data": {"file": PUBLIC_BASE.rstrip("/") + f"/media/{fid}"
                                              if PUBLIC_BASE else f"/media/{fid}"}})

            # 1) 界面/群管理动作：本地先执行（群动作只在群里跑）
            # 管理权只归服务器 AI（BOT_ID）；外部 agent 与普通用户只能聊天
            local_actions = parse_intent(text, has_image)
            if uid != BOT_ID and not is_admin(uid):
                local_actions = [a for a in local_actions if not a["type"].startswith("chat.")]
            if is_group:
                for a in local_actions:
                    if a["type"].startswith("chat."):
                        a["payload"].setdefault("conv_id", conv)
            else:
                local_actions = [a for a in local_actions
                                 if not a["type"].startswith("chat.") or a["type"] == "chat.create"]
            if has_image:
                img_ids = [a["file_id"] for a in attachments if a["kind"] == "image"]
                for a in local_actions:
                    if a["type"] == "theme.from_image" and img_ids:
                        a["payload"] = {"file_id": img_ids[0]}

            notes = []
            if local_actions:
                st, notes = apply_actions(local_actions, conv, uid)
                await broadcast_all({"t": "ui_update", "settings": st, "actions": local_actions,
                                     "themes": THEMES + get_setting("custom_themes", [])})
                if any(a["type"].startswith("chat.") for a in local_actions):
                    await broadcast_conv_update()

            prompt = [text] if text else []
            for a in attachments:
                frow = _file_row(a["file_id"])
                if a["kind"] == "doc":
                    body = extract_text(pathlib.Path(frow["path"])) if frow else ""
                    if len(body) <= 6000:
                        prompt.append(f"【文件】{a['name']}，共{len(body)}字符：\n{body}")
                    elif body:
                        prompt.append(f"【文件】{a['name']}，共{len(body)}字符，前4000字符：\n{body[:4000]}")
                    else:
                        prompt.append(f"【文件】{a['name']}（未提取到文本，用户可下载查看）")
                elif a["kind"] == "image":
                    prompt.append(f"【图片】{a['name']}")
                elif a["kind"] == "video":
                    prompt.append(f"【视频】{a['name']}（不解析内容）")
                elif a["kind"] in ("audio", "other"):
                    prompt.append(f"【附件】{a['name']}（仅预览/下载）")

            user_text = "\n\n".join(prompt).strip()
            if not user_text:
                await ws.send_text(json.dumps({"t": "error", "msg": "空消息"}, ensure_ascii=False))
                continue

            extra = {}
            rp = payload.get("reply_to")
            if rp:
                try:
                    extra["quote"] = msg_quote(conv, int(rp))
                except Exception:
                    pass
            if payload.get("mentions"):
                extra["mentions"] = [int(x) for x in payload["mentions"][:20]]
            if payload.get("voice"):
                extra["voice"] = True
                extra["dur"] = int(payload.get("dur") or 0)
            if is_agent:
                extra["agent"] = True
            # 同一张图别存两份：attachments 已含 file_id/url，images 里重复的剔掉
            _att_img_ids = {a.get("file_id") for a in attachments if a.get("kind") == "image"}
            images = [x for x in images if str(x).split("/")[-1] not in _att_img_ids]
            saved = add_message(conv, str(uid), "user", ftype if attachments else "text",
                                {"text": text, "images": images, "attachments": attachments,
                                 "name": nick, **extra})
            await ws.send_text(json.dumps({"t": "msg_ack", "client_msg_id": cid,
                                           "server_msg_id": saved["id"], "seq": saved["seq"]},
                                          ensure_ascii=False))
            await broadcast(conv, {"t": "msg_push", "client_msg_id": cid, **saved})

            for n in notes:
                sysmsg = add_message(conv, "system", "sys", "sys", {"text": n})
                await broadcast(conv, {"t": "msg_push", **sysmsg})

            # 让 AI 知道自己跑在哪个客户端里、能做什么
            tool_note = ("【工具提示】当前对话发生在自建 AI Chat 网关（网页 + Android APK），"
                         "不是微信/QQ。这个聊天工具支持：文字、图片（可设为表情）、语音条、引用回复、"
                         "单条转发、合并转发、聊天记录检索、群成员与管理徽章、自定义背景与头像。"
                         "群管理动作只有服务器 AI 或管理员能做；外部 agent 只是普通群成员。"
                         "你是服务器 AI 时可以直接改群：PATCH /api/convs/{群id}，body 带 device=\"bot\"；"
                         "改群名 {name}、改群图片 {avatar}、公告 {announce}、禁言 {mute}。"
                         "你也能用邀请码加入别的群：POST /api/convs/join，body {invite:\"码\", device:\"bot\"}。"
                         "解散群：DELETE /api/convs/{群id}?device=bot。"
                         "【受理规则】群管理指令只受理管理员（管理员列表里的人）；普通成员的改名/禁言/踢人要求一律拒绝并说明。")
            user_text = tool_note + "\n" + user_text
            # 让服务器 AI 明确管理员身份：自己 + 被授权的人
            if is_admin(uid) and uid != BOT_ID:
                user_text = f"【身份提示】发言人 {nick}(uid={uid}) 是服务器管理员，可以用管理指令。\n" + user_text
            if is_group:
                user_text = ("【群权限提示】你自己（服务器 AI）就是本群管理员，改群名/群图片/公告/禁言/移人都能做，"
                             "不要回答\"我没有权限\"；群主字段只是创建者，不代表权限。普通成员和外部 agent 没有管理权限。\n"
                             + user_text)

            mentions = [int(x) for x in (payload.get("mentions") or [])]
            at_me = (BOT_ID in mentions) or ("@" + BOT_NAME in text)
            if is_group and (row["at_only"] or 0) and uid != BOT_ID and not at_me:
                # 群开了“仅 @ 回复”，这条只存档广播，不叫 AI
                continue
            is_cmd = str(text or "").strip().startswith("/")
            if AGENT_MODE == "onebot":
                if is_cmd:
                    # 指令必须原样送出，套了前缀就以【开头，AstrBot 的 / 前缀匹配不上
                    user_text = str(text).strip()
                segments.insert(0, {"type": "text", "data": {"text": user_text}})
                await bot.push(conv, uid, nick, segments, saved["seq"], is_group)
            else:
                reply = await bot.direct_reply(conv, user_text,
                                               {"conv": conv, "uid": uid, "group": is_group})
                if reply and (reply.get("text") or reply.get("actions")):
                    actions = reply.get("actions") or extract_actions(reply.get("text", ""))
                    clean = strip_actions(reply.get("text", ""))
                    notes2 = []
                    if actions:
                        st, notes2 = apply_actions(actions, conv, BOT_ID)
                        await broadcast_all({"t": "ui_update", "settings": st, "actions": actions,
                                             "themes": THEMES + get_setting("custom_themes", [])})
                    sysb = add_message(conv, "bot", "bot", "bot",
                                       {"text": clean, "images": [], "attachments": [], "actions": actions})
                    await broadcast(conv, {"t": "msg_push", **sysb})
                    for n in notes2:
                        await broadcast(conv, {"t": "msg_push", **add_message(conv, "system", "sys", "sys", {"text": n})})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log("客户端异常：" + type(exc).__name__)
    finally:
        try:
            c = locals().get("conv")
            if c and clients.get(c):
                clients[c].discard(ws)
        except Exception:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8099")), access_log=False)
