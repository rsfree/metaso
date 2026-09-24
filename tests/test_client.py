#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端层测试：请求体形状 / SSE 归一化 / 错误映射 / 身份与出口 / 传输阶梯。

对应契约 docs/UPSTREAM.md：§0 最大坑（首轮顶层 conversationId）、§11.6 频控两层模型。
"""
from __future__ import annotations

import dataclasses
import string
import time

import pytest

from app.client import (
    FINGERPRINT_COOKIES,
    MetasoChatClient,
    generate_fingerprint_cookies,
    resolve_model,
)
from app.errors import (
    QuotaExhaustedError,
    RateLimitedError,
    RiskControlError,
    UpstreamUnavailableError,
)
from app.config import get_settings


def make_client(**overrides) -> MetasoChatClient:
    s = dataclasses.replace(get_settings(), **overrides)
    return MetasoChatClient(s)


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

def test_first_round_body_omits_top_level_conversation_id():
    """最大的坑：首轮顶层带 conversationId → 上游 0.1s 回 -500。必须有机械断言。"""
    body = MetasoChatClient.build_body("北京天气", "tok-1", mode="detail")
    assert "conversationId" not in body
    assert "parentMessageId" not in body
    assert body["messages"][0]["conversationId"].startswith("temp-")
    assert body["token"] == "tok-1"
    assert body["mode"] == "detail"
    assert body["stream"] is True


def test_continuation_body_carries_ids():
    body = MetasoChatClient.build_body("他老婆是谁", "tok-1", mode="detail",
                                       engine_type="scholar",
                                       conversation_id="1937", parent_message_id="8848")
    assert body["conversationId"] == "1937"
    assert body["parentMessageId"] == "8848"
    assert body["messages"][0]["parentId"] == "8848"
    assert body["engineType"] == "scholar"


def test_resolve_model_exact_alias_and_miss():
    assert resolve_model("metaso:search").mode == "detail"
    assert resolve_model("ai-search").model == "metaso:search"
    assert resolve_model("meta-deepresearch").experimental is True
    assert resolve_model("gpt-4o") is None
    assert resolve_model("") is None


# ---------------------------------------------------------------------------
# SSE 归一化与错误映射
# ---------------------------------------------------------------------------

def test_control_frames_fold_into_meta_and_heartbeat_dropped():
    assert list(MetasoChatClient._normalize(
        {"type": "conversation_init", "data": {"id": "1937"}})) == \
        [{"meta": {"conversation_id": "1937"}}]
    assert list(MetasoChatClient._normalize({"type": "heartbeat"})) == []
    frames = list(MetasoChatClient._normalize(
        {"id": "res-1", "choices": [{"delta": {"content": "晴", "total_cite_num": 3}}]}))
    assert {"delta": {"content": "晴"}} in frames
    assert {"meta": {"result_id": "res-1", "total_cite_num": 3}} in frames
    assert list(MetasoChatClient._normalize(
        {"choices": [{"delta": {"citations": [{"title": "天气"}]}}]})) == \
        [{"citations": [{"title": "天气"}]}]


def test_error_frames_map_4001_quota_429_rate_and_minus500():
    quota = MetasoChatClient._frame_error({"code": 4001, "msg": "搜索次数超出限制"})
    assert isinstance(quota, QuotaExhaustedError)
    assert quota.retryable is False

    limited = MetasoChatClient._frame_error({"code": 429, "msg": "too many"})
    assert isinstance(limited, RateLimitedError)
    assert limited.retryable is True

    lost = MetasoChatClient._frame_error({"code": -500, "msg": "Unable to find ConversationEntity"})
    assert isinstance(lost, UpstreamUnavailableError)
    assert "conversationId" in lost.message


# ---------------------------------------------------------------------------
# 身份 / 出口 / 传输
# ---------------------------------------------------------------------------

def test_identity_generated_matches_declared_shapes():
    vals = generate_fingerprint_cookies()
    assert set(vals) == set(FINGERPRINT_COOKIES)
    for name, n in FINGERPRINT_COOKIES.items():
        assert len(vals[name]) == n
        assert all(ch in string.ascii_letters + string.digits for ch in vals[name])


def test_auto_identity_seeded_and_rotated_once_on_429(monkeypatch):
    c = make_client()
    assert c.identity_generated is True
    assert set(FINGERPRINT_COOKIES) <= {ck.name for ck in c.http.cookies}

    calls = {"n": 0}

    def fake_once(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitedError("桶满")
        yield from [{"delta": {"content": "ok"}}, {"done": True}]

    monkeypatch.setattr(MetasoChatClient, "_stream_once", fake_once)
    before = c.stats["identity_rotations"]
    events = list(c.stream("q", mode="detail"))
    assert calls["n"] == 2, "429 后必须恰好重试一次"
    assert events[-1] == {"done": True}
    assert c.stats["identity_rotations"] == before + 1


def test_pinned_identity_never_rotated(monkeypatch):
    c = make_client(ms_cookie="aliyungf_tc=pinned")
    assert c.identity_generated is False
    calls = {"n": 0}

    def boom(self, *a, **k):
        calls["n"] += 1
        raise RateLimitedError("桶满")

    monkeypatch.setattr(MetasoChatClient, "_stream_once", boom)
    with pytest.raises(RateLimitedError):
        list(c.stream("q", mode="detail"))
    assert calls["n"] == 1
    assert c.stats["identity_rotations"] == 0


def test_no_retry_after_events_emitted(monkeypatch):
    """已向调用方吐过事件再遇 429：不能重试（会重复正文）。"""
    c = make_client(ms_cookie="aliyungf_tc=pinned")

    def burst(self, *a, **k):
        yield {"delta": {"content": "半截"}}
        raise RateLimitedError("桶满")

    monkeypatch.setattr(MetasoChatClient, "_stream_once", burst)
    with pytest.raises(RateLimitedError):
        list(c.stream("q", mode="detail"))


def test_transport_pin_escalates_at_boot():
    """METASO_TRANSPORT=curl_cffi：启动即钉 Chrome TLS 指纹（容器部署推荐）。"""
    c = make_client(ms_transport="curl_cffi")
    assert c.transport == "curl_cffi"
    c2 = make_client()
    assert c2.transport == "requests"


def test_4001_rotates_egress_once_when_pool_present(monkeypatch):
    c = make_client(ms_proxy_pool="http://a:1,http://b:2")
    assert c.http.proxies["http"] == "http://a:1"
    calls = {"n": 0}

    def fake_once(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise QuotaExhaustedError("窗口烧干")
        yield from [{"done": True}]

    monkeypatch.setattr(MetasoChatClient, "_stream_once", fake_once)
    events = list(c.stream("q", mode="concise"))
    assert calls["n"] == 2
    assert c.http.proxies["http"] == "http://b:2", "4001 有池必须换出口重试"
    assert c.stats["egress_rotations"] == 1
    assert events[-1] == {"done": True}


def test_4001_without_pool_propagates_immediately(monkeypatch):
    c = make_client(ms_proxy_pool="  ")
    calls = {"n": 0}

    def boom(self, *a, **k):
        calls["n"] += 1
        raise QuotaExhaustedError("烧干")

    monkeypatch.setattr(MetasoChatClient, "_stream_once", boom)
    with pytest.raises(QuotaExhaustedError):
        list(c.stream("q", mode="concise"))
    assert calls["n"] == 1


def test_egress_rotation_invalidates_token_and_wraps():
    c = make_client(ms_proxy_pool="http://u:p@pool:2086, socks5://10.0.0.1:1080",
                    ms_proxy="http://ignored:1")
    assert c.http.proxies["http"] == "http://u:p@pool:2086", "池优先于单代理"
    c._token, c._token_at = "old-tok", time.time()
    c.rotate_egress()
    assert c.http.proxies["https"] == "socks5://10.0.0.1:1080"
    assert c._token == "" and c._token_at == 0.0, "换出口必须作废旧 token"
    c.rotate_egress()
    assert c.http.proxies["http"] == "http://u:p@pool:2086", "round-robin 回绕"
    label = c.egress_label()
    assert "u:p" not in label and "pool:2086" in label, "诊断必须脱敏凭据"


def test_waf_challenge_escalates_transport_and_retries_once(monkeypatch):
    c = make_client(ms_cookie="aliyungf_tc=pinned")
    calls = {"n": 0}

    def fake_once(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RiskControlError("HTML 挑战页")
        yield from [{"delta": {"content": "ok"}}, {"done": True}]

    def fake_escalate(self):
        self.transport = "curl_cffi"
        return True

    monkeypatch.setattr(MetasoChatClient, "_stream_once", fake_once)
    monkeypatch.setattr(MetasoChatClient, "_escalate_transport", fake_escalate)
    events = list(c.stream("q", mode="detail"))
    assert calls["n"] == 2
    assert c.transport == "curl_cffi", "升级必须粘住"
    assert events[-1] == {"done": True}

    c2 = make_client(ms_cookie="aliyungf_tc=pinned")
    calls2 = {"n": 0}

    def boom(self, *a, **k):
        calls2["n"] += 1
        raise RiskControlError("挑战")

    monkeypatch.setattr(MetasoChatClient, "_stream_once", boom)
    monkeypatch.setattr(MetasoChatClient, "_escalate_transport", lambda self: False)
    with pytest.raises(RiskControlError):
        list(c2.stream("q", mode="detail"))
    assert calls2["n"] == 1, "curl_cffi 不可用时挑战必须原样上抛"


def test_http_429_and_waf_page_classified():
    c = MetasoChatClient.__new__(MetasoChatClient)
    c.settings = get_settings()
    c.stats = {"rate_limited": 0, "html_pages": 0}
    limited = c._classify(429, "rate limited")
    assert isinstance(limited, RateLimitedError)
    waf = c._classify(403, "<!DOCTYPE html><html>滑动验证</html>")
    assert isinstance(waf, RiskControlError)
