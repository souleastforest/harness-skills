---
description: "把 DeepSeek 官方 Search / Fetch 专用接口接入 DSH 的 ctx.web，并保留旧搜索/抓取后端作为自动回退与一键回滚的载体。"
kind: "local-bundle"
---

# dsh-deepseek-web-tools

把 [DeepSeek 官方 Search / Fetch 接口](https://api-docs.deepseek.com/zh-cn/guides/search/) 接入 DSH 的 `ctx.web` seam，让 `web_search` / `web_fetch` 工具直接走这两个专用检索端点；同时保留原有后端，形成**两级自动回退 + 一条一键回滚路径**。

## 为什么需要它

DSH 已交付的 `@deepseek-ai/dsh-web-search-deepseek` 写在 DeepSeek 还没有独立检索端点之前：它用 Anthropic 兼容的 Messages 接口挂 `web_search` 服务端工具，**一次搜索要消耗一个完整模型轮次**（延迟与生成 token 都按模型请求计），并在抓取上只能依赖匿名本地 HTTP（受 SSRF 防护与反爬限制）。

官方文档现在发布了两个纯 JSON 接口，与对话共用同一套 Bearer 鉴权：

| 能力 | 端点 | 返回 | 实测延迟 |
|---|---|---|---|
| Search | `POST /v1/tools/search` | 结构化结果（标题 / URL / 短摘要 / 长摘要 / 正文 / 发布时间 / 权威度 / 排序分） | ~0.4 s |
| Fetch | `POST /v1/tools/fetch` | 单页 Markdown 正文 | ~0.2 s |

本插件把两者注册为 `ctx.web` 的 `deepseek-tools` 提供方，所以同样的 `web_search` / `web_fetch` 工具调用，从「一次模型轮次」变成「一次普通 HTTP 请求」，且 `web_fetch` 拿到的是干净 Markdown（由服务端抽取），不再依赖本地 HTML→Markdown 转换，也不受目标站点针对本机 IP 的反爬限制。

## 它注册了什么

```
ctx.web.searchProviders["deepseek-tools"]   → POST {baseURL}/v1/tools/search
ctx.web.fetchProviders["deepseek-tools"]    → POST {baseURL}/v1/tools/fetch
```

`cordis.patch.yml` 做两件事：

1. **覆盖 `web` 行的提供方选择**：`searchProvider: deepseek-tools`、`fetchProvider: deepseek-tools`；
2. **插入本插件的 Host 行**（id `web-deepseek-tools`）。

> 关键设计：把 seam 固定到本插件的唯一一份配置就在**组合包自己的 patch 层**里。因此禁用/移除本组合包时，这份覆盖会随之消失，组合自动回到 base 层的 `deepseek-official` + `http`，不需要手工改 profile 文件。

## 回退设计

三层，从内到外：

### 1. 认证级回退（同一次调用内）

会话由 `deepseek-account` 路由时，先用账号票据（`x-dsh-auth-token`，与账号版 LLM 适配器同一套）请求；若该端点返回 `401`，自动降级为 `DEEPSEEK_API_KEY` 再试一次。凭据解析顺序与 shipped 插件一致：字面 `apiKey` → `ctx.credentials`（Models 页写入的密钥）→ 启动环境变量。

### 2. 接口级回退（同一次工具调用内）

主端点失败后，直接把请求交给**同一个能力注册表里**的旧提供方，再返回它的结果；对 agent 完全透明：

| 能力 | 默认回退目标 | 回退后行为 |
|---|---|---|
| search | `deepseek-official`（原有 Messages 适配器） | 退化为一次模型轮次，仍返回结构化来源 |
| fetch | `http`（原有匿名本地抓取） | 返回原始 HTML/文本，由工具渲染 |

触发规则：

| 主路径结果 | 是否重试 | 是否回退 |
|---|---|---|
| HTTP 408 / 429 / 5xx | 是（`maxRetries`，默认 1 次，指数退避 + 抖动） | 重试耗尽后回退 |
| 网络错误（连接被拒/重置） | 是 | 重试耗尽后回退 |
| 本插件超时（`timeoutMs`，默认 15 s） | **否**（已花掉延迟预算） | 是 |
| HTTP 400 / 403 / 404 / 422（结构或策略拒绝） | 否 | 是 |
| HTTP 401 / 402（凭据/余额） | 401 会先做认证级降级 | 是（若旧链路能用另一凭据成功） |
| 调用方取消（工具超时 / 用户停止） | — | **否**，取消必须表现为取消 |
| 响应无法解析 / 抓取无正文 | 否 | 是 |
| 回退目标未注册或不可用 | — | 否，错误里附明确说明 |

回退目标本身也失败时，返回一个同时携带两侧原因的错误，不会隐藏主路径的失败。

### 3. 组合级回滚（一键）

禁用或移除本组合包即可，无需改任何 profile 配置：

```text
plugin_manager({ action: "set_bundle", target: "@local/dsh-deepseek-web-tools", enabled: false })
plugin_manager({ action: "remove_bundle", target: "@local/dsh-deepseek-web-tools" })   // 彻底卸载
```

禁用后：`web` 行恢复 base 的 `deepseek-official` + `http`，搜索/抓取行为与安装前完全一致（组合包自己的 patch 层消失，profile 的 `cordis.patch.yml` 从未被本插件修改）。

## 配置

行 id：`web-deepseek-tools`。所有字段都有默认值，安装即用；需要覆盖时在 profile 的 `cordis.patch.yml` 里加一条按 id 的覆盖即可（**整段 config 会被替换**，只写要改的键 + 依赖的键）。

> 本插件刻意不从 `@deepseek-ai/*` import 任何东西（原因见下节）：因此它没有 schemastery `Config`，Plugins 设置页**不会**显示字段 schema。配置由 `resolveConfig` 在**激活期**校验：未知键、类型错误、越界值都会让这一行激活失败并在插件管理器里显示诊断，而不是静默无效。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `apiKey` | 未设置 | 字面 API 密钥；优先用 `apiKeyEnv`，避免密钥进入配置文件 |
| `apiKeyEnv` | `DEEPSEEK_API_KEY` | 每次调用经 `ctx.credentials`（无该服务时读启动环境）解析的凭据引用 |
| `baseURL` | `https://api.deepseek.com` | Tools API 根地址；缺省回退 `$DEEPSEEK_TOOLS_BASE_URL`；支持带路径前缀的网关地址 |
| `timeoutMs` | `15000` | 单次尝试上限（工具总预算 60 s，留出回退余量） |
| `maxRetries` | `1` | 可重试状态码/网络错误的额外尝试次数（0–10；超时不重试） |
| `searchCount` | `10` | 调用方未传 `maxResults` 时的条数；调用方传值优先（夹在 1–50） |
| `searchNeedContent` | `false` | 是否让接口返回正文 `content`（放不进 `web_search` 输出，仅作摘要兜底用） |
| `searchLocalization` | 未设置 | 透传给 Search 的本地化 IP 提示 |
| `searchFallback` | `true` | 搜索失败时是否走回退提供方 |
| `searchFallbackProvider` | `deepseek-official` | 回退搜索提供方 id |
| `fetchFallback` | `true` | 抓取失败时是否走回退提供方 |
| `fetchFallbackProvider` | `http` | 回退抓取提供方 id |
| `fetchMaxContentChars` | `100000` | 抓取 Markdown 字符上限（工具自身还有一层输出上限） |
| `snippetChars` | `600` | 用长摘要充当 snippet 时的裁剪长度 |

环境变量 `$DEEPSEEK_TOOLS_BASE_URL` 与配置 `baseURL` 等价（配置优先），独立于对话适配器的 `$DEEPSEEK_BASE_URL`。

### 为什么零 import

`install_bundle` 对工作区目录的安装结果是 profile 的 `link:` 依赖，插件文件位于 profile 的 `node_modules` 之外。Desktop 的 profile 包解析器只对**位于 profile 内**的包把 `@deepseek-ai/*` 裸导入映射到安装目录，因此 link 安装的包无法 import Harness 包（用两个探针插件验证过：零 import 的行能激活，加一行 `import '@deepseek-ai/schemastery'` 后同一行 `failed to import`）。本仓库其它 `@local/*` 插件同样保持零 import。

本插件因此自带一个行为等价的本地 `WebError`（`lib/web-error.js`，code 可按机器路由），不依赖 `@deepseek-ai/dsh-web` 的内部路径，也不会因 DSH 升级而失配。

> 运维注意：link 安装的插件代码在激活时被 Node 的 ESM 缓存持有——**改完插件文件后需要重启 DSH** 才会加载新代码；而禁用/启用组合包、改组合包配置都是实时的。

## 观测

每次调用由发起 agent 追加仅用于日志的会话事件（不含密钥）：

| 事件 | 时机 |
|---|---|
| `web/deepseek-tools-request` | 发出请求前，含端点与请求体 |
| `web/deepseek-tools-fallback` | 主路径失败、即将交给回退提供方 |
| `web/deepseek-tools-fallback-skipped` | 配置了回退但目标未注册/不可用 |
| `web/deepseek-tools-account-error` | 账号票据解析失败、已降级到 API key |

## 计费与限速（引自官方文档）

- 按**请求次数**计费：Search / Fetch 各 5 元 / 1000 次成功请求；失败、无结果、超时不计费。
- 账号级 QPS：Search 5、Fetch 5，两者独立。一次 `web_search` 最多并发 4 个查询，靠近限速时 429 由本插件的重试 + 回退兜底。
- 文档注明：在当前用户在 DeepSeek Harness 中通过服务端工具调用访问 Search/Fetch 暂不计费，**2026-11-01 00:00（北京时间）起**按次收费。

## 已知限制

- **`count` 不能超过 50**：工具默认 8 条、本插件默认 10 条，均远低于上限；`request.maxResults` 会被夹到 1–50。
- **`web_search` 的接口不返回「答案」字段**：只有来源列表（标题/URL/摘要/日期），因此工具输出中不会出现 summary answer——这与官方 Search 接口一致。
- **官方 Fetch 的 Markdown 会转义部分字符**：实测 `https://api-docs.deepseek.com/...` 返回的正文含 `https\:\/\/`、`\#\#` 等转义，这是接口自身的 HTML→Markdown 行为，本插件原样交给工具渲染，不做二次清洗。
- **回退目标依赖 base 层仍在挂载**：如果某个部署把 `web-search-deepseek` / `web-fetch-http` 行改掉，回退会返回带说明的错误，而不是静默失败。
- **`fetch` 对目标站点策略敏感**：官方 Fetch 有安全策略（403）与可用性限制（404），这些情况都会按上表回退到本地抓取。
- 回退到 `deepseek-official` 时仍是一次完整模型轮次，慢且贵；回退只保证「可用」，不保证「同样快」。

## 验证记录（本机，2026-10-18）

**单元测试 `node --test` → 42 passed**

- 映射与词汇：来源去重、snippet/summary 裁剪、标题去重、空 `results`、缺失 `results`、空正文、`count` 夹取、`date → publishedAt`。
- 错误分类：408/429/5xx 可重试，400/401/402/403/404/422 不可重试。
- 传输与回退（stub fetch）：重试后成功、重试耗尽回退、回退禁用、回退目标缺失（附说明）、回退也失败（双原因）、账号票据 401 → API key 降级、取消不回退、超时不重试而回退、凭据缺失、`available()`。
- 激活契约：默认值、未知键拒绝、越界/类型拒绝、`apply()` 注册两个 provider、`$DEEPSEEK_TOOLS_BASE_URL`、非法 `baseURL` 报错。

**真实接口冒烟 `node test/live-smoke.mjs`**

- Search：5 条来源 / 410 ms；Fetch：`https://example.com` → 949 字符 Markdown。
- 不可达端点 → 回退成功；`401` → 回退成功。

**安装后在 DSH 内实测（同一会话，A/B 对照）**

| 场景 | 结果 |
|---|---|
| 组合包安装 | `application: applied`，无 warnings；`Config.listConfigs` 可见 `include:web-deepseek-tools` 行 |
| `web_search` | 8 条来源，含短摘要与 RFC3339 `publishedAt`（仅本插件会这样映射） |
| `web_fetch` | 成功抓取 `api-docs.deepseek.com`（本机 fake-IP DNS 下本地抓取会被 SSRF 防护拒绝，即旧行为必失败） |
| 禁用组合包（回滚） | 立即生效：`web_search` 回到旧形态（无 snippet/日期），`web_fetch` 回到 `URL hostname ... resolves to a non-public IP address` |
| 重新启用 | 立即恢复：新形态搜索 + 成功抓取；无需重启 |
| 回退实测（临时 `baseURL: http://127.0.0.1:9`） | 搜索：主端点连接失败并重试 2 次后，经 `deepseek-official` 回退成功返回旧形态结果；抓取：主端点 + `http` 均失败，返回同时携带两侧原因的错误 |
| 临时覆盖清理 | profile `cordis.patch.yml` 已按 SHA-256 校验字节级恢复（`6F57077E…`） |
| 会话事件 | 日志中确认 `7 × web/deepseek-tools-request`、`2 × web/deepseek-tools-fallback`、`2 × web/deepseek-search-llm-request`（旧链路确实运行过） |

**安装约束（探针实验）**

- 两个探针插件确认：link 安装的工作区组合包**无法解析 `@deepseek-ai/*` 裸导入**（零 import 的行能激活；加一行 `@deepseek-ai/schemastery` 后同一行 `failed to import`）。本插件因此保持零 import 并自带本地 `WebError`。

## 源码地图

| 文件 | 职责 |
|---|---|
| `index.js` | Host 插件入口：配置解析/校验、凭据与账号票据解析、注册两个 provider、会话事件（零 `@deepseek-ai/*` import） |
| `lib/providers.js` | 传输与回退：认证级降级、重试、超时、结果映射、回退链（依赖注入，零 import） |
| `lib/core.js` | 纯词汇：请求体构造、响应映射、状态码分类、退避、端点归一化（零 import） |
| `lib/web-error.js` | 本地 `WebError`（与 `@deepseek-ai/dsh-web` 可观测契约一致，避免 link 安装的裸导入问题） |
| `test/core.test.mjs` | 纯函数单测 |
| `test/providers.test.mjs` | provider 单测（stub fetch，覆盖全部回退分支） |
| `test/config.test.mjs` | 配置解析/校验与 `apply()` 激活契约单测 |
| `test/live-smoke.mjs` | 真实接口冒烟（需 `DEEPSEEK_API_KEY`，不计入 `node --test`） |
| `cordis.patch.yml` | 组合包 patch：覆盖 `web` 行 + 插入 Host 行 |
