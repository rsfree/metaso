#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具：零真实网络 —— 上游一律桩化。

环境在 import app 前钉死（enabled=1 + 时间参数归零），理由同 textin/conftest：
缺省值里的 15s 间隔与 900s 冷却会让测试套件假死。
"""
from __future__ import annotations

import os

os.environ.setdefault("METASO_ENABLED", "1")
os.environ.setdefault("METASO_MIN_INTERVAL", "0")
os.environ.setdefault("METASO_PER_MINUTE", "0")
os.environ.setdefault("METASO_COOLDOWN", "0")
# 确保走「自动随机身份」路径（钉死 COOKIE 的用例自行 dataclasses.replace）
os.environ.pop("METASO_COOKIE", None)
os.environ.pop("METASO_PROXY_POOL", None)
os.environ.pop("METASO_PROXY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.client import MetasoChatClient  # noqa: E402
from app.main import app, guest_channel, login_channels  # noqa: E402

#: 一轮假上游事件的固定剧本（覆盖 meta/delta/citations/done 全形态）
FAKE_STREAM = [
    {"meta": {"conversation_id": "1937", "response_message_id": "8848"}},
    {"delta": {"reasoning_content": "正在检索 12 篇…"}},
    {"delta": {"content": "北京今天晴 [[1]]"}},
    {"citations": [{"title": "中国天气网", "refer_id": "1"}]},
    {"done": True},
]


@pytest.fixture
def stub_stream(monkeypatch):
    """把 Client._stream_once 换成不触网的固定剧本，并记录调用参数。"""
    calls: list[dict] = []

    def fake_once(self, query, *, mode, engine_type="",
                  conversation_id=None, parent_message_id=None):
        calls.append({"query": query, "mode": mode, "engine_type": engine_type,
                      "conversation_id": conversation_id,
                      "parent_message_id": parent_message_id})
        yield from FAKE_STREAM

    monkeypatch.setattr(MetasoChatClient, "_stream_once", fake_once)
    return calls


@pytest.fixture
def api(stub_stream):
    """TestClient + 已桩化的上游（每次测试全新会话表与登录通路缓存）。"""
    guest_channel.sessions._data.clear()
    login_channels._data.clear()
    with TestClient(app) as c:
        yield c
