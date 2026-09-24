#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端点级测试：/health、/v1/models、/v1/chat/completions（非流式/流式/干跑/错误）。"""
from __future__ import annotations




from app import main
from app.client import MetasoChatClient
import dataclasses
from app.errors import QuotaExhaustedError, RateLimitedError


CHAT = "/v1/chat/completions"


def test_health_reports_channels(api):
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["ready"] is True
    guest = body["channels"]["guest"]
    assert guest["mode"] == "anonymous"
    assert guest["ready"] is True
    assert guest["egress"] == "direct"
    assert set(guest["gate"]) >= {"min_interval_s", "cooldown_remaining_s"}
    assert "Bearer" in body["routing"]["login"]


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

    monkeypatch.setattr(main.guest_channel.gate, "_cooldown_until", 10 ** 12)
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


def test_bearer_login_cookie_routes_to_login_channel(api, monkeypatch):
    """Bearer 值含 uid+sid ⇒ 自动建登录通路（独立 client 实例），guest 计数不动。"""
    monkeypatch.setattr(main, "login_channels", main.LoginChannelCache())
    r = api.post(CHAT,
                 headers={"Authorization": "Bearer tid=t;_c_WBKFRo=x;_nb_ioWEgULi=;uid=u1;sid=s1"},
                 json={"model": "metaso:search",
                       "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 200
    assert len(main.login_channels) == 1, "按 cookie 建缓存通路"
    cached = next(iter(main.login_channels._data.values()))[1]
    assert cached.client.mode == "login"
    assert cached.client is not main.guest_channel.client, "必须是独立通路实例"
    assert main.guest_channel.sessions._data == {}, "guest 通路不应被使用"


def test_bearer_login_cookie_cached_across_calls(api, monkeypatch):
    """同一 cookie 的重复调用复用同一登录通路（会话表落在缓存通路上）。"""
    monkeypatch.setattr(main, "login_channels", main.LoginChannelCache())
    payload = {"model": "metaso:search", "session_id": "s-login",
               "messages": [{"role": "user", "content": "q"}]}
    for _ in range(2):
        r = api.post(CHAT, headers={"Authorization": "Bearer uid=u1; sid=s1"}, json=payload)
        assert r.status_code == 200
    assert len(main.login_channels) == 1, "同一 cookie 复用同一通路"
    cached = next(iter(main.login_channels._data.values()))[1]
    assert list(cached.sessions._data) == ["s-login"], "请求路由到了缓存通路"
    assert main.guest_channel.sessions._data.get("s-login") is None, "guest 通路未被使用"


def test_bearer_non_cookie_routes_to_guest(api, monkeypatch):
    """非 cookie（Bearer guest / 无 token / 乱值）一律走匿名通路。"""
    monkeypatch.setattr(main, "login_channels", main.LoginChannelCache())
    guest = main.guest_channel
    for i, auth in enumerate(({"Authorization": "Bearer guest"}, {},
                              {"Authorization": "Bearer garbage-token"})):
        r = api.post(CHAT, headers=auth,
                     json={"model": "metaso:search", "session_id": f"s-g{i}",
                           "messages": [{"role": "user", "content": "q"}]})
        assert r.status_code == 200
    assert set(guest.sessions._data) == {"s-g0", "s-g1", "s-g2"}, "全部落在匿名通路"
    assert len(main.login_channels) == 0, "非 cookie bearer 不得建登录通路"


def test_not_ready_returns_empty_models_and_503_search(api, monkeypatch):



    frozen = dataclasses.replace(main.settings, ms_enabled=False, ms_cookie="")
    monkeypatch.setattr(main, "settings", frozen)
    monkeypatch.setattr(main.guest_channel, "settings", frozen)
    body = api.get("/v1/models").json()
    assert body["data"] == [] and body["available"] is False
    r = api.post(CHAT, json={"model": "metaso:search",
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 503
    assert "未启用" in r.json()["error"]["message"]
