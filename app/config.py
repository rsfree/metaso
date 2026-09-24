#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集中配置 —— 全部从环境变量读取（与 biz-api 的 METASO_* 同名同义，便于迁移）。

设计原则：凭据只从 env 读，不落盘进代码；缺省值取保守侧（限速偏慢、默认关闭）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _s(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return (raw if raw is not None else default).strip()


def _b(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name) or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _s("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _i("PORT", 8110))

    # ------------------------------------------------------------ 上游：metaso
    # 默认关（保守姿态）：匿名额度按出口窗口计（~13 发/窗口），上游随时可能收紧。
    ms_enabled: bool = field(default_factory=lambda: _b("METASO_ENABLED", False))
    ms_base: str = field(default_factory=lambda: _s("METASO_BASE", "https://metaso.cn").rstrip("/"))
    # 钉死身份 Cookie（登录态或自备指纹值）。留空 = 自动生成随机指纹身份。
    ms_cookie: str = field(default_factory=lambda: _s("METASO_COOKIE"))
    ms_proxy: str = field(default_factory=lambda: _s("METASO_PROXY"))
    # 出口代理池：逗号/换行分隔；非空优先于 ms_proxy。换出口=换额度窗口（§11.6 追记）。
    ms_proxy_pool: str = field(default_factory=lambda: _s("METASO_PROXY_POOL"))
    ms_timeout: float = field(default_factory=lambda: _f("METASO_TIMEOUT", 180.0))
    ms_token_ttl: float = field(default_factory=lambda: _f("METASO_TOKEN_TTL", 600.0))
    ms_min_interval: float = field(default_factory=lambda: _f("METASO_MIN_INTERVAL", 15.0))
    ms_per_minute: int = field(default_factory=lambda: _i("METASO_PER_MINUTE", 4))
    ms_cooldown: float = field(default_factory=lambda: _f("METASO_COOLDOWN", 900.0))
    ms_user_agent: str = field(default_factory=lambda: _s(
        "METASO_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    ))
    # 传输钉死：requests（默认）| curl_cffi。实测（2026-09-24）：可疑出口上
    # python-requests 的 TLS 指纹会被门禁拒（帧 429），Chrome 指纹（curl_cffi）通过
    # —— 疑似环境跑容器时直接钉 curl_cffi。
    ms_transport: str = field(default_factory=lambda: _s("METASO_TRANSPORT", "requests").lower())

    # ------------------------------------------------------------ 会话登记表
    chat_session_ttl: float = field(default_factory=lambda: _f("CHAT_SESSION_TTL", 1800.0))
    chat_session_max: int = field(default_factory=lambda: _i("CHAT_SESSION_MAX", 512))

    # ------------------------------------------------------------ 派生
    @property
    def metaso_ready(self) -> bool:
        """启用条件：显式 METASO_ENABLED=1 或钉死 METASO_COOKIE。"""
        return bool(self.ms_enabled or self.ms_cookie)

    @property
    def metaso_proxy_pool(self) -> list[str]:
        """出口代理池：逗号/换行分隔，去空项。非空时优先于 `ms_proxy`。"""
        raw = (self.ms_proxy_pool or "").strip()
        if not raw:
            return []
        parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
        return [p for p in parts if p]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
