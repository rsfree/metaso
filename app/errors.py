#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错误类型 —— 上游失败一律归一为可判定的服务错误，由 main.py 统一映射成 HTTP。"""
from __future__ import annotations


class ServiceError(Exception):
    """服务错误基类。status_code / code / err_type 决定对外 JSON 形态。"""

    status_code = 500
    code = "internal_error"
    err_type = "internal_error"
    retryable = False

    def __init__(self, message: str, *, detail: dict | None = None,
                 param: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.param = param
        self.retry_after = retry_after

    def body(self) -> dict:
        err: dict = {"message": self.message, "type": self.err_type, "code": self.code}
        if self.param:
            err["param"] = self.param
        if self.detail:
            err["detail"] = self.detail
        if self.retry_after:
            err["retry_after"] = self.retry_after
        return {"error": err}


class BadRequestError(ServiceError):
    status_code = 400
    code = "invalid_request_error"
    err_type = "invalid_request_error"


class RateLimitedError(ServiceError):
    """429：无票即拒 / 窗口抖动。退避可解（自动身份会换新重试一次）。"""

    status_code = 429
    code = "upstream_rate_limited"
    err_type = "rate_limit_error"
    retryable = True


class QuotaExhaustedError(ServiceError):
    """4001：出口窗口烧干。同出口重试无效 —— 换出口（池）/ 等窗口自愈。"""

    status_code = 429
    code = "upstream_quota_exhausted"
    err_type = "rate_limit_error"
    retryable = False


class RiskControlError(ServiceError):
    """WAF HTML 挑战 / 风控页。重试只会延长封锁。"""

    status_code = 503
    code = "upstream_risk_control"
    err_type = "service_unavailable_error"


class UpstreamUnavailableError(ServiceError):
    status_code = 502
    code = "upstream_unavailable"
    err_type = "api_error"


class ServiceUnavailableError(ServiceError):
    """本服务自己的闸门冷却期（对上游静默，避免持续施压）。"""

    status_code = 503
    code = "cooldown_active"
    err_type = "service_unavailable_error"


class DisabledUpstreamError(ServiceError):
    """上游未启用（默认关）：宁可 503 明说，也不假装可用。"""

    status_code = 503
    code = "capability_unavailable"
    err_type = "service_unavailable_error"
