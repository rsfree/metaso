#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单上游节流闸门：主动降速 + 滑动窗口 + 429 冷却。

定位（契约 §11.4/§11.6）：这是**适应**限流，不是绕过 —— 额度池按出口 IP×窗口计，
本闸门保证单出口不超速；真正的吞吐扩容靠 METASO_PROXY_POOL 换出口。
"""
from __future__ import annotations

import threading
import time

from .config import Settings
from .errors import ServiceUnavailableError


class Gate:
    def __init__(self, settings: Settings) -> None:
        self.min_interval = settings.ms_min_interval
        self.per_minute = settings.ms_per_minute
        self.cooldown = settings.ms_cooldown
        self._lock = threading.Lock()
        self._last = 0.0
        self._window: list[float] = []
        self._cooldown_until = 0.0
        self._rate_limited_total = 0

    # ---------------------------------------------------------------- 节流

    def acquire(self) -> None:
        """阻塞直到允许下一次请求（最小间隔 + 滑动窗口）。线程安全。"""
        while True:
            with self._lock:
                now = time.monotonic()
                wait = 0.0
                if self.min_interval > 0:
                    wait = max(wait, self._last + self.min_interval - now)
                if self.per_minute > 0:
                    self._window = [t for t in self._window if now - t < 60.0]
                    if len(self._window) >= self.per_minute:
                        wait = max(wait, 60.0 - (now - self._window[0]))
                if wait <= 0:
                    self._last = now
                    if self.per_minute > 0:
                        self._window = [t for t in self._window if now - t < 60.0]
                        self._window.append(now)
                    return
            time.sleep(min(wait, 1.0))

    # ---------------------------------------------------------------- 冷却

    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - time.monotonic())

    def assert_not_cooling(self) -> None:
        """冷却期内直接快失败（对上游静默 —— 持续施压只会延长封锁）。"""
        remain = self.cooldown_remaining()
        if remain > 0:
            raise ServiceUnavailableError(
                f"metaso 闸门冷却中（还剩 {remain:.0f}s）：上一轮 429 后对上游静默，"
                f"冷却结束自动恢复。",
                retry_after=remain)

    def mark_rate_limited(self) -> None:
        with self._lock:
            self._cooldown_until = time.monotonic() + self.cooldown
            self._rate_limited_total += 1

    def tune(self, min_interval: float) -> None:
        """按凭据模式调整节流间隔（登录态实测可持续 ~4 发/10s ⇒ 2.5s；匿名 15s）。"""
        self.min_interval = min_interval

    # ---------------------------------------------------------------- 诊断

    def stats(self) -> dict:
        with self._lock:
            return {
                "min_interval_s": self.min_interval,
                "per_minute": self.per_minute,
                "cooldown_s": self.cooldown,
                "cooldown_remaining_s": round(max(0.0, self._cooldown_until - time.monotonic()), 1),
                "window_used": len(self._window),
                "rate_limited_total": self._rate_limited_total,
            }
