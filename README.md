# metaso-service

秘塔 AI 搜索（metaso.cn）**免登录**搜索对话服务 —— 纯 HTTP、无浏览器依赖，
`POST /v1/chat/completions` 对齐 OpenAI Chat Completions（带 `citations` /
`reasoning_content` 标准扩展）。

> 定位：自用 / 研究。上游提供官方付费 API（`/api/v1/search`），生产用途请走正路。
> 本仓是 rsfree 家族一员（同源：`rsfree/textin`、`rsfree/baidu`）。

## 端点

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 搜索对话（`stream: true` 走 SSE；`dry_run: true` 干跑不触上游）—— 需 Bearer key（配置了 `METASO_API_KEYS` 时） |
| GET | `/v1/models` | 模型清单（6 条路由；未启用时返回空清单）—— 免鉴权 |
| GET | `/health` | 就绪度 + 身份/出口/传输诊断（脱敏）—— 免鉴权 |
| GET | `/llms.txt` | **给 LLM/Agent 读的说明书**（从注册表与异常类派生，防漂移门禁覆盖）—— 免鉴权 |
| GET | `/` | HTML 落地页 —— 免鉴权 |

模型：`metaso:search`（默认）/ `concise` / `research` / `scholar` / `video` /
`deepresearch`（实验性，匿名必撞 4001）。别名：`metaso`、`ai-search`、`meta-search` 等。

## 频控两层模型（2026-09-24 实测，详见 docs/UPSTREAM.md §11.6+追记）

| 层 | 键 | 行为 | 服务对策 |
|---|---|---|---|
| 门票层 | 指纹 cookie **存在性**（不验内容、不看 IP） | 无票 = 秒 429 | 自动生成随机指纹身份 |
| 额度层 | **出口 IP × 时间窗口**（~13 发/窗口） | 烧干 = 4001，任何身份都拒 | `METASO_PROXY_POOL` 换出口=换窗口 |

失败恢复（各至多重试一次，未吐事件前）：`4001`→换出口（有池）/上抛（无池）；
`429`→换身份+换出口；WAF→换出口→`curl_cffi impersonate=chrome` 粘住升级。

## 凭据模式（自动识别，节流与恢复策略随模式调整）

| 模式 | 触发条件 | 额度 | 429 恢复策略 |
|---|---|---|---|
| **login** | `METASO_COOKIE` 含 `uid`+`sid` | **登录积分 500/天**（1 发≈1 积分，首答 1~1.3s） | 等待 10s（实测恢复窗口）后**原样重试**——绝不换身份（会丢登录态） |
| **pinned** | `METASO_COOKIE` 无 uid+sid | 同匿名（钉死指纹） | 等待重试（不轮换钉死身份） |
| **anonymous** | 无 `METASO_COOKIE`（默认） | 匿名 ~13 发/出口/窗口 | 换身份 → 换出口 → Chrome TLS 指纹 |

节流自动调速：登录 2.5s / 匿名 15s（env 显式设置 `METASO_MIN_INTERVAL` 时以 env 为准）。
频控实测数字见 docs/UPSTREAM.md §11.6 追记六。

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
METASO_ENABLED=1 uvicorn app.main:app --port 8110

curl -s localhost:8110/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"metaso:search","messages":[{"role":"user","content":"今天有什么大新闻"}]}'
```

## 配置

全部 env 注入（见 `.env.example`）：`METASO_ENABLED` / `METASO_COOKIE`（钉死身份，留空
自动生成）/ `METASO_PROXY` / `METASO_PROXY_POOL` / `METASO_TIMEOUT` / `METASO_TOKEN_TTL` /
`METASO_MIN_INTERVAL` / `METASO_PER_MINUTE` / `METASO_COOLDOWN`。

## 测试与工具

```bash
pip install -r requirements-dev.txt
pytest                 # 离线全绿（上游一律桩化，零真实请求）
ruff check app tests scripts
scripts/probe.py       # 干跑打印请求体；--live 真实发一次（消耗 1 发窗口额度）
scripts/smoke.sh       # 起真服务打零消耗端点
scripts/mint_identity.py  # Playwright 铸造真值指纹身份（暂缓启用，后备手段）
```

## 契约

上游逆向契约与全部实测取证：**[docs/UPSTREAM.md](docs/UPSTREAM.md)**
（含频控两层模型的完整差分矩阵与 4001/429 处置依据）。
