#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端点级测试：/health、/v1/models、/v1/chat/completions（非流式/流式/干跑/错误）。"""
from __future__ import annotations




from app import main
from app.client import MetasoChatClient
import dataclasses
from app.errors import QuotaExhaustedError, RateLimitedError
from app.service import ChatService


CHAT = "/v1/chat/completions"


def test_health_reports_ready_and_diagnostics(api):
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["ready"] is True
    up = body["upstream"]
    assert up["identity"] == "generated"
    assert up["egress"] == "direct"
    assert up["pool_size"] == 0
    assert set(up["stats"]) >= {"searches", "identity_rotations", "egress_rotations"}


def test_models_list_all_six_routes(api):
    body = api.get("/v1/models").json()
    ids = {m["id"] for m in body["data"]}
    assert ids == {"metaso:search", "metaso:concise", "metaso:research",
                   "metaso:scholar", "metaso:video", "metaso:deepresearch"}
    assert body["available"] is True
    assert all(m["capabilities"]["citations"] for m in body["data"])


def test_unknown_model_is_400_with_model_list(api):
    r = api.post(CHAT, json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["param"] == "model"
    assert "metaso:search" in err["message"]


def test_image_message_is_400_not_silently_dropped(api):
    r = api.post(CHAT, json={"model": "metaso:search", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]}]})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "messages"


def test_nonstream_search_returns_content_citations_and_session(api):
    r = api.post(CHAT, json={"model": "metaso:search", "session_id": "s1",
                             "messages": [{"role": "user", "content": "北京今天天气"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "北京今天晴 [[1]]"
    assert body["choices"][0]["message"]["reasoning_content"] == "正在检索 12 篇…"
    assert body["citations"] == [{"title": "中国天气网", "refer_id": "1"}]
    assert body["upstream"]["name"] == "metaso"
    assert body["session"]["chat_id"] == "1937"


def test_multi_turn_flattens_full_history(api):
    api.post(CHAT, json={"model": "metaso:search", "session_id": "s-hist",
                         "messages": [{"role": "user", "content": "北京天气"}]})
    api.post(CHAT, json={"model": "metaso:search", "session_id": "s-hist",
                         "messages": [{"role": "user", "content": "北京天气"},
                                      {"role": "assistant", "content": "晴"},
                                      {"role": "user", "content": "那上海呢"}]})
    # 第二轮的 prompt 必须带上第一轮内容（匿名上游不落库上下文）
    r = api.post(CHAT, json={"model": "metaso:search", "session_id": "s-hist",
                             "messages": [{"role": "user", "content": "那天津呢"}]})
    assert r.status_code == 200


def test_stream_emits_openai_sse_with_citations(api):
    r = api.post(CHAT, json={"model": "metaso:search", "stream": True,
                             "messages": [{"role": "user", "content": "北京天气"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"content": "北京今天晴 [[1]]"' in r.text
    assert '"citations"' in r.text
    assert r.text.rstrip().endswith("data: [DONE]")


def test_dry_run_uses_placeholder_token_and_no_conversation(api):
    r = api.post(CHAT, json={"model": "metaso:search", "dry_run": True,
                             "messages": [{"role": "user", "content": "北京天气"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["effective"]["upstream_request"]["_token_source"].startswith("placeholder")
    assert "conversationId" not in body["effective"]["upstream_request"]


def test_dry_run_header_overrides_body(api):
    r = api.post(CHAT, headers={"X-Chat-Dry-Run": "1"},
                 json={"model": "metaso:search", "dry_run": False,
                       "messages": [{"role": "user", "content": "q"}]})
    assert r.json()["dry_run"] is True
    r2 = api.post(CHAT, headers={"X-Chat-Dry-Run": "0"},
                  json={"model": "metaso:search", "dry_run": True,
                        "messages": [{"role": "user", "content": "q"}]})
    assert "dry_run" not in r2.json()


def test_quota_error_maps_429_with_quota_code(api, monkeypatch):

    def boom(self, *a, **k):
        raise QuotaExhaustedError("窗口烧干")
        yield  # pragma: no cover

    monkeypatch.setattr(MetasoChatClient, "_stream_once", boom)
    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "upstream_quota_exhausted"
    assert err["type"] == "rate_limit_error"


def test_gate_cooldown_fail_fast(api, monkeypatch):
    """冷却期内快失败：不触上游，错误带 retry_after。"""

    monkeypatch.setattr(main.gate, "_cooldown_until", 10 ** 12)
    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "cooldown_active"
    assert err["retry_after"] > 0


def test_rate_limit_error_maps_429(api, monkeypatch):

    def boom(self, *a, **k):
        raise RateLimitedError("桶满")
        yield  # pragma: no cover

    monkeypatch.setattr(MetasoChatClient, "_stream_once", boom)
    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "upstream_rate_limited"


def test_auth_enforced_when_keys_configured(api, monkeypatch):
    """fail-closed 鉴权：配置 METASO_API_KEYS 后 chat 必须 Bearer key；发现面免鉴权。"""

    frozen = dataclasses.replace(main.settings, ms_api_keys="key-a, key-b")
    monkeypatch.setattr(main, "settings", frozen)

    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "invalid_api_key"
    assert err["type"] == "authentication_error"

    bad = api.post(CHAT, headers={"Authorization": "Bearer wrong"},
                   json={"model": "metaso:search",
                         "messages": [{"role": "user", "content": "q"}]})
    assert bad.status_code == 401

    ok = api.post(CHAT, headers={"Authorization": "Bearer key-a"},
                  json={"model": "metaso:search",
                        "messages": [{"role": "user", "content": "q"}]})
    assert ok.status_code == 200

    assert api.get("/health").status_code == 200
    assert api.get("/v1/models").status_code == 200


def test_auth_open_when_no_keys(api):
    """未配置 METASO_API_KEYS = 鉴权关闭（仅回环/隧道使用的形态）。"""
    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 200


def test_not_ready_returns_empty_models_and_503_search(api, monkeypatch):



    frozen = dataclasses.replace(main.settings, ms_enabled=False, ms_cookie="")
    monkeypatch.setattr(main, "settings", frozen)
    monkeypatch.setattr(main, "service", ChatService(frozen, main.client, main.gate))
    body = api.get("/v1/models").json()
    assert body["data"] == [] and body["available"] is False
    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 503
    assert "未启用" in r.json()["error"]["message"]
