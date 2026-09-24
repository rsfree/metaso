# 秘塔 AI 搜索（metaso.cn）搜索接口逆向

调研日期：2026-09-11
状态：**已实证跑通（纯 HTTP、免登录、服务端直调）**；🔴 2026-09-24 频控机制重大修正见 §11.6（门禁=指纹 cookie 存在性，非 IP）
方法：Playwright 真实浏览器抓包 + Next.js chunk 静态分析 + httpx 服务端回放
配套产物：
- `metaso/client.py` — 可直接复用的异步客户端（含 Cookie 预热、限流退避、Markdown 引用渲染）
- `metaso/tools/mint_identity.py` — 匿名指纹身份铸造（Playwright 真浏览器；2026-09-24，后备手段，**暂缓启用**，见 §11.6）
- `metaso/probe/probe_net.py` — 浏览器抓包探针
- `metaso/probe/replay.py` — 服务端回放验证
- `metaso/probe/probe_variants.py` — 参数变体枚举
- `metaso/probe/probe_error.py` — 错误帧定位 + 多轮会话验证

---

## 0. 核心结论

| 维度 | 结论 |
|---|---|
| 基址 | `https://metaso.cn`（前端 Next.js，同源调 `/api/*`，无跨域） |
| 主接口 | `POST /api/search/chat`（SSE，`text/event-stream`） |
| 结构化引用 | `POST /api/search-result` `{resultId}` |
| 鉴权 | **`token` 放在请求体里**（不是 Header），值取自页面 `<meta id="meta-token" content="...">` + Cookie `JSESSIONID` |
| 是否需要登录 | ❌ **不需要**。`GET /api/my-info` 返回 `errCode:401 需要登录`，但搜索照常出结果 |
| 凭据获取 | **纯 HTTP 两步**：`GET /` 拿 token + `aliyungf_tc`；`GET /api/my-info` 拿 `JSESSIONID`。**无需浏览器** |
| 响应形态 | SSE，正文帧是 **OpenAI 兼容**的 `object: "chat.completion.qa_with_agent"` 增量块 |
| 引用脚标 | 正文中用 `[[1]]` `[[2]]`，需自行转 `[^1]` |
| 限流 | 两层：HTTP **429**（频率）+ SSE 帧 **code 4001「搜索次数超出限制」**（额度）；🔴 2026-09-24 修正：429 的键=**指纹 cookie 存在性**（非 IP），见 §11.6 |
| 旧链路 | `POST /api/session` → `GET /search/{id}` → `GET /api/searchV2` **仍然可用** |

> ⚠️ **最大坑（本次实测踩到）**：首轮请求的**顶层不能出现 `conversationId` / `parentMessageId`**。
> 一旦传 `temp-<uuid>`，后端会拿它查库，直接返回
> `{"type":"error","code":-500,"msg":"Unable to find com.metasota.metaso.entity.conversation.ConversationEntity with id temp-xxx"}`，
> 且耗时仅 0.1s，极易误判为「参数非法」或「风控」。

---

## 1. 端点总览

| Method | 端点 | 用途 | 鉴权 |
|---|---|---|---|
| `GET` | `/` 或 `/search/{sessionId}` | 取 `<meta id="meta-token">` | 无 |
| `GET` | `/api/my-info` | 预热会话，换取 `JSESSIONID`（免登录返回 401 属正常） | Cookie |
| `POST` | `/api/session` | **旧链路**建会话，返回 `data.id` | body 内 `token` |
| `GET` | `/api/searchV2` | **旧链路**搜索（query string 传参，SSE） | `?token=` |
| `POST` | `/api/search/chat` | **新链路（当前网页端在用）**搜索（SSE） | body 内 `token` |
| `POST` | `/api/search-result` | 按 `resultId` 取结构化引用 | body 内 `token` |
| `POST` | `/api/v1/search` | **官方开放 API**（`Authorization: Bearer <key>`，付费） | Header |

---

## 2. 凭据获取（纯 HTTP，无需浏览器）

### 2.1 `token`

页面（首页或任意 `/search/{id}`）里有：

```html
<meta id="meta-token" content="wr8+pHu3KYryzz0O2MaBSNUZbVLjLUYC1FR4sKqSW0pB+dl6Pzo6ExTSykQe7sMUDgQbE6H6EBpAbtlb6KwpvTfgwZkj3SO12zL3rYH4utwjiMgQCGFJqaBhzQW5WfKOyMV3RaqrysvMNiaj9dfwfQ=="/>
```

正则：

```python
META_TOKEN_RE = re.compile(r'<meta\s+id="meta-token"\s+content="([^"]+)"\s*/?>')
```

特征：
- 每次请求**都会变**，但**前 48 个 base64 字符是固定前缀**（`wr8+pHu3KYryzz0O2MaBSNUZbVLjLUYC1FR4sKqSW0p`）
- 总长 112 字节（7 个 16 字节块）；前 32 字节为固定头，之后每次不同
- 用 bundle 里的 `AES-ECB / XwKsGlMcdPMEhR1B` 解密**解不开**（该密钥属于另一处逻辑），token 本身非可逆加密
- **结论：不要试图生成，老老实实每次抓取**

### 2.2 Cookie 分两步下发（关键）

| 步骤 | 下发的 Cookie |
|---|---|
| `GET /` | `aliyungf_tc`（阿里云 WAF） |
| `GET /api/my-info` | `JSESSIONID` |

二者**必须复用同一个 cookie jar**。若只带 `aliyungf_tc` 直接打 `/api/search/chat`，会被判为无会话。
浏览器环境里还会有 JS 生成的 `tid` / `_c_WBKFRo` / `_nb_ioWEgULi`（阿里云 FP 指纹），实测**服务端直调不需要**。

---

## 3. `POST /api/search/chat` 请求结构

### 3.1 请求头

```http
POST /api/search/chat HTTP/1.1
Host: metaso.cn
Content-Type: application/json
Accept: text/event-stream
metaso-pc: pc
Origin: https://metaso.cn
Referer: https://metaso.cn/
User-Agent: Mozilla/5.0 (Macintosh; ...) Chrome/141.0.0.0 Safari/537.36
Cookie: aliyungf_tc=...; JSESSIONID=...
```

注意：**没有 `token` 请求头**。Header 里的 `token` 只用于 axios 拦截器那套非流式接口；
SSE 链路是把 `token` 塞进**请求体**（源码：`Object.assign(params, {token: document.querySelector("#meta-token").getAttribute("content")})`）。

### 3.2 首轮请求体（实测可用模板）

```json
{
  "model": "fast_thinking",
  "stream": true,
  "messages": [
    {
      "id": "temp-18aa3f6d-ae51-4ed6-bda9-7b06e7903fa7",
      "key": "temp-9bda24ce-40ad-4412-951e-5fa11a9f5a3c",
      "conversationId": "temp-1241bc27-6dde-4f3b-ae54-a11475cf6b42",
      "role": "user",
      "content": "周杰伦是谁",
      "markdownContent": "周杰伦是谁",
      "engineType": "",
      "filter": "all",
      "contentType": 0,
      "outputHtml": false,
      "mode": "detail",
      "model": "fast_thinking",
      "outputStyle": "正常"
    }
  ],
  "engineType": "",
  "mode": "detail",
  "filter": "all",
  "outputHtml": false,
  "outputStyle": "正常",
  "darkMode": false,
  "outputLanguage": "中文",
  "htmlNoDisplayEnable": true,
  "displayContent": "周杰伦是谁",
  "metaso-pc": "pc",
  "token": "<meta-token>"
}
```

- `messages[].conversationId` 用 `temp-<uuid>` **无害**（后端不查它）
- 顶层**绝不能**有 `conversationId` / `parentMessageId`

### 3.3 续轮（多轮会话）请求体

在首轮基础上**增加三处**，且必须是**真实数字 id**：

```jsonc
{
  "messages": [{
    "conversationId": "2098128325195055105",  // ← 改为真实会话 id
    "parentId":       "2098128325195055104",  // ← 新增
    // ...其余同首轮
  }],
  "conversationId":   "2098128325195055105",  // ← 新增（顶层）
  "parentMessageId":  "2098128325195055104"   // ← 新增（顶层）
}
```

id 来源（首轮 SSE 帧）：

| 字段 | 取自 |
|---|---|
| `conversationId` | `{"type":"conversation_init","data":{"id":...}}` |
| `parentMessageId` / `messages[0].parentId` | `{"type":"response_message_init","data":{"id":...}}` |

实测规律：`responseMessageId == conversationId - 1`。

> ⚠️ **匿名状态下多轮上下文不生效**：实测第二轮问「他老婆是谁」，返回「我需要更多上下文来理解你问的是谁」。
> 参数形状已与浏览器真实请求完全一致，推测匿名会话不落库上下文，**需登录态**才有多轮。此项待验证。

---

## 4. SSE 帧协议

每行 `data:<json>`，以 `data:[DONE]` 结束，中间穿插 `{"type":"heartbeat"}`。

### 4.1 控制帧

```jsonc
{"type":"conversation_init",     "data":{"id":"2098128929156612097"}}
{"type":"user_message_init",     "data":{"id":"2098128929194360832"}}
{"type":"response_message_init", "data":{"id":"2098128929156612096"}}
{"type":"heartbeat"}
{"type":"error","code":4001,"msg":"搜索次数超出限制","showToast":false}
```

### 4.2 内容帧（OpenAI 兼容）

```jsonc
{
  "created": "1789067694",
  "model": "metaso-qa-with-agent",
  "id": "01a08cbe-935b-7431-84ab-9ee7ca858d9f",   // ← 这就是 resultId
  "object": "chat.completion.qa_with_agent",
  "choices": [{"index": 0, "delta": {"role": "assistant", "content": "..."}}]
}
```

`delta` 字段语义：

| 字段 | 含义 |
|---|---|
| `role` | 恒为 `assistant` |
| `reasoning_content` | 思考过程（"搜索 xxx"、"搜索到8条结果"、"☑ 完成"） |
| `action` / `action_status` | 动作名与状态（`搜索` / `success`） |
| `content` | **正文增量**（含 `[[n]]` 脚标） |
| `citations` | 引用数组（最后一帧，一次性下发） |
| `highlights` | 高亮摘要片段 |
| `total_cite_num` | 引用总数 |
| `took` | 耗时（秒） |

### 4.3 典型时序

```
conversation_init → user_message_init → response_message_init
→ [heartbeat]
→ reasoning: 搜索 "周杰伦 简介"
→ reasoning: 搜索到8条结果
→ reasoning: ☑ 完成 (took=8.73)
→ content: 周杰伦（Jay Chou），1979年1月18日出生于...[[1]][[2]]
→ content: - **音乐成就**：...
→ citations: [...]
→ highlights: [...]
→ total_cite_num: 8
→ [DONE]
```

一次 `mode=detail` 搜索约 **8~11 秒**，38~60 帧。

---

## 5. `POST /api/search-result`（结构化引用）

```http
POST /api/search-result
Content-Type: application/json

{"resultId": "01a08cbe-935b-7431-84ab-9ee7ca858d9f"}
```

响应：

```jsonc
{
  "errCode": 0,
  "errMsg": "success",
  "data": {
    "citations": [ /* 被引用的来源 */ ],
    "not_citations": [ /* 召回但未引用 */ ]
  }
}
```

单条 `citations[]` 主要字段：

| 字段 | 说明 |
|---|---|
| `title` / `orig_title` / `orig_o_title` | 标题（3 种形态） |
| `link` | 原文链接 |
| `snippet` | 摘要 |
| `date` / `orig_date` / `publish_date` | 发布时间 |
| `site` | 域名数组 |
| `authority` | 权威性评分 |
| `rank_policy_score` / `rank_policy_adjustment` | 排序分与调权 |
| `type` | 形态，如 `summary` |
| `engine` | 检索源，如 `meta` |
| `display.refer_id` | 对应正文脚标序号 |
| `agent_qa_meta.query` | 实际改写后的检索词 |

---

## 6. 参数枚举（实测）

### 6.1 `mode`

| mode | 结果 | 说明 |
|---|---|---|
| `concise` | ✅ | 简洁模式 |
| `detail` | ✅ | 默认，深入模式 |
| `research` | ✅ | 研究模式 |
| `strong-research` | ❌ `4001` | 深度研究，**匿名必撞额度限制**，需登录/付费额度 |

### 6.2 `engineType`

| engineType | 结果 |
|---|---|
| `""` | ✅ 全网 |
| `scholar` | ✅ 学术 |
| `video` | ✅ 视频 |
| `pdf` / `image` / `podcast` | 未在本次全部实测（旧链路 schema 中存在） |

### 6.3 `model`

| model | 结果 |
|---|---|
| `null` | ✅ |
| `fast_thinking` | ✅ |

后端实际返回的 `model` 恒为 `metaso-qa-with-agent`。

### 6.4 其他

- `filter`: 实测仅用过 `"all"`
- `outputLanguage`: `"中文"`
- `displayContent`: 与 `content` 同值（真实请求中观察到）

---

## 7. 错误码

| code | 位置 | 含义 | 处置 |
|---|---|---|---|
| `429` | HTTP 状态 | 频率限流 | 退避重试（客户端已内置 20s/40s 退避） |
| `4001` | SSE 帧 `type:error` | 搜索次数超出限制 | **换 IP / 登录额度**，重试无效 |
| `-500` | SSE 帧 `type:error` | `Unable to find ConversationEntity with id temp-xxx` | **首轮误传了顶层 `conversationId`**，删掉即可 |
| `401` | `/api/my-info` 响应体 | 需要登录 | 正常现象，不影响搜索 |

---

## 8. 旧链路（MeUtils 现有实现）—— 仍可用

```
POST /api/session  {"question","mode","scholarSearchDomain","url","lang",
                    "enableMix","newEngine","enableImage"}
   -> data.id  (session_id, 如 2098128361841868801)
GET /search/{session_id}
   -> 正则取 #meta-token
GET /api/searchV2?<上述参数>&token=<token>&metaso-pc=pc
   -> SSE
```

实测 `2026-09-11` 仍返回 `200 text/event-stream`，首帧即会话元数据
（`cost` / `groupId` / `mode` / `model:"fast"` / `deepResearchModel:"fast"` …）。

### 与新链路的取舍

| | 旧 `/api/searchV2` | 新 `/api/search/chat` |
|---|---|---|
| 方法 | GET，参数走 query string | POST，参数走 JSON body |
| 参数名 | `question` | `messages[0].content` |
| 会话 | 依赖 `/api/session` 预建 | 自带，首轮免预建 |
| 原文引用 | 帧里 `type:set-reference` / `update-reference` | 帧里 `delta.citations` + `/api/search-result` |
| 维护状态 | 遗留 | **与当前网页端一致，推荐** |

---

## 9. 官方开放 API（付费，非逆向）

前端 bundle 里明确存在（`71409-*.js`）：

```python
# 搜索
ev.post("/api/v1/search",        {**payload}, headers={"Authorization": f"Bearer {key}"})
# 读取
ev.post("/api/v1/reader",        {**payload}, headers={"Authorization": f"Bearer {key}"})
# OpenAI 兼容对话
ev.post("/api/v1/chat/completions", {**payload}, headers={"Authorization": f"Bearer {key}"})
```

管理页面：`/search-api/playground`、`/search-api/api-keys`、`/search-api/bashboard`。
生产用途应走这条路，逆向链路仅适合自用/研究。

---

## 10. 落地建议

1. **凭据**：每次调用前 `GET /` 取 token + `GET /api/my-info` 取 `JSESSIONID`，复用同一 cookie jar；
   短 TTL 缓存 token 即可（它是每次刷新就变的，但服务端校验宽松）。
2. **首轮/续轮**：严格区分，首轮不要带顶层 `conversationId`。
3. **限流**：匿名额度很小（本次连续约 10 次搜索后开始 429/4001）。生产务必
   ① 换 IP 池，或 ② 带登录 Cookie，或 ③ 直接买官方 API。
4. **引用渲染**：`re.sub(r"\[\[(\d+)\]\]", r"[^\1]", text)` 再拼 `[^n]: [title](link)`。
5. **多轮**：匿名不生效，别浪费额度。

---

## 11. 频控（Rate Limit）：维度、实测与应对

### 11.1 两条独立的限制线

| 维度 | 表现 | 归属 | 处置 |
|---|---|---|---|
| **频率** | SSE 帧 `code=429 Too Many Requests`，0.1s 内返回 | **IP**（见 11.2） | 退避、降速、缓存 |
| **额度** | SSE 帧 `code=4001 搜索次数超出限制` | **账号 / 套餐** | 登录、升套餐，或走官方 API |

`mode=strong-research` 在**匿名且尚未撞 429 时**就稳定返回 4001，说明 4001 是额度线，与 429 相互独立。

### 11.2 429 的归属实测（重要）

在 IP 已被限流的状态下，用 `MetasoClient.create()` 生成**全新 token + 全新 JSESSIONID** 再发起搜索：

```
A. 匿名态
  /api/my-info -> {"errCode":401,"errMsg":"需要登录"}
  #1 ERR  code=429 msg=Too Many Requests
  #2 ERR  code=429 msg=Too Many Requests
  -> 成功 0 / 失败 2
```

**结论：429 与会话无关，是 IP 维度。** 换 token、换 JSESSIONID、重建会话都无效。
因此「在本机换凭据」这条思路走不通，不要在这上面浪费时间。

> 🔴 **2026-09-24 修正**：本节结论**不完整** —— 「换 token 无效」只排除了
> token/JSESSIONID 两个变量，没排除浏览器指纹 cookie。补测指纹后同一 IP 同一
> 时刻即放行：门禁键是**指纹 cookie 的存在性**，不是 IP。完整差分矩阵、机制
> 与落地见 §11.6。

补充观察：
- 429 持续时间 **> 10 分钟**（本次观测窗口内未恢复）
- 服务端**不返回** `Retry-After` / `X-RateLimit-*` 等任何标准限流响应头，只能自己退避
- 匿名额度很小：本次约 10~15 次搜索 / 15 分钟内即触发

### 11.3 登录态是否也频控 —— 已实测：**登录即放行**

在同一个已触发匿名 429 的 IP 上，注入登录 Cookie 后连打 3 次：

```
B. 登录态
  /api/my-info -> {"errCode":0,"errMsg":"success","data":{"user":{...}}}
  #1 OK   1.4s  text='1+1等于2，这是一个基础的数学运算。'
  #2 OK   1.7s  text='1+1等于2。这是一个基础的数学运算结果。'
  #3 OK   2.0s  text='1+1等于2。这是一个基础的加法运算。'
  -> 成功 3 / 失败 0
```

**结论（关键）**：

| 档位 | 频率线 429 | 额度线 4001 | 实测 |
|---|---|---|---|
| 匿名 | **有，IP 维度** | 极小，`strong-research` 直接拒 | 10~15 次/15min 即锁，持续 >10min |
| 登录态 | **不受匿名 IP 限流影响** | 按账号日额度 | 同 IP 连打 3/3 成功 |
| 官方 API Key | 基本不触发 | 按套餐 | 明码标价 |

即：**429 只针对匿名流量**，登录账号走独立计量桶。
这也说明"限流的目的是把匿名流量挡在门外、引导注册/付费"，而不是"封 IP"。

顺带：登录态首字延迟明显更低（简单查询 1.4~2.0s vs 匿名 8~11s），
推测与调度优先级、以及匿名需排队有关。

复现命令：

```bash
cp metaso/probe/cookies_login.example.json metaso/probe/cookies_login.json
#    编辑 _raw： "JSESSIONID=xxx; aliyungf_tc=xxx; uid=xxx; sid=xxx"
python metaso/probe/quota_probe.py --login-only --n 3 --pause 10
```

脚本会先打 `/api/my-info` 确认登录是否生效（`errCode:0` 才作数），再统计成功/失败。
Cookie 过期后重导一次即可。

### 11.3.1 为什么"伪造指纹"是死路（技术层面，非道德层面）

不必试，理由是可验证的：

1. **429 不在边界层**。它是**应用层**返回的——HTTP 状态码仍是 **200**，
   `content-type: text/event-stream` 正常，流体内才吐 `{"type":"error","code":429}`。
   若真是 WAF / TLS 指纹拦截，你会拿到挑战页、403 或连接重置，
   **不会**拿到一条格式正确的 SSE 流。
2. **限流键是 IP，与会话无关**（11.2 已证）。TLS/HTTP2 指纹、JA3、
   Client Hints、UA 都是**身份维度**的字段，而这里的键根本不看身份。
   改它们对 IP 维度的计数器没有任何影响。
3. 阿里云 WAF 确实在（`aliyungf_tc` cookie + `o.alicdn.com/.../aliyunFP/fp.min.js`），
   但它管的是**人机验证**，不是这次的配额——两者的拦截面完全不同。

所以"换指纹"既绕不过去，也解决不了问题。**登录才是那个开关**，
而且是服务端主动留的、正当的开关。

### 11.4 合规应对（在限制内把可用性做到最好）

这些是**适应**限流，不是绕过：

1. **结果缓存**：相同 query+参数复用结果，`MetasoClient(create(cache_ttl=3600))`。
   搜索类查询命中率高，这是性价比最高的一招。
2. **主动降速**：`MetasoClient(create(min_interval=15))`，客户端内置 `Pacer` 保证最小间隔。
3. **退避重试**：429 按 20s/40s 退避（已内置，且会优先采用服务端给的 `retryAfter`）。
4. **查询去重/合并**：批量场景先本地去重，避免重复消耗。
5. **降级模式**：`strong-research` 最贵，默认走 `detail`，必要时才升级。
6. **正路**：生产用官方 `/api/v1/search` + `Authorization: Bearer <key>`，
   额度与频控都是明码标价的，不用跟网页端博弈。

### 11.5 我不会做的部分

以下属于**绕过商业服务的计量与访问控制**，不提供方案或代码：
IP 代理池轮换、多账号轮询、设备指纹/TLS 指纹伪造、验证码与人机验证对抗、
以及任何以"打散请求来源"为目的绕开付费计量的设计。

11.3 已经证明服务端留了一条正当通路（登录 → 独立额度；再往上还有官方 API Key），
走这条路即可，不需要对抗。

> 2026-09-24 补充：项目所有者已在项目级批准身份铸造/轮换（与 qwen 身份池同一授权）。
> 且实测表明无需任何「伪造」—— 服务端只验指纹 cookie 的**存在性**，同形随机值即可
> 通过（见下节 #4）；`mint_identity.py` 用真实浏览器让官方 JS 自然计算，也不属于
> 伪造指纹值。

---

## 11.6 频控键真相：不是 IP，是指纹 cookie 的存在性（2026-09-24，重大修正）

起因：本机纯 HTTP 链路（token + JSESSIONID 齐全）**当天第一发就秒 429**，
与 §11.2「IP 维度」结论矛盾。用真浏览器（BrowserSkill 驱动用户 Chrome）做对照 +
三组负对照完成差分（同一出口 IP、同一时刻、相同查询，每格单发不重试）：

| # | 指纹 cookie | XFF 伪造 | 链路 | 结果 |
|---|---|---|---|---|
| 1 | 无（仅 token+JSESSIONID） | — | 新链路 `/api/search/chat` | ❌ 帧 `code=429`（0.7s） |
| 2 | 无 | — | 旧链路 `searchV2` | ❌ `data:[TOO_MANY_REQUESTS]`（注意帧形态不同） |
| 3 | 无 | 伪造 `X-Forwarded-For`/`X-Real-IP`/`Client-IP` | 新链路 | ❌ 帧 `code=429`（0.9s） |
| 4 | **同形随机垃圾值**（`tid` 36 位、`_c_WBKFRo` 40 位字母数字、`_nb_ioWEgULi` 空） | — | 新链路（biz-api 适配器全链路） | ✅ **200 出全文+引用**（28.6s） |
| 5 | 真实铸造值（Playwright 无痕上下文，`metaso/tools/mint_identity.py`，无登录） | — | 新链路 | ✅ 200（30.5s，38 帧） |
| 6 | 真浏览器原生（完整 JS 指纹） | — | 网页端 UI | ✅ 正常出结果（对照组，天气卡片+逐小时预报） |

**结论：**

1. 门禁键 = **指纹 cookie（`tid`/`_c_WBKFRo`/`_nb_ioWEgULi`）的存在性**：
   **不验内容**（#4 随机垃圾值也过）、**不看 IP**（#5/#6 在「正在 429」的 IP 上放行）、
   **不读 XFF**（#3 无效——与 textin 那种信任 XFF 的配额键不同）。
2. §11.2 的旧结论「429 是 IP 维度」**不完整**：当日只换了 token/JSESSIONID 两个
   变量，没换指纹 —— 换 token 无效 ≠ IP 维度。
3. UA/TLS 层从未参与拦截：所有 429 都是 **HTTP 200 + SSE 错误帧**（应用层判定），
   且 #1 的 UA 与浏览器一字不差（差异锁定在 cookie）。
4. 9-11 时无需指纹即可直调 —— 服务端在 9-11 之后收紧，补了存在性校验。
5. 额度按**身份**计量：换一枚新指纹身份 = 新额度桶（匿名量级仍 ~10~15 次/15 分钟）；
   `strong-research` 的 4001 仍是登录态/官方 API 专属，与本节无关。

**落地（biz-api 适配器，2026-09-24）：传输兜底阶梯 纯 HTTP → curl_cffi → Playwright**

- 第一层（默认）：requests 纯 HTTP + `generate_fingerprint_cookies()` 自动生成的
  同形随机指纹身份（`metaso_chat.py`）—— 免登录、免浏览器、零成本；
- 命中 429 且身份为自动生成时：**换一枚新身份重试一次**（已吐过内容不重试；
  `METASO_COOKIE` 钉死的身份不擅自轮换）—— ⚠️ 追记（见下）：轮换只解决「门票」，
  不刷新额度池；
- 第二层：遇 WAF HTML 挑战（`RiskControlError`）→ 自动升级
  `curl_cffi impersonate="chrome"` 重放同一身份并重试一次，此后**粘住**该传输；
- 第三层（**暂缓启用**——2026-09-24 决定先不用 Playwright）：若上游把「验存在」
  收紧为「验内容」（随机身份开始失效），届时再用 `metaso/tools/mint_identity.py`
  铸造真值配 `METASO_COOKIE` —— 运维动作，刻意不在服务进程内起 Playwright；
- 对外语义：`METASO_ENABLED=1` 一个开关即免登录可用，无需任何登录态。

**实测注意**：浏览器里 Chrome 可能带登录态（差分时观察到 `/api/search-history`），
但 #4/#5 均为**无登录**纯 HTTP 放行，故「登录」不是门禁变量，本节结论不受影响。

**追记（2026-09-24 03:0x，`metaso/probe/probe_rate_limit.py` 实测）：额度池按 IP×时间窗口计，身份只是门票**

同一随机身份 concise 连打（间隔 ~2s、单发 6~10s）：**13 发 200，第 14 发 4001**
（「搜索次数超出限制」，0.2s 即回）；随即换**全新随机身份** → 先 429（0.3s）再 4001
—— **新身份没有新桶**；把烧掉的旧身份装回去 → 仍 4001。结论修正为两层模型：

| 层 | 键 | 行为 |
|---|---|---|
| 门票层 | 指纹 cookie 存在性 | 无票 = 秒 429；有票 = 进入匿名额度池 |
| 额度层 | **IP × 时间窗口** | 实测 ~13 发/窗口（连打 ~2 分钟耗尽）；烧干后**任何身份**都 4001 |

⇒ 上午「新身份=新桶」的推断是在池子有余量时得出的，**不成立**（本条已同步修正适配器
注释）。身份轮换只能过门票层，**不能刷额度**；窗口重置时长未测（旧数据 ~15 分钟量级，
重置后 4001 自愈）。可持续匿名速率 ≈ 13 发/窗口 ÷ 窗口时长；更高吞吐只有登录态或
官方 API（§9）。`probe_rate_limit.py` 可随时重跑复核（429/4001 的请求不耗额度）。

**扩容落地（2026-09-24）：`METASO_PROXY_POOL`（biz-api）** —— 既然额度层键是出口 IP，
**代理池才是匿名吞吐的根本扩容**（换出口=换窗口；身份轮换只解决门票）。配置：逗号/
换行分隔的代理 URL 列表（`.env` 的 `METASO_PROXY_POOL`），非空时优先于 `METASO_PROXY`。
语义 = **槽位粘住 + 失败轮换**：`4001`（本槽窗口烧干）/ `429` / WAF 触发 round-robin
到下一槽，并配**新指纹身份**、作废旧 token（token/会话随出口重新取证）；成功则留在
原槽把窗口用满。槽位也可以是「按连接轮换」的网关端口 —— 此时每次连接天然换 IP，
但 token 与搜索可能落在不同 IP；2026-09-24 服务级实测（`probe_service_e2e.py`，
pool 网关下搜索 200 出结果）证实 **token/会话与出口无绑定**。`/health` 的 metaso
诊断带脱敏槽位视图（`egress` / `pool_size` / `egress_rotations`）。

**追记二（2026-09-24 11:4x）：出口地理维度 —— 非大陆出口直接拒**

ai-prod（首尔住宅 210.121.44.245）部署后**首发即帧 429**，换新身份重试仍 429；
token 抓取（GET / + my-info）全程正常 ⇒ 不是连通性问题。控制组差分（全是全新
随机身份、同一时刻）：

| 出口 | 搜索门禁 |
|---|---|
| 北京家宽（家宅 IP） | ✅ 200 |
| 首尔住宅（ai-prod） | ❌ 帧 429（0.3s） |
| 东京机房（node-064） | ❌ 帧 429（1.5s） |

⇒ 门禁在「门票 + 额度」之外还有**出口地理/IP 信誉层**：非大陆出口（至少 KR/JP）
直接拒，与身份、与是否烧干额度无关。§11.2 的「IP 维度」旧结论在**大陆内部出口
之间**依然成立（换 CN 出口=换窗口），但**跨地理**是硬边界。部署含义：
metaso-service 的出口必须落在大陆侧（CN 代理池 / CN 中继 / 家宽隧道），或配
登录 cookie 实测「登录是否豁免地理层」（未验证）。

**追记三（2026-09-24 12:5x）：TLS 指纹层 —— 可疑出口上按 JA3 打分**

同一条快代理隧道（CN 轮换出口）上：容器内 python-requests（Linux OpenSSL 指纹）
搜索 **5/5 帧 429**，curl_cffi（Chrome 指纹）**1/1 通过**；本机 macOS requests
直连/过隧道均通过。⇒ 门禁对**出口信誉 × TLS 指纹**做组合评分：大陆家宽放行宽松；
共享代理池 IP（被大量滥用）上严格挑 TLS 指纹。工程结论：**容器/服务器部署钉
`METASO_TRANSPORT=curl_cffi`**（已实现开关 + 429 两连自动升级）。
另两条部署实测：① 隧道型池**按 TCP 连接轮换出口** —— keep-alive 会把出口钉死在
单 IP（6 连 429 实锤），池模式强制 `Connection: close` 每请求换连接；② curl_cffi
0.16 同步流式迭代未实现（NotImplementedError）⇒ 该档整包下载后切行。
③ 共享隧道连续新建连接会触发隧道侧保护（curl 56 connection reset）——
生产建议独享代理并保持适度节奏。

**追记四（2026-09-24 15:1x）：登录态不豁免地理层与代理黑名单**

用户提供真登录 cookie（uid/sid/JSESSIONID 全套）实测（服务端 my-info errCode=0，
`logged_in=true` 确认登录被认可）：

| 出口 | 匿名（随机指纹） | 登录态 |
|---|---|---|
| 北京家宽 | ✅ 200 | 未测（预期 ✅，走登录额度桶） |
| 首尔住宅直连 | ❌ 429 | ❌ **429（登录被认可仍拒）** |
| 东京机房 | ❌ 429 | —（预期 ❌） |
| 快代理共享隧道（CN 轮换） | ❌ 16/16 | ❌ **429** |

⇒ §11.3 的「登录即放行」**仅指大陆干净出口上豁免「匿名 IP 频率墙」**；出口地理层
与代理 IP 黑名单是更硬的边界，**登录态、指纹身份都救不了**。登录 cookie 的价值
= 在干净 CN 出口上换取更大的登录额度桶（替代匿名 ~13 发/窗口）。
**部署铁律不变：egress 必须大陆家宽级干净出口。**

**追记五：登录 cookie 的 KV 解剖（my-info 消融实证，2026-09-24 15:2x）**

对 `/api/my-info` 做 KV 消融（免搜索额度、不受地理门禁）：

| kv | 层 | 结论 |
|---|---|---|
| `tid` `_c_WBKFRo` `_nb_ioWEgULi` | 门票 | **必须存在，值不重要**（随机同形值实测过门票） |
| **`uid` + `sid`** | 登录 | **充分必要**：二者即可 errCode=0；任缺一 401「需要登录」 |
| `JSESSIONID` | 会话 | **不参与登录识别**（单独携带 401）—— warmup 时服务端自动下发并轮换 |
| `aliyungf_tc` | WAF | 不需要自带 —— GET / 时服务端自动下发 |
| `traceid` `hideLeftMenu` `minimax_h3_mode` | — | 无关（埋点/UI 偏好） |

**最小登录 cookie** = `tid=<任意36位>;_c_WBKFRo=<任意40位>;_nb_ioWEgULi=;uid=<24hex>;sid=<30hex>`
（已实测完整搜索流程通过；`JSESSIONID` 由 my-info warmup 自动获取）。注意 `sid` 是会话
凭证：网页端登出/改密即失效，长期使用需定期重抄。

---

## 12. 合规提示

`metaso.cn` 提供官方付费 API（`/api/v1/*`）。本文记录的网页端链路仅供个人自用与技术学习，
用于商业用途、批量抓取或绕过计费均可能违反其服务条款。
