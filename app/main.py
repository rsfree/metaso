#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 入口：/v1/chat/completions（OpenAI 对齐）+ /v1/models + /health + /llms.txt + /。

通路定义（2026-09-24 定稿，**自动判断，只有两条**）——看 `Authorization: Bearer` 的值：

  值里含 `uid=` 且含 `sid=`  → **登录通路**：把这串当登录 cookie 用
                              （调用方自带登录态，走登录积分 500/天，首答 ~1s）
  其余一切请求               → **未登录通路**（`Bearer guest`、无 token、任何其他值；
                              自动随机指纹身份，~13 发/出口/窗口）

两条通路都没有密钥强度 —— 公网部署即公开（登录通路=调用方自带额度，guest=公开匿名
窗口），介意就用入口层（nginx）限流/白名单，或只在回环/内网开放。
"""
from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from . import __version__
from .client import METASO_MODELS, MetasoChatClient
from .config import Settings, get_settings
from .errors import ServiceError
from .gate import Gate
from .llms_txt import render_landing, render_llms_txt
from .service import ChatService

settings = get_settings()


def _build_channel(s: Settings) -> ChatService:
    """一个通路 = 一个客户端（身份/出口/传输状态）+ 一个闸门 + 独立会话表。"""
    client = MetasoChatClient(s)
    gate = Gate(s)
    gate.tune(s.min_interval_for(client.mode == "login"))
    return ChatService(s, client, gate)


def _is_login_cookie(token: str) -> bool:
    """Bearer 值是否为登录 cookie：含 `uid=` 与 `sid=`（消融实证的登录充分必要对）。"""
    return "uid=" in token and "sid=" in token


#: 未登录通路：常驻单例（自动随机指纹身份）。
guest_channel = _build_channel(dataclasses.replace(settings, ms_cookie=""))


class LoginChannelCache:
    """按 cookie 串缓存登录通路 —— 同一 cookie 的重复调用复用已热的
    token / JSESSIONID / 会话表，不必每次重建。"""

    def __init__(self, max_size: int = 64, ttl: float = 1800.0) -> None:
        self.max_size = max_size
        self.ttl = ttl
        self._data: dict[str, tuple[float, ChatService]] = {}

    def get_or_build(self, cookie: str) -> ChatService:
        key = hashlib.sha256(cookie.encode()).hexdigest()[:32]
        now = time.time()
        hit = self._data.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        if len(self._data) >= self.max_size:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        svc = _build_channel(dataclasses.replace(settings, ms_cookie=cookie))
        self._data[key] = (now, svc)
        return svc

    def __len__(self) -> int:
        return len(self._data)


login_channels = LoginChannelCache()


def resolve_channel(headers: dict[str, str]) -> ChatService:
    """`Bearer <含 uid= 与 sid= 的 cookie>` → 登录通路；其余一切 → 未登录通路。"""
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if _is_login_cookie(token):
        return login_channels.get_or_build(token)
    return guest_channel


app = FastAPI(title="metaso-service", version=__version__,
              description="秘塔 AI 搜索（metaso.cn）免登录搜索对话服务 —— 契约见 docs/UPSTREAM.md")


@app.exception_handler(ServiceError)
async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "metaso-service",
        "version": __version__,
        "ready": guest_channel.settings.metaso_ready,
        "channels": {
            "guest": {
                "mode": guest_channel.client.mode,
                "ready": guest_channel.settings.metaso_ready,
                "logged_in": guest_channel.client.logged_in,
                "egress": guest_channel.client.egress_label(),
                "pool_size": len(guest_channel.client.pool),
                "transport": guest_channel.client.transport,
                "gate": guest_channel.gate.stats(),
            },
        },
        "login_channels_cached": len(login_channels),
        "routing": {
            "login": "Authorization: Bearer <登录cookie（含 uid= 与 sid=）>",
            "guest": "其余一切请求（含 Bearer guest / 无 token）",
        },
    }


@app.get("/v1/models")
def models() -> dict:
    """模型清单。不制造假能力：上游未启用时返回空清单。"""
    ready = settings.metaso_ready
    return {
        "object": "list",
        "surface": "chat",
        "available": ready,
        "data": [
            {
                "id": m.model,
                "object": "model",
                "kind": "chat",
                "surface": "chat",
                "experimental": m.experimental,
                "notes": m.notes,
                "capabilities": {"stream": True, "citations": True,
                                 "vision": False, "thinking": False},
            } for m in METASO_MODELS
        ] if ready else [],
        "warnings": [] if ready else ["metaso 未启用：设 METASO_ENABLED=1 或配 METASO_COOKIE"],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    headers = {k.lower(): v for k, v in request.headers.items()}
    svc = resolve_channel(headers)
    payload = await request.json()
    if payload.get("stream"):
        return StreamingResponse(
            svc.stream_sse(payload, headers),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
    return JSONResponse(status_code=200, content=svc.complete(payload, headers))


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """人类入口（HTML 落地页）—— 免鉴权，服务自身渲染（勿在入口层放静态副本）。"""
    return HTMLResponse(render_landing(settings))


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt() -> PlainTextResponse:
    """LLM/Agent 说明书（内容从注册表与异常类派生）—— 免鉴权。"""
    return PlainTextResponse(render_llms_txt(settings),
                             media_type="text/markdown; charset=utf-8")
