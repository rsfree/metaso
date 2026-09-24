#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metaso 上游客户端 —— 契约来源 docs/UPSTREAM.md（含 §11.6+追记频控两层模型）。

四件必须先讲清的事（做错不会报错、只会静默出错）：

**1. 首轮请求体顶层绝不能出现 conversationId / parentMessageId。**
一旦传 `temp-<uuid>`，后端拿它查库，0.1s 回 `-500 Unable to find ConversationEntity`，
极易误判成「参数非法」或「风控」。两个键只在拿到真实数字 id 后才写。

**2. 频控是两层模型（2026-09-24 实测定稿）。**
门票层 = 指纹 cookie（tid/_c_WBKFRo/_nb_ioWEgULi）的**存在性**（不验内容、不看 IP，
伪造 XFF 无效）；额度层 = **出口 IP×时间窗口**（实测 ~13 发/窗口，烧干后任何身份
4001）。所以：未配凭据时自动生成随机指纹身份（免登录自足）；身份轮换只解决门票，
**换出口（代理池）才是换窗口**。

**3. 传输兜底阶梯：纯 HTTP → curl_cffi → Playwright（暂缓）。**
WAF 按 TLS 指纹挑战时升级 `curl_cffi impersonate="chrome"` 并粘住；若上游未来收紧
到校验指纹值内容，改用 scripts/mint_identity.py 铸造真值配 METASO_COOKIE。

**4. SSE 内容帧本就是 OpenAI 形态**，本层主要收敛控制帧、把 [[n]]/citations 提出来、
把 4001/429/-500 映射成可判定错误。
"""
from __future__ import annotations

import json
import re
import secrets
import string
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Iterator

import requests

from .config import Settings
from .errors import (
    QuotaExhaustedError,
    RateLimitedError,
    RiskControlError,
    ServiceError,
    UpstreamUnavailableError,
)

#: 页面里的 `<meta id="meta-token" content="...">`（自闭合，content 内含 + / =）
META_TOKEN_RE = re.compile(r'<meta\s+id="meta-token"\s+content="([^"]+)"\s*/?>')

#: 干跑时用的占位 token —— 干跑的定义就是「不触上游」
PLACEHOLDER_TOKEN = "<meta-token:干跑未抓取>"

#: 门票三件套（指纹 cookie 名）—— 随机同形值即可过（服务端只验存在性）
FINGERPRINT_COOKIES: dict[str, int] = {
    "tid": 36,
    "_c_WBKFRo": 40,
    "_nb_ioWEgULi": 0,
}

#: 钉死身份（含登录态）429 的恢复等待：实测突发窗口 +10s 恢复（2026-09-24）
PIN_RETRY_WAIT = 10.0

_ALNUM = string.ascii_letters + string.digits


@dataclass(frozen=True)
class ModelRoute:
    model: str
    title: str
    mode: str
    engine_type: str = ""
    experimental: bool = False
    notes: str = ""


#: 只暴露实测过的形态；`pdf`/`image`/`podcast` 引擎未实测不列（不制造假能力）。
METASO_MODELS: tuple[ModelRoute, ...] = (
    ModelRoute("metaso:search", "秘塔 AI 搜索 · 深入", "detail",
               notes="默认形态；一次约 8~15s"),
    ModelRoute("metaso:concise", "秘塔 AI 搜索 · 简洁", "concise"),
    ModelRoute("metaso:research", "秘塔 AI 搜索 · 研究", "research"),
    ModelRoute("metaso:scholar", "秘塔 AI 搜索 · 学术", "detail", "scholar"),
    ModelRoute("metaso:video", "秘塔 AI 搜索 · 视频", "detail", "video"),
    ModelRoute("metaso:deepresearch", "秘塔 AI 搜索 · 深度研究", "strong-research",
               experimental=True,
               notes="匿名额度必然拒绝（4001），需登录态或官方 API 额度"),
)

#: 旧写法 → 规范名。会话表以规范名为键，别名来回写会不停重建上游会话。
METASO_ALIASES: dict[str, str] = {
    "metaso": "metaso:search",
    "秘塔": "metaso:search",
    "meta-search": "metaso:search",
    "ai-search": "metaso:search",
    "metaso/search": "metaso:search",
    "ai-search-pro": "metaso:research",
    "meta-research": "metaso:research",
    "meta-deepsearch": "metaso:research",
    "meta-search:scholar": "metaso:scholar",
    "meta-search/video": "metaso:video",
    "meta-deepresearch": "metaso:deepresearch",
}

_ROUTE_BY_MODEL = {r.model: r for r in METASO_MODELS}


def resolve_model(raw: str) -> ModelRoute | None:
    """模型名解析：规范名精确匹配 → 别名归一。刻意不猜（mode 决定额度消耗）。"""
    if not raw:
        return None
    if raw in _ROUTE_BY_MODEL:
        return _ROUTE_BY_MODEL[raw]
    alias = METASO_ALIASES.get(raw.strip().lower())
    return _ROUTE_BY_MODEL[alias] if alias else None


def generate_fingerprint_cookies() -> dict[str, str]:
    """生成一枚随机指纹身份（同形随机值）。

    定位：不是伪造指纹 —— 服务端只验存在性，我们也没有复刻任何指纹算法。
    若上游未来收紧为「验内容」，改用 scripts/mint_identity.py 的真浏览器铸造值。
    """
    return {name: ("".join(secrets.choice(_ALNUM) for _ in range(n)) if n > 0 else "")
            for name, n in FINGERPRINT_COOKIES.items()}


def _parse_cookie(raw: str) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    if not raw:
        return jar
    for key, morsel in SimpleCookie(raw).items():
        jar.set(key, morsel.value, domain="metaso.cn", path="/")
    return jar


def _iter_sse_lines(r: Any, *, buffered: bool = False) -> Iterator[str]:
    """requests 与 curl_cffi 两种响应的流式行迭代统一入口。

    buffered=True（curl_cffi 兜底档）：其 iter_content 在部分版本未实现
    （NotImplementedError）—— SSE 体量小（几十 KB），整读切行可接受。
    """
    if not buffered:
        if hasattr(r, "iter_lines"):
            try:
                yield from r.iter_lines(decode_unicode=True)
                return
            except (TypeError, NotImplementedError):
                pass
    body = r.content
    for line in body.split(b"\n"):
        yield line.decode("utf-8", "replace")


def _loads(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class MetasoChatClient:
    """长生命周期单例：cookie jar / token / 身份 / 出口 / 传输状态都挂在实例上。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = requests.Session()
        self.http.headers.update({
            "user-agent": settings.ms_user_agent,
            "accept-language": "zh-CN",
        })
        self.transport = "requests"
        self._curl = None
        self._curl_cffi_missing = False
        self.identity_generated = False
        self.stats = {"token_refreshes": 0, "searches": 0, "errors": 0,
                      "rate_limited": 0, "quota_hits": 0, "html_pages": 0,
                      "identity_rotations": 0, "transport_escalations": 0,
                      "egress_rotations": 0}
        if settings.ms_cookie:
            self.http.cookies.update(_parse_cookie(settings.ms_cookie))
            if "tid" not in {c.name for c in self.http.cookies}:
                # 登录 cookie 未带门票三件套 ⇒ 自动补随机指纹（存在即可、值不重要；
                # rotate_identity 只写三件套，不动 uid/sid 登录对）
                self.rotate_identity()
        else:
            self.rotate_identity()

        # 凭据模式（决定 429 恢复策略与节流节奏，见 stream）：
        #   login     — cookie 含 uid+sid：走登录积分（500/天），突发限速 ~4 发/10s
        #   pinned    — 钉死指纹/自备身份（无 uid+sid）
        #   anonymous — 自动随机指纹身份（~13 发/出口/窗口）
        if settings.ms_cookie:
            self.mode = ("login" if ("uid=" in settings.ms_cookie and "sid=" in settings.ms_cookie)
                         else "pinned")
        else:
            self.mode = "anonymous"

        # 出口：代理池 > 单代理 > 直连。池按槽位粘住，失败类错误触发轮换。
        self.pool = list(settings.metaso_proxy_pool)
        self.pool_idx = 0
        if self.pool:
            self._apply_egress(self.pool[0])
        elif settings.ms_proxy:
            self.http.proxies = {"http": settings.ms_proxy, "https": settings.ms_proxy}
        # 池（尤其是「按连接轮换」的隧道型）必须每次请求新建连接 —— keep-alive 会把
        # 出口钉死在单一 IP 上（实测：容器长连接被钉在热 IP 上 6 连 429）。
        self.connection_close = bool(self.pool)
        # 传输钉死（METASO_TRANSPORT=curl_cffi）：可疑出口上 python-requests 的 TLS
        # 指纹会被门禁拒（实测帧 429），Chrome 指纹通过 —— 容器部署建议钉死。
        if settings.ms_transport == "curl_cffi":
            self._escalate_transport()

        self._token = ""
        self._token_at = 0.0
        self.logged_in: bool | None = None

    # ---------------------------------------------------------------- 身份

    def rotate_identity(self) -> None:
        """换一枚随机指纹身份。

        ⚠️ 实测预期（probe_rate_limit.py）：轮换拿到的只是「门票」；额度池按
        IP×窗口计（~13 发），烧干后新身份同样 429/4001 —— 轮换不刷新池，
        **换出口才是换窗口**。只对自动生成的身份使用；钉死的 COOKIE 不擅自覆盖。
        """
        for name, value in generate_fingerprint_cookies().items():
            self.http.cookies.set(name, value, domain="metaso.cn", path="/")
        self.identity_generated = True
        self.stats["identity_rotations"] += 1

    # ---------------------------------------------------------------- 出口

    def _apply_egress(self, proxy_url: str) -> None:
        """钉出口并作废旧出口绑定的会话状态（token 重取成本=一次首页 GET，免费）。"""
        self.http.proxies = {"http": proxy_url, "https": proxy_url}
        self._curl = None
        self.transport = "requests"
        self._token = ""
        self._token_at = 0.0

    def rotate_egress(self) -> bool:
        """round-robin 推进到下一个出口槽并配新身份。池为空返回 False。"""
        if not self.pool:
            return False
        self.pool_idx = (self.pool_idx + 1) % len(self.pool)
        self._apply_egress(self.pool[self.pool_idx])
        if self.identity_generated:
            self.rotate_identity()
        self.stats["egress_rotations"] += 1
        return True

    def egress_label(self) -> str:
        """脱敏出口描述（诊断用）：只露 scheme://host:port，不带凭据。"""
        if not self.pool:
            return "single-proxy" if self.settings.ms_proxy else "direct"
        u = urllib.parse.urlparse(self.pool[self.pool_idx])
        netloc = u.hostname or "?"
        if u.port:
            netloc = f"{netloc}:{u.port}"
        return f"slot {self.pool_idx + 1}/{len(self.pool)} {u.scheme}://{netloc}"

    # ---------------------------------------------------------------- 传输

    def _escalate_transport(self) -> bool:
        """WAF 按 TLS 指纹挑战时升级 curl_cffi（impersonate=chrome）并粘住。"""
        if self.transport != "requests":
            return True
        if self._curl_cffi_missing:
            return False
        try:
            from curl_cffi import requests as curl_requests  # noqa: PLC0415 可选依赖，缺席要能降级
        except ImportError:
            self._curl_cffi_missing = True
            return False
        curl = curl_requests.Session(impersonate="chrome")
        curl.cookies.update({c.name: c.value for c in self.http.cookies})
        curl.headers.update({
            "user-agent": self.settings.ms_user_agent,
            "accept-language": "zh-CN",
        })
        if self.http.proxies:
            curl.proxies = dict(self.http.proxies)
        self._curl = curl
        self.transport = "curl_cffi"
        self.stats["transport_escalations"] += 1
        return True

    def _http_get(self, url: str, *, timeout: float,
                  connection_close: bool = False) -> Any:
        headers = {"connection": "close"} if connection_close else None
        if self.transport == "curl_cffi":
            return self._curl.get(url, timeout=timeout, headers=headers)
        return self.http.get(url, timeout=timeout, headers=headers)

    def _http_post_stream(self, url: str, *, body: dict,
                          headers: dict, timeout: Any) -> Any:
        if self.connection_close:
            headers = {**headers, "connection": "close"}
        if self.transport == "curl_cffi":
            # curl_cffi 0.16 的流式迭代未实现（iter_content → NotImplementedError）
            # ⇒ 整包下载（SSE 体量小），由 _iter_sse_lines(buffered=True) 切行
            return self._curl.post(url, json=body, headers=headers,
                                   timeout=float(timeout[1]))
        return self.http.post(url, json=body, headers=headers,
                              stream=True, timeout=timeout)

    # ---------------------------------------------------------------- 凭据

    def cached_token(self) -> str:
        fresh = self._token and (time.time() - self._token_at) < self.settings.ms_token_ttl
        return self._token if fresh else ""

    def _fetch_token(self) -> str:
        """GET / 取 meta-token，随后 GET /api/my-info 换 JSESSIONID（两步同 jar）。"""
        try:
            r = self._http_get(f"{self.settings.ms_base}/",
                               timeout=min(30.0, self.settings.ms_timeout),
                               connection_close=self.connection_close)
        except Exception as e:  # noqa: BLE001 - requests/curl_cffi 异常族不同，统一转译
            raise UpstreamUnavailableError(
                f"metaso 首页请求失败: {e}") from e

        if r.status_code != 200:
            raise self._classify(r.status_code, r.text)
        m = META_TOKEN_RE.search(r.text or "")
        if not m:
            raise self._classify(r.status_code, r.text,
                                 extra="页面里没有 #meta-token（可能是验证页或站点改版）")

        # 预热会话换 JSESSIONID；免登录时 errCode=401 属正常
        try:
            info = self._http_get(f"{self.settings.ms_base}/api/my-info",
                                  timeout=min(30.0, self.settings.ms_timeout),
                                  connection_close=self.connection_close)
            self.logged_in = info.json().get("errCode") == 0
        except Exception:  # noqa: BLE001 - 预热失败不致命，搜索仍可能成功
            self.logged_in = bool(self.settings.ms_cookie)

        self._token = m.group(1)
        self._token_at = time.time()
        self.stats["token_refreshes"] += 1
        return self._token

    def _token_or_raise(self) -> str:
        tok = self.cached_token()
        return tok if tok else self._fetch_token()

    # ---------------------------------------------------------------- 请求体

    @staticmethod
    def build_body(query: str, token: str, *, mode: str, engine_type: str = "",
                   conversation_id: str | None = None,
                   parent_message_id: str | None = None,
                   lang: str = "中文") -> dict:
        """构造 /api/search/chat 请求体。

        关键约束：**首轮顶层不能出现 conversationId / parentMessageId**（上游会拿它
        查库 → 0.1s 回 -500）；续轮必须是真实数字 id。
        """
        msg: dict[str, Any] = {
            "id": f"temp-{uuid.uuid4()}",
            "key": f"temp-{uuid.uuid4()}",
            "conversationId": conversation_id or f"temp-{uuid.uuid4()}",
            "role": "user",
            "content": query,
            "markdownContent": query,
            "engineType": engine_type,
            "filter": "all",
            "contentType": 0,
            "outputHtml": False,
            "mode": mode,
            "model": None,
            "outputStyle": "正常",
        }
        body: dict[str, Any] = {
            "model": None,
            "stream": True,
            "messages": [msg],
            "engineType": engine_type,
            "mode": mode,
            "filter": "all",
            "outputHtml": False,
            "outputStyle": "正常",
            "darkMode": False,
            "outputLanguage": lang,
            "htmlNoDisplayEnable": True,
            "displayContent": query,
            "metaso-pc": "pc",
            "token": token,
        }
        if conversation_id:
            body["conversationId"] = conversation_id
            if parent_message_id:
                msg["parentId"] = parent_message_id
                body["parentMessageId"] = parent_message_id
        return body

    # ---------------------------------------------------------------- 错误

    def _classify(self, status: int, text: str, extra: str = "") -> ServiceError:
        head = (text or "")[:200]
        low = head.lower()
        if (status in (401, 403)
                or "<!doctype" in low or "<html" in low
                or "aliyun" in low or "滑动验证" in head or "captcha" in low):
            self.stats["html_pages"] += 1
            return RiskControlError(
                f"metaso 上游返回 HTML 验证页（阿里云 WAF 人机验证），HTTP {status}"
                f"{'：' + extra if extra else ''}。该挑战不能靠重试通过，持续施压只会延长封锁。",
                detail={"http_status": status, "body_head": head})
        if status == 429:
            self.stats["rate_limited"] += 1
            return RateLimitedError(
                "metaso 上游限流（HTTP 429：当前身份/出口被限）。门禁键是指纹身份而非 IP；"
                "配了代理池则自动轮换下一槽（额度池按出口窗口计）。")
        return UpstreamUnavailableError(
            f"metaso 上游 HTTP {status}{'：' + extra if extra else ''}",
            detail={"http_status": status, "body_head": head})

    @staticmethod
    def _frame_error(obj: dict) -> ServiceError:
        code = obj.get("code")
        msg = str(obj.get("msg") or "")[:200]
        if code == 429:
            return RateLimitedError(
                f"metaso 限流（帧 code=429）：{msg}。门禁键是指纹身份而非 IP —— "
                f"自动生成的身份将换新重试。")
        if code == 4001:
            return QuotaExhaustedError(
                f"metaso 匿名额度窗口耗尽（帧 code=4001）：{msg}。同出口重试无效 —— "
                f"额度池按出口 IP×窗口计（实测 ~13 发/窗口）；配 METASO_PROXY_POOL 自动"
                f"换出口，或 METASO_COOKIE 走登录额度，或改用官方付费 API（/api/v1/search）。",
                detail={"code": code})
        if code == -500:
            return UpstreamUnavailableError(
                f"metaso 返回 -500（{msg}）。已知成因是首轮请求带了顶层 conversationId"
                f"—— 若在此出现，说明上游行为已变化。",
                detail={"code": code})
        return UpstreamUnavailableError(
            f"metaso 错误帧 code={code}: {msg}", detail={"code": code})

    # ---------------------------------------------------------------- SSE

    def stream(self, query: str, *, mode: str, engine_type: str = "",
               conversation_id: str | None = None,
               parent_message_id: str | None = None) -> Iterator[dict]:
        """逐条 yield 归一化事件；三类失败轴自动恢复（未吐内容事件前才重试）：

        - 4001（窗口烧干）：有池换下一出口重试一次；无池上抛（自愈前重试无意义）；
        - 429（无票即拒/热 IP）：**池（轮换隧道）下重试预算 3 次** —— 每次换出口=
          换一个新出口 IP 抽样（429 响应 ~0.3s，抽样成本极低），第 2 次失败后升级
          Chrome TLS 指纹；无池只试 1 次（换身份）；钉死身份上抛；
        - WAF 挑战：有池先换出口，仍被挑再升级 curl_cffi；不可用则上抛。
        总尝试封顶 6。
        """
        emitted_content = False   # 只看内容事件；meta 控制帧后重试是安全的（实测 429 前有 meta）
        quota_tried = False
        rate_retries = 0
        pin_retries = 0
        unavail_retries = 0
        waf_actions = 0
        # 429 重试预算：池（尤其轮换隧道）下每次重试=换一个新出口 IP 抽样；无池只试 1 次。
        max_rate_retries = 3 if self.pool else 1
        for _attempt in range(6):
            try:
                for ev in self._stream_once(
                        query, mode=mode, engine_type=engine_type,
                        conversation_id=conversation_id,
                        parent_message_id=parent_message_id):
                    if "delta" in ev or "citations" in ev:
                        emitted_content = True
                    yield ev
                return
            except QuotaExhaustedError:
                if emitted_content or quota_tried or not self.pool:
                    raise
                quota_tried = True
                self.rotate_egress()
            except RateLimitedError:
                if emitted_content:
                    raise
                if self.mode in ("login", "pinned"):
                    # 钉死身份（含登录态）：429 = 突发窗口（实测 +10s 恢复）——
                    # **绝不能换身份**（换指纹丢登录态）；有池可换出口（cookie 不动）；
                    # 等待后**原样重试**，预算 2 次。
                    if pin_retries >= 2:
                        raise
                    pin_retries += 1
                    if self.pool:
                        self.rotate_egress()
                    time.sleep(PIN_RETRY_WAIT)
                else:
                    if rate_retries >= max_rate_retries or not self.identity_generated:
                        raise
                    rate_retries += 1
                    if not self.rotate_egress():
                        self.rotate_identity()
                    if rate_retries == 2:
                        # 两连 429：换身份+换出口都不行 ⇒ 很可能是 TLS 指纹层被拒
                        # （python-requests JA3，实测 2026-09-24）⇒ 升级 Chrome 指纹
                        if not self._escalate_transport():
                            raise
                    if self.pool:
                        time.sleep(0.6)   # 换连接后给上游闸门一点缓冲
                if self.pool:
                    time.sleep(0.6)   # 换连接后给上游闸门一点缓冲
            except UpstreamUnavailableError:
                # 连接级失败（隧道 curl 56 / 偶发 reset）= 本次抽样没抽好 ⇒
                # 池模式下换一条连接（=换出口抽样）重试两次；无池上抛。
                if emitted_content or unavail_retries >= 2 or not self.pool:
                    raise
                unavail_retries += 1
                self.rotate_egress()
                time.sleep(0.6)
            except RiskControlError:
                if emitted_content or waf_actions >= 2:
                    raise
                waf_actions += 1
                if self.pool and waf_actions == 1:
                    self.rotate_egress()
                elif not self._escalate_transport():
                    raise

    def _stream_once(self, query: str, *, mode: str, engine_type: str = "",
                     conversation_id: str | None = None,
                     parent_message_id: str | None = None) -> Iterator[dict]:
        token = self._token_or_raise()
        body = self.build_body(query, token, mode=mode, engine_type=engine_type,
                               conversation_id=conversation_id,
                               parent_message_id=parent_message_id)
        url = f"{self.settings.ms_base}/api/search/chat"
        headers = {
            "accept": "text/event-stream",
            "content-type": "application/json",
            "metaso-pc": "pc",
            "origin": self.settings.ms_base,
            "referer": f"{self.settings.ms_base}/",
        }
        try:
            r = self._http_post_stream(url, body=body, headers=headers,
                                       timeout=(15.0, self.settings.ms_timeout))
        except Exception as e:  # noqa: BLE001 - requests/curl_cffi 异常族不同，统一转译
            raise UpstreamUnavailableError(f"metaso 搜索请求失败: {e}") from e

        self.stats["searches"] += 1
        done = False
        try:
            if r.status_code != 200:
                head = ""
                try:
                    head = (r.text or "")[:300]
                except Exception:  # noqa: BLE001 - 读流式 body 失败不掩盖原错误
                    head = ""
                raise self._classify(r.status_code, head)

            for line in _iter_sse_lines(
                    r, buffered=(self.transport == "curl_cffi")):
                raw = (line or "").strip()
                if not raw:
                    continue
                if raw.startswith("data:"):
                    raw = raw[5:].strip()
                if raw == "[DONE]":
                    done = True
                    yield {"done": True}
                    break
                obj = _loads(raw)
                if obj is None:
                    continue
                yield from self._normalize(obj)
        finally:
            close = getattr(r, "close", None)
            if close:
                close()

        if not done:
            # 流被上游断开：不能当成正常结束（否则调用方拿到半截答案却以为完成）
            yield {"done": True, "truncated": True}

    @staticmethod
    def _normalize(obj: dict) -> Iterator[dict]:
        """把 metaso 的三类帧收敛成四类事件：meta / delta / citations / done。"""
        kind = obj.get("type")
        if kind == "heartbeat":
            return
        if kind == "error":
            raise MetasoChatClient._frame_error(obj)

        if kind in ("conversation_init", "user_message_init", "response_message_init"):
            key = {"conversation_init": "conversation_id",
                   "user_message_init": "user_message_id",
                   "response_message_init": "response_message_id"}[kind]
            yield {"meta": {key: (obj.get("data") or {}).get("id")}}
            return

        if kind is not None:
            # 未知控制帧：不猜语义，原样带出去（除 heartbeat）
            yield {"meta": {"frame": kind}}
            return

        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            out: dict[str, Any] = {}
            if delta.get("reasoning_content"):
                out["reasoning_content"] = delta["reasoning_content"]
            if delta.get("content"):
                out["content"] = delta["content"]
            if out:
                yield {"delta": out}
            meta_keys = ("action", "action_status", "total_cite_num", "took")
            meta = {k: delta[k] for k in meta_keys if delta.get(k) is not None}
            rid = obj.get("id")
            if rid:
                meta["result_id"] = rid
            if meta:
                yield {"meta": meta}
            if delta.get("citations"):
                yield {"citations": delta["citations"]}
            elif delta.get("highlights"):
                yield {"meta": {"highlights": delta["highlights"]}}

    # ---------------------------------------------------------------- 诊断

    def diagnostics(self) -> dict:
        return {
            "base": self.settings.ms_base,
            "ready": self.settings.metaso_ready,
            "mode": self.mode,
            "logged_in": self.logged_in,
            "cookie_configured": bool(self.settings.ms_cookie),
            "identity": "pinned" if self.settings.ms_cookie else "generated",
            "transport": self.transport,
            "egress": self.egress_label(),
            "pool_size": len(self.pool),
            "token_cached": bool(self.cached_token()),
            "stats": dict(self.stats),
        }
