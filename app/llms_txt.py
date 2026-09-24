#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/llms.txt（LLM 说明书）与 / 落地页的渲染 —— fleet 统一约定。

🔴 铁律：内容**全部派生**，不许手抄 ——
  · 能力表 ← app.client.METASO_MODELS（注册表）
  · 错误表 ← app.errors 的异常类（遍历子类，杜绝幽灵码）
  · 计数当场 len() 计算；鉴权/可用性 ← settings
只有小节骨架与 curl 示例是静态文案。防漂移门禁见 tests/test_llms_txt.py。
"""
from __future__ import annotations

from typing import Iterator

from . import __version__
from .client import METASO_MODELS
from .config import Settings
from .errors import ServiceError

SERVICE_NAME = "metaso-service"


def _iter_error_classes(cls: type = ServiceError) -> Iterator[type]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _iter_error_classes(sub)


def _error_rows() -> list[tuple[str, str, int, bool, str]]:
    """从异常类派生错误表：(HTTP, code, err_type, retryable, 含义首行)。"""
    rows: list[tuple[str, str, int, bool, str]] = []
    for cls in sorted(_iter_error_classes(), key=lambda c: (c.status_code, c.code)):
        doc = (cls.__doc__ or "").strip().splitlines()
        rows.append((cls.code, cls.err_type, cls.status_code,
                     bool(cls.retryable), doc[0] if doc else ""))
    return rows


def render_llms_txt(settings: Settings) -> str:
    ready = settings.metaso_ready
    total = len(METASO_MODELS)
    avail = total if ready else 0
    auth_on = bool(settings.api_keys)
    lines: list[str] = []
    add = lines.append

    add(f"# {SERVICE_NAME}")
    add("")
    add("> 秘塔 AI 搜索（metaso.cn）**免登录**搜索对话服务：`POST /v1/chat/completions`")
    add("> 对齐 OpenAI Chat Completions，带 `citations` 与 `reasoning_content` 扩展。")
    add("> 本文件给 LLM/Agent 读；人类入口：`/`（HTML 落地页）。")
    add("")
    add("## 先读（危险项 / 额度）")
    add("")
    add("- **匿名额度按「出口 IP × 时间窗口」计（实测 ~13 发/窗口）**：烧干回 4001，"
        "同出口重试无效；服务端已内置自动恢复（换出口/换身份/换 TLS 指纹），"
        "调用方按下方错误表归因即可。")
    add("- `metaso:deepresearch` 匿名额度**必然拒绝**（帧 4001）—— 需上游登录态或官方 API。")
    add(f"- **鉴权：{'启用' if auth_on else '未启用'}** —— "
        + ("chat 端点必须 `Authorization: Bearer <key>`（fail-closed）；"
           "`/health` `/v1/models` `GET /` `/llms.txt` 免鉴权。"
           if auth_on else
           "当前部署未配置 `METASO_API_KEYS`，chat 端点开放（仅限回环/内网使用）。"))
    add("- 单一 search 型对话面：**不接受图片输入**，无思考开关；"
        "多轮请传完整历史（匿名上游不落库上下文，服务端已做 flatten）。")
    add("")
    add("## 端点")
    add("")
    add("| 方法 | 路径 | 鉴权 | 说明 |")
    add("|---|---|---|---|")
    add("| POST | `/v1/chat/completions` | " + ("Bearer key" if auth_on else "开放") +
        " | 搜索对话；`\"stream\": true` 走 SSE；`\"dry_run\": true` 干跑不触上游 |")
    add("| GET | `/v1/models` | 免 | 模型清单（未启用时返回空清单） |")
    add("| GET | `/health` | 免 | 就绪度 + 身份/出口/传输/闸门诊断（脱敏） |")
    add("| GET | `/llms.txt` | 免 | 本文件 |")
    add("| GET | `/` | 免 | HTML 落地页 |")
    add("")
    add(f"## 能力表（注册 {total} 项，本部署可用 {avail} 项）")
    add("")
    add("| 模型 | 说明 | mode | experimental | 备注 |")
    add("|---|---|---|---|---|")
    for m in METASO_MODELS:
        add(f"| `{m.model}` | {m.title} | {m.mode} | {'是' if m.experimental else '否'} "
            f"| {m.notes or '—'} |")
    add("")
    add("## 最小可用调用")
    add("")
    add("```bash")
    add("curl -s -X POST /v1/chat/completions -H 'content-type: application/json' \\")
    if auth_on:
        add("  -H 'Authorization: Bearer <key>' \\")
    add("  -d '{\"model\":\"metaso:search\",\"messages\":[{\"role\":\"user\",\"content\":\"今天有什么大新闻\"}]}'")
    add("")
    add("# 非流式返回：choices[0].message.content + citations + upstream 统计")
    add("# 流式：data: {chat.completion.chunk} … data: [DONE]")
    add("```")
    add("")
    add("## 错误表（从 app/errors.py 异常类派生，杜绝幽灵码）")
    add("")
    add("归因规则：`retryable=true` → 退避后重试同一请求；`false` → 改参/换模型/等窗口自愈。")
    add("")
    add("| HTTP | code | type | retryable | 含义 |")
    add("|---|---|---|---|---|")
    for code, etype, status, retryable, meaning in _error_rows():
        add(f"| {status} | `{code}` | {etype} | {'是' if retryable else '否'} | {meaning} |")
    add("")
    add("## 链接")
    add("")
    add("- 上游契约与全部实测取证：`docs/UPSTREAM.md`（§11.6 频控两层模型 + 出口地理层 + TLS 指纹层）")
    add("- 仓库：`rsfree/metaso`（公开）")
    add(f"- 版本：{__version__}")
    add("")
    return "\n".join(lines)


def render_landing(settings: Settings) -> str:
    ready = settings.metaso_ready
    total = len(METASO_MODELS)
    avail = total if ready else 0
    auth_on = bool(settings.api_keys)
    models = "".join(
        f"<tr><td><code>{m.model}</code></td><td>{m.title}</td>"
        f"<td>{'⚠️ 实验性' if m.experimental else '✓'}</td></tr>"
        for m in METASO_MODELS)
    auth_note = ("chat 端点需要 <code>Authorization: Bearer &lt;key&gt;</code>"
                 if auth_on else "当前未启用鉴权（仅限内网使用）")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SERVICE_NAME}</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222;line-height:1.6}}
code,pre{{background:#f4f4f4;border-radius:6px}} code{{padding:2px 6px}}
pre{{padding:14px;overflow-x:auto}} table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:6px 10px;text-align:left}} th{{background:#fafafa}}
a{{color:#0b62d6}} .muted{{color:#777;font-size:.9em}}
</style></head><body>
<h1>{SERVICE_NAME} <span class="muted">v{__version__}</span></h1>
<p>秘塔 AI 搜索（metaso.cn）的<b>免登录</b>搜索对话服务：一个 <code>POST /v1/chat/completions</code>
（OpenAI Chat Completions 对齐），返回答案 + <code>citations</code> 引用 + 搜索过程增量。
凭据免登录（指纹身份自动管理），额度与限流由服务端自适应。</p>
<h2>端点</h2>
<ul>
<li><code>POST /v1/chat/completions</code> — 搜索对话（SSE：<code>"stream": true</code>；干跑：<code>"dry_run": true</code>）— {auth_note}</li>
<li><code>GET /v1/models</code> — 模型清单（{avail}/{total} 可用）</li>
<li><code>GET /health</code> — 就绪度与出口/身份诊断</li>
<li><code>GET /llms.txt</code> — <b>给 LLM/Agent 读的说明书</b></li>
</ul>
<h2>能力（{avail}/{total}）</h2>
<table><tr><th>模型</th><th>说明</th><th>状态</th></tr>{models}</table>
<h2>试一下</h2>
<pre>curl -s -X POST /v1/chat/completions \\
  -H 'content-type: application/json' {'-H "Authorization: Bearer &lt;key&gt;" ' if auth_on else ''}\\
  -d '{{"model":"metaso:search","messages":[{{"role":"user","content":"今天有什么大新闻"}}]}}'</pre>
<p class="muted">频控两层模型（门票=指纹身份 / 额度=出口 IP×窗口）与全部实测取证见
docs/UPSTREAM.md；LLM 说明：<a href="/llms.txt">/llms.txt</a>。</p>
</body></html>"""
