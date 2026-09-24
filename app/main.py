#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 入口：/v1/chat/completions（OpenAI 对齐）+ /v1/models + /health。"""
from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .client import METASO_MODELS, MetasoChatClient
from .config import get_settings
from .errors import ServiceError, UnauthorizedError
from .gate import Gate
from .service import ChatService

settings = get_settings()
client = MetasoChatClient(settings)
gate = Gate(settings)
service = ChatService(settings, client, gate)

app = FastAPI(title="metaso-service", version="0.1.0",
              description="秘塔 AI 搜索（metaso.cn）免登录搜索对话服务 —— 契约见 docs/UPSTREAM.md")


def require_api_key(request: Request) -> None:
    """fail-closed 鉴权（baidu 同款）：配置了 METASO_API_KEYS 才启用；
    发现面（/health /v1/models）免鉴权。"""
    keys = settings.api_keys
    if not keys:
        return
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else auth
    if token not in keys:
        raise UnauthorizedError(
            "缺少或无效的 API key：请带 `Authorization: Bearer <key>` 请求头。")


@app.exception_handler(ServiceError)
async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "metaso-service",
        "version": "0.1.0",
        "ready": settings.metaso_ready,
        "upstream": client.diagnostics(),
        "gate": gate.stats(),
        "config": {"default_model": "metaso:search"},
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
    require_api_key(request)
    payload = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if payload.get("stream"):
        return StreamingResponse(
            service.stream_sse(payload, headers),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
    return JSONResponse(status_code=200, content=service.complete(payload, headers))


@app.get("/")
def root() -> dict:
    return {"service": "metaso-service", "endpoints": ["/v1/chat/completions", "/v1/models", "/health"],
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
