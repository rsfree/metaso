#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话编排：请求校验 → 会话坐标 → 上游流 → OpenAI 形态响应/SSE。

多轮语义：匿名态上游**不落库上下文**（契约 §3.3），正确性由每轮把完整历史
flatten 进单条 prompt 保证；会话登记表只是 token 层优化（续轮带上 conversationId）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterator

from .client import (
    METASO_MODELS,
    PLACEHOLDER_TOKEN,
    MetasoChatClient,
    resolve_model,
)
from .config import Settings
from .errors import (
    BadRequestError,
    DisabledUpstreamError,
    ServiceError,
)
from .gate import Gate

DEFAULT_MODEL = "metaso:search"


# ---------------------------------------------------------------------------
# 会话登记表（token 层优化；丢了也不会导致上下文错乱）
# ---------------------------------------------------------------------------

@dataclass
class SessionRef:
    chat_id: str = ""        # metaso conversationId（首轮 SSE 帧才拿到）
    parent_id: str | None = None


class SessionStore:
    def __init__(self, settings: Settings) -> None:
        self.ttl = settings.chat_session_ttl
        self.max = settings.chat_session_max
        self._data: dict[str, tuple[float, SessionRef]] = {}

    def get(self, key: str) -> SessionRef | None:
        item = self._data.get(key)
        if not item:
            return None
        at, ref = item
        if time.time() - at > self.ttl:
            self._data.pop(key, None)
            return None
        return ref

    def put(self, key: str, ref: SessionRef) -> None:
        if len(self._data) >= self.max:
            # 粗暴淘汰最旧的 —— 登记表丢失只影响续轮 token 复用，不影响正确性
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.time(), ref)


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------

def _content_text(content: Any) -> str:
    """OpenAI content：string 或 parts 数组（只收 text，图片明确拒绝）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                raise BadRequestError("messages 内容部件格式不正确", param="messages")
            if part.get("type") == "text":
                pieces.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                raise BadRequestError(
                    "metaso 是纯文本搜索对话，不接受图片输入；识图请用支持视觉的上游。",
                    param="messages")
            else:
                raise BadRequestError(
                    f"不支持的 messages 内容部件类型：{part.get('type')!r}",
                    param="messages")
        return "".join(pieces)
    raise BadRequestError("messages[].content 必须是字符串或部件数组", param="messages")


def flatten_history(messages: list[dict]) -> str:
    """把多轮完整历史压成单条 prompt —— 匿名上游不落库上下文，正确性全靠这里。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        text = _content_text(m.get("content")).strip()
        if not text:
            continue
        label = {"system": "系统", "user": "用户", "assistant": "助手"}.get(role, role)
        lines.append(f"{label}：{text}")
    if not lines:
        raise BadRequestError("messages 不能为空", param="messages")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

class ChatService:
    def __init__(self, settings: Settings | None = None,
                 client: MetasoChatClient | None = None,
                 gate: Gate | None = None) -> None:
        self.settings = settings or Settings()
        self.client = client or MetasoChatClient(self.settings)
        self.gate = gate or Gate(self.settings)
        self.sessions = SessionStore(self.settings)

    # ---------------------------------------------------------------- 校验

    def _prepare(self, payload: dict, headers: dict) -> tuple:
        route = resolve_model(str(payload.get("model") or DEFAULT_MODEL))
        if route is None:
            known = ", ".join(m.model for m in METASO_MODELS)
            raise BadRequestError(
                f"未知模型 {payload.get('model')!r}。可用模型：{known}"
                f"（别名见 README）。注意 metaso 只提供搜索型对话。",
                param="model")
        if not self.settings.metaso_ready:
            raise DisabledUpstreamError(
                "metaso 上游未启用（默认关闭）：设 METASO_ENABLED=1 或配 METASO_COOKIE。"
                "详见 /health。")

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise BadRequestError("messages 必须是非空数组", param="messages")

        prompt = flatten_history(messages)
        warnings: list[str] = []
        unsupported: list[str] = []
        if payload.get("enable_search"):
            warnings.append("metaso 本身就是搜索型对话，enable_search 恒为真，无需开启")
        if payload.get("enable_thinking"):
            unsupported.append("enable_thinking")
            warnings.append("metaso 没有思考开关：reasoning 增量（搜索动作/引用数/耗时）由上游自动产生")

        dry_run = bool(payload.get("dry_run")) or \
            str(headers.get("x-chat-dry-run", "")).lower() in ("1", "true", "yes")
        if str(headers.get("x-chat-dry-run", "")).lower() in ("0", "false", "no"):
            dry_run = False

        session_id = payload.get("session_id")
        ref = None
        if session_id:
            # 新会话也建空 ref（坐标由首轮 SSE 回填 adopt）
            ref = self.sessions.get(session_id) or SessionRef()
        return route, prompt, warnings, unsupported, dry_run, session_id, ref

    # ---------------------------------------------------------------- 干跑

    def _dry_run_body(self, route, prompt: str, ref: SessionRef | None) -> dict:
        token = self.client.cached_token()
        body = self.client.build_body(
            prompt, token or PLACEHOLDER_TOKEN,
            mode=route.mode, engine_type=route.engine_type,
            conversation_id=ref.chat_id if ref else None,
            parent_message_id=ref.parent_id if ref else None)
        body["_token_source"] = "cache" if token else "placeholder(干跑未抓取)"
        return body

    # ---------------------------------------------------------------- 事件流

    def _events(self, route, prompt: str, ref: SessionRef | None) -> Iterator[dict]:
        if self.gate.cooldown_remaining() > 0:
            self.gate.assert_not_cooling()
        self.gate.acquire()
        for ev in self.client.stream(
                prompt, mode=route.mode, engine_type=route.engine_type,
                conversation_id=ref.chat_id if ref else None,
                parent_message_id=ref.parent_id if ref else None):
            if ref is not None:
                meta = ev.get("meta") or {}
                if meta.get("conversation_id"):
                    ref.chat_id = str(meta["conversation_id"])
                if meta.get("response_message_id"):
                    ref.parent_id = str(meta["response_message_id"])
            yield ev

    # ---------------------------------------------------------------- 对外

    def complete(self, payload: dict, headers: dict | None = None) -> dict:
        headers = headers or {}
        route, prompt, warnings, unsupported, dry_run, session_id, ref = \
            self._prepare(payload, headers)

        if dry_run:
            body = self._dry_run_body(route, prompt, ref)
            return {
                "dry_run": True,
                "effective": {"upstream_request": body},
                "model": route.model,
                "warnings": warnings, "unsupported": unsupported,
            }

        content: list[str] = []
        reasoning: list[str] = []
        citations: list[Any] = []
        meta: dict[str, Any] = {}
        truncated = False
        t0 = time.time()
        try:
            for ev in self._events(route, prompt, ref):
                if ev.get("done"):
                    truncated = bool(ev.get("truncated"))
                    break
                d = ev.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                if "citations" in ev:
                    citations = ev["citations"]
                if ev.get("meta"):
                    meta.update(ev["meta"])
        except ServiceError:
            raise

        if truncated:
            warnings.append("上游流被提前断开，答案可能不完整")

        answer = "".join(content)
        if reasoning:
            warnings.append("reasoning_content 为搜索过程增量（动作/引用数/耗时），非思考链")
        message: dict[str, Any] = {"role": "assistant", "content": answer}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)

        out: dict[str, Any] = {
            "id": f"chatcmpl-{meta.get('result_id', 'metaso')}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": route.model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "upstream": {
                "name": "metaso",
                "model": route.model,
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "conversation_id": meta.get("conversation_id"),
                "result_id": meta.get("result_id"),
                "took": meta.get("took"),
            },
            "citations": citations,
            "warnings": warnings,
            "unsupported": unsupported,
        }
        if session_id and ref is not None:
            out["session"] = {"chat_id": ref.chat_id}
            self.sessions.put(session_id, ref)
        return out

    def stream_sse(self, payload: dict, headers: dict | None = None) -> Iterator[str]:
        headers = headers or {}
        route, prompt, warnings, unsupported, _dry, session_id, ref = \
            self._prepare(payload, headers)

        def chunk(delta: dict, *, finish: str | None = None) -> str:
            payload_chunk: dict[str, Any] = {
                "id": "chatcmpl-metaso",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": route.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload_chunk, ensure_ascii=False)}"

        yield chunk({"role": "assistant", "content": ""})
        # 首字节加速：路由完成即刻宣告（前端可立即渲染"搜索中"），不等上游开口
        yield f"data: {json.dumps({'meta': {'search_started': True, 'channel': route.model}}, ensure_ascii=False)}"
        if warnings or unsupported:
            yield f"data: {json.dumps({'warnings': warnings, 'unsupported': unsupported}, ensure_ascii=False)}"

        citations: list[Any] = []
        meta: dict[str, Any] = {}
        truncated = False
        try:
            for ev in self._events(route, prompt, ref):
                if ev.get("done"):
                    truncated = bool(ev.get("truncated"))
                    break
                d = ev.get("delta") or {}
                if d.get("reasoning_content"):
                    yield chunk({"reasoning_content": d["reasoning_content"]})
                if d.get("content"):
                    yield chunk({"content": d["content"]})
                if "citations" in ev:
                    citations = ev["citations"]
                    yield f"data: {json.dumps({'citations': citations}, ensure_ascii=False)}"
                if ev.get("meta"):
                    meta.update(ev["meta"])
                    # 上游控制帧（conversation_init 等）即刻透传 —— 比 reasoning 更早的首字节
                    yield f"data: {json.dumps({'meta': ev['meta']}, ensure_ascii=False)}"
        except ServiceError as e:
            yield f"data: {json.dumps({'error': e.body()['error']}, ensure_ascii=False)}"
            yield "data: [DONE]"
            return

        if truncated:
            yield f"data: {json.dumps({'warnings': ['上游流被提前断开，答案可能不完整']}, ensure_ascii=False)}"
        yield chunk({}, finish="stop")
        if session_id and ref is not None:
            self.sessions.put(session_id, ref)
            yield f"data: {json.dumps({'session': {'chat_id': ref.chat_id}}, ensure_ascii=False)}"
        yield "data: [DONE]"
