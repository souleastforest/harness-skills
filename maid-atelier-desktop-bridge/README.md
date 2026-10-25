# maid-atelier-desktop-bridge

一个 **Client-only 的 DSH bundle**：把 [maid-atelier](https://www.npmjs.com/package/@smalltailqwq/dsh-client-ui-skin-maid-atelier)
皮肤的美术层，重新挂载到 **DSH Desktop 自有窗口布局**的对话列里。

> 状态：**已验证可用**（2026-10-13）
> 环境：Windows · dsh `0.2.0-rc.2` · dsh-plugin-desktop `2.0.17` · 皮肤 `0.1.7` · 窗口模式 `advanced`

本文是这次排查与实现的完整记录：**两个互相独立的缺陷**叠在一起，第一个在上游皮肤里，第二个在本人写的 manifest 里；第二个静默得多，也更容易让排查走进死胡同。

---

## 1. 问题与目标

**现象**：皮肤装好、侧栏装饰全部正常，但**对话区是一片纯黑**，宫殿背景和两位女仆立绘都不出现。

**目标**：在不改动皮肤包、不改动 DSH 内核的前提下，让美术层正常显示；同时保留皮肤原生的亮/暗切换、聊天态构图与窄窗重排。

---

## 2. 环境事实

| 项 | 值 |
|---|---|
| dsh 内核 | `0.2.0-rc.2` |
| 皮肤 `skin.json` 声明 | `dshCompatibility: "0.2.0rc2"` ← 完全对上 |
| `dsh-plugin-desktop` | `2.0.17` |
| Profile 窗口模式 | `advanced`（来自 `~/.dsh/profiles/desktop/cordis.patch.yml` 覆盖应用层默认的 `compatibility`） |
| 皮肤资源路由 | `/skin-assets/maid-atelier/*`（皮肤自己的 Host 半提供，工作正常） |

---

## 3. 定位过程

### 3.1 排除资源与路由

31 个随包素材头部全部合法（`RIFF....WEBP`，且 RIFF 长度 == 文件大小 − 8）。侧栏装饰渲染正常，证明路由与相对路径解析都没问题。

> 一个无效测试要记下来：我用 shell 直接请求 `/skin-assets/maid-atelier/...` 得到 **403**，一度以为是路由挂了。其实根路径 `/` 同样 403 —— 该 profile 在 `advanced` + `openBrowser: false` 下会拒绝非 Electron 请求，这是设计内行为。**没有 loopback 令牌时的 403 不是路由证据。**

### 3.2 锁定挂载点缺失

皮肤 `lib/client.js:2379`：

```js
const CONVERSATION_COLUMN_SELECTOR = "[class*='centerCol']";
```

`ensureChatAreaStage()` 拿不到这个元素就 `return false`，**永不插入**舞台。而舞台元素自己承载宫殿背景：

```css
body[data-dsh-maid-atelier] [data-skin-chrome=character-stage]{
  background: var(--maid-palace-art) center bottom / cover no-repeat;
  position:absolute; inset:0;
}
```

两位女仆是它的 `<img data-maid-character>` 子节点；上下花边挂在同一个容器（`ensureChatAreaChrome`）。

`centerCol`（`CZPbNW_centerCol`）只存在于 `@deepseek-ai/dsh-client-ui-layout` 的官方 `AppFrame`。而桌面外壳在 `advanced` / `extended` 模式下由 `dsh-plugin-desktop` 的 `applyAdvancedShell()` 注册**自己的 `root` 槽位**（`DesktopOwnedFrame`），整个替换掉官方 frame，渲染的是：

| 用途 | 元素 |
|---|---|
| 侧栏 | `aside.dshDesktopSidebarSurface > div.dshDesktopUpstreamSidebar.dshDesktop_sidebarCol` |
| 对话区 | `main.dshDesktopConversationSurface` |
| 右栏 | `aside.dshDesktopRightbarSurface` |

**整个桌面客户端 bundle 里搜 `centerCol` 零命中。** 侧栏之所以看起来正常，是 `[class*='sidebarCol']` 意外匹配到了桌面外壳的 `dshDesktop_sidebarCol`。

### 3.3 排除"作者没适配 dsh 0.2"

拉取 npm 上 0.1.6 / 0.1.7 两个 tarball 做 diff —— 所谓"0.2 适配"就是这些：

| 项 | 0.1.6（面向 0.1.7rc2） | 0.1.7（面向 0.2.0rc2） |
|---|---|---|
| `dshCompatibility` | `"0.1.7rc2"` | **`"0.2.0rc2"`** |
| `SIDEBAR_COLUMN_SELECTOR` | `":is([data-pane='sidebar'], [class*='sidebarCol'])"` | `"[class*='sidebarCol']"` |
| `CONVERSATION_COLUMN_SELECTOR` | `":is([data-pane='conversation'], [class*='centerCol'])"` | `"[class*='centerCol']"` |

适配本身是对的：0.2.0-rc.2 的 `@deepseek-ai/dsh-client-ui-layout` 确实导出 `CZPbNW_centerCol` / `CZPbNW_sidebarCol`。被删掉的 `data-pane` 兜底也救不了桌面端 —— 全运行时里 `data-pane` 只出现一次（桌面侧栏包装上的 `data-pane="sidebar"`），**没有任何 `data-pane="conversation"`**，所以 0.1.6 在桌面 `advanced` 下同样黑屏。不是回归，也不是 0.2 适配没做好。

### 3.4 缓存字节比对：区分"没挂上"与"没下到"

把渲染进程 HTTP 缓存的对象大小与 31 个素材逐一比对，得到一份非常干净的指纹：

| 资源 | 状态 | 解释 |
|---|---|---|
| 侧栏装饰 8 个 | 已抓取并显示 | 侧栏元素存在 |
| 三张女仆立绘 | **已抓取但从未显示** | `createCharacterStage()` 对**游离** `<img>` 设了 `src`，浏览器照样发请求；元素始终没进 DOM，所以永不绘制 |
| 宫殿背景 / 上下花边 | **完全没请求** | 它们是"永不入 DOM 的元素"上的 CSS `background-image`；自定义属性里的 `url()` 只有被已渲染的声明消费时才会抓取 |

"图抓了却不显示、宫殿图根本没抓" —— 这是**挂载点缺失**的独有指纹，可以彻底排除 URL、路由、资源损坏。

---

## 4. 两个独立的根因

### 坑 A（上游皮肤）：把一个布局实现细节当成唯一挂载点

皮肤用 CSS-module **哈希类名** `centerCol` 作为唯一挂载目标，没有兜底，也不对"找不到挂载点"做任何可见降级 —— 表现只是黑屏，排障成本极高。

- 已在 `Small-tailqwq/dsh-deep-whale#160` 上报（标题指向 `maid-atelier 0.3.2 Windows 桌面端背景不可见`），说明比 0.1.7 更晚的版本依然存在。
- 正确修法在上游：

  ```js
  const CONVERSATION_COLUMN_SELECTOR =
    "[class*='centerCol'], .dshDesktopConversationSurface";
  ```

### 坑 B（本插件 manifest）：`exports` 里少了 `"./package.json"`

这个坑是本人写 manifest 时留下的，也是整个排查里最难发现的一环。详见 §6。

---

## 5. 方案选择

**为什么是新 bundle，而不是 patch、也不是改 `node_modules`**

- **Loader patch 修不了**：patch 只能按 `id` 覆盖 `config`、改 `disabled`、`insert`/`group`。皮肤的 Host 半只有 `apply(ctx)`、**没有导出 Config**，缺陷在 `lib/client.js` 的运行逻辑里，patch 够不着。patch 只能"投递新车"，不能"修车"。
- **改 `node_modules` 不耐用**：profile 的 `node_modules` 由 pnpm 托管（`.modules.yaml` / `pnpm-lock.yaml` / `.package-map.json`），任何 `dsh plugin add|update` 都会覆盖；皮肤自带的 `skin.build.json`（fingerprint + sourceCommit）也会与实际内容不符。
- **fork 皮肤包**要长期跟上游 rebase，且 dsh-skin 的互斥逻辑在管 `ui-skin-maid-atelier` 行本身。

**为什么用"风格 B"（DOM 垫片）而不是纯 CSS**

纯 CSS 也能画（宫殿本来只是个 CSS 变量），但女仆立绘在皮肤里是游离节点、`querySelector` 够不到，只能重画伪元素并复刻定位规则。选择 DOM 垫片是为了**100% 复用皮肤现成 CSS**（连过渡动画都白送），代价是要处理 React 重挂与重复插入。

**合规提醒**：`cordis-plugin-development` 的 `references/ui-plugin.md` 明确写着 *"Do not read another plugin's DOM, stylesheet, or component source to estimate placement"*。本桥**确实踩了这条**。它成立的前提是：这不是"猜测摆位"，而是专门桥接一个**已知皮肤**的既有契约（用它自己的 `--maid-*` 变量与 `data-*` 钩子）。所以它必须被视为**临时桥**，不能替代上游修复。

---

## 6. 实现

### 6.1 文件

| 文件 | 作用 |
|---|---|
| `package.json` | `dsh.bundle.patch` + `dsh.client`（`platform: web`、`immediately: true`、`inject: []`） |
| `cordis.patch.yml` | `insert: id=maid-atelier-desktop-bridge` |
| `index.js` | Host 半，空 `apply()` —— 资源路由由皮肤自己提供，无需 Host 行为 |
| `client.js` | Client 半，真正干活（约 8.8 KB，零依赖） |

### 6.2 复刻的元素

在 `.dshDesktopConversationSurface` 里按皮肤的原始顺序放置（舞台在前、花边在后）：

| 元素 | 作用 |
|---|---|
| `[data-skin-chrome="character-stage"]` | 宫殿（经 `--maid-palace-art`）+ `left`/`right`/`vision` 三张立绘 |
| `[data-skin-chrome="top-trim"]` | 顶部花边，含 `landing`/`workspace` 两层 |
| `[data-skin-chrome="bottom-trim"]` | 底部花边 + 徽饰（`:after`） |

宫殿、花边与所有 `--maid-*-art` 都从皮肤已经写在 `<body>` 上的自定义属性读回，所以**亮/暗切换与 workspace 状态全部白送**。只有三个女仆 hash 是硬编码的。

### 6.3 两条必需的定位规则

```css
body[data-dsh-maid-atelier] .dshDesktopConversationSurface { position: relative; }
body[data-dsh-maid-atelier] .dshDesktopConversationSurface
  :is([data-phase='hero'], [data-phase='active'], [data-phase='settling']) { position: relative; }
```

第二条不是可选的美化：舞台是 `position:absolute; z-index:0`，**定位元素会绘制在正常流内容之上**，没有这一条，宫殿会盖住输入框。皮肤原本把这两条挂在 `[class*=centerCol]` 上，所以必须补。

### 6.4 守卫与行为镜像

- **自解除**：一旦出现不是本桥创建的 `[data-skin-chrome="character-stage"]`（切到兼容模式、或上游修好后的皮肤），立刻拆掉自己的节点；皮肤关闭时也拆。结构上不可能出现双份女仆。
- **窄窗重排**：镜像皮肤的 `installMaidCharacterFit`（`CROWDED_GAP_PX = 32` / `RESTORE_GAP_PX = 96` → `data-maid-figures-crowded`），保留窄窗口下滑走一位女仆的行为。
- 皮肤自己的 `data-maid-chat-active` / `data-maid-workspace` / `data-maid-conversation-active` 由皮肤设在自己 `<body>` 上，纯 CSS 直接响应。

---

## 7. 坑 B：client 半根本没被加载

### 现象

桥写好了、装上了，但**什么也没发生**。而且：

| 检查 | 结果 |
|---|---|
| `include:maid-atelier-desktop-bridge` 行 | `enabled: true`，**`fiberPhase: "active"`** |
| `list_bundles` | `installed: true, enabled: true` |
| Host 日志 | 我们的包**零错误** |
| 浏览器是否请求过我们的模块 | **从未**（整个渲染进程缓存里搜不到包名，而同一份 combo 里能搜到 `ui-skin-maid-atelier`） |
| 我们的 `client.js` 是否被 Host 读过 | 没有 |

Host 半完全健康，坏的只是 **client 模块图**。

### 排查弯路（记录下来）

1. **先怀疑是竞态**：`@deepseek-ai/dsh-client-modules` 在服务激活时一次性组合模块图（`for (entry of ctx.loader.entries()) dirty.add(...)` → `compose()` → `flush()`），所以推断"新装 bundle 需要重启一次" —— 这也确实是 DSH 自己的约定（皮肤 README：*首次安装后重启一次 DSH，之后切换走配置热重载*）。**但 F5 不够、重启也不够。**
2. **再怀疑是增量扫描漏了**：连重启后的 boot 都没有 `ClientPackageCompositionError`（而 boot 时的 `flush()` 在有失败时**会抛**），说明失败发生在**更早、更静默**的地方。

### 根因

`locatePkgJson()` 在 Loader 没有 `internal.resolveSync` 时（本桌面运行时就是这种情况）走兜底分支：

```js
createRequire(baseUrl).resolve(`${packageName}/package.json`)
```

而我的 `exports` 没有声明 `./package.json`，于是：

```
FAIL @local/maid-atelier-desktop-bridge/package.json
  -> ERR_PACKAGE_PATH_NOT_EXPORTED: Package subpath './package.json' is not defined by "exports"
```

关键在于**这个异常被吞掉了**：

```js
try { ... } catch { return; }     // ← locatePkgJson
```

调用链于是变成：`locatePkgJson` → `undefined` → `resolveMeta()` 返回 `null` → **把"这不是 client 包"的否定结论按 specifier 缓存到下次重启** → `reconcilePackage` 静默跳过 → 模块图里没有我们 → index 里没有对应的 `<script>`/`<link rel=preload>` → 浏览器永远不会请求它。

**三个因素叠加，使它极难被发现**：异常被 `catch {}` 吞掉（零日志）、否定结论被缓存（重启前无法自愈）、Host 侧全绿（`fiberPhase: active`）。

### 修复

```json
"exports": {
  ".": "./index.js",
  "./client": "./client.js",
  "./package.json": "./package.json"     // ← 必需
}
```

改完再重启一次（因为否定结论缓存到重启为止）。这次用 Node 把 `resolveMeta` 的整条路径原样模拟了一遍做验证：

```
exactPackageSpecifier -> @local/maid-atelier-desktop-bridge
locatePkgJson         -> OK ...\maid-atelier-desktop-bridge\package.json
dsh.client            -> {"platform":"web","immediately":true,"inject":[]}
platform web?         -> true
exports['./client']   -> ./client.js
clientPath            -> ...\maid-atelier-desktop-bridge\client.js
client.js exists?     -> true 8822 bytes
```

---

## 8. 验收（实测数据）

修好前 / 后，比对渲染进程 HTTP 缓存：

| 项 | 修好前 | 修好后 |
|---|---|---|
| 皮肤素材缓存命中 | 11 / 31 | **14 / 31** |
| 暗色宫殿 `ae6917bb…` (270544 B) | **从未被请求** | **已命中** |
| 顶部花边 `e49115f3…` (39106 B) | 未请求 | **已命中** |
| 底部徽饰 `24f2fb04…` (45398 B) | 未请求 | **已命中** |
| 本模块进 boot combo | 否 | **是**（`f_0000ea` 43238 B / `f_0000ed` 4600999 B） |

宫殿图从"从未被请求"变为"被请求"，是这次修好最硬的客观判据 —— 因为那个 `url()` 只可能被挂载成功的舞台消费。

---

## 9. 已知限制

- **上游修好后本桥会自动失效**（守卫检测到皮肤自己的舞台就拆掉自己）。这是正确行为，不是缺陷。
- **它踩了"不要读别的插件 DOM"的约定**（§5）。它是临时桥，不作为长期方案。
- **唯一一处未命中**：`47072d2d…`（972 B，底部花边 tile）在皮肤 bundle 里被引用、元素也已挂载（其徽饰 `24f2fb04` 已加载），但浏览器始终没请求这张图。视觉结果经确认正常，此项**未查明原因、判定为不影响观感**，留作观察项。
- 其余未命中素材都属于**当前屏幕用不到的状态**：设置面板 `3d2b126c…`、聊天态输入框缎带 `70152698…`/`3d7e6d87…`/`55f08096…`/`944d281c…`、启动错误页素材等。
- 不恢复：composer 花边与输入框外壳（它们挂在 `[data-composer-card]` 上，桌面 frame **会**渲染该元素，皮肤自己已经挂好了）；`--maid-titlebar-height` / 侧栏宽度规则（与对话列无关）。

---

## 10. 复用教训（写给以后写 DSH bundle 的人）

1. **`exports` 必须包含 `"./package.json"`。** 漏掉它，`dsh-client-modules` 会**静默**判定该包不是 client 包。注意 `cordis-plugin-development` 技能的 `templates/decoration/package.json` 里**没有**这一项 —— 照抄那个模板的 bundle 在本运行时上会丢掉 client 半。皮肤包是导出它的，所以它能正常工作。
2. **"Host 行 `fiberPhase: active`" 不能证明 client 半会加载。** 两者是独立管线。
3. **新装 bundle 要重启一次**；而**否定结论也缓存到重启为止**，所以"改了 manifest 再 F5"不会生效。
4. **可用的排查手段**（都不需要浏览器控制）：
   - 在渲染进程缓存里 grep 包名 → 判断浏览器是否请求过该模块；
   - 缓存对象大小 ↔ 素材大小逐一比对 → 区分"没下到"与"没挂上"；
   - 用 Node 原样模拟 `locatePkgJson` / `resolveMeta` → 直接复现静默失败。
5. **桌面端的 index 注入只有一条通道。** 桌面壳的 `index.html` 从安装包静态 dist 直出，`tapIndex`（函数变换）过不去；唯一通道是 `webserver/index-inject` 推的结构化行，经 IPC 交给页面侧解释器逐行应用。且解释器对两种 script 行处理**不对称**：`script` 是 `createElement + textContent + append`（不会"加载失败"），`script-src` 是 `await loadScript(src)`（失败即 reject）。这一条来自本 profile 里 `dsh-whale-widget` 的源码注释，值得记住。
6. **本机安装形态**：bundle 通过 `dsh.profile.bundles` 注册，`node_modules/@local/maid-atelier-desktop-bridge` 是指向本目录的 Junction，因此改本目录的文件即时生效（但 manifest 变更仍需重启）。

---

## 11. 安装 / 验证 / 卸载

安装（已装）：

```sh
dsh plugin --profile desktop add link:E:/Dev/2026-projects/harness-skills/maid-atelier-desktop-bridge
# 或在本仓库用 plugin_manager install_bundle 指向本目录
```

验证（皮肤启用、窗口为 `advanced` 模式、页面已加载）：

```powershell
$c = "$env:APPDATA\DSH Desktop\Partitions\dsh-desktop-renderer\Cache\Cache_Data"
Get-ChildItem $c -File | Where-Object { $_.Length -eq 270544 } |
  Select-Object Name, Length      # 命中 = 暗色宫殿已被请求 = 背景在画
Get-ChildItem $c -File | Where-Object { $_.Name -like 'f_*' } |
  ForEach-Object { if (Select-String -Path $_.FullName -Pattern 'maid-atelier-desktop-bridge' -List -Quiet) { $_.Name } }
# 命中 = 本模块已进入 boot combo
```

卸载：

```sh
dsh plugin --profile desktop remove @local/maid-atelier-desktop-bridge
```

回滚只影响这一个包：不改皮肤包、不改 profile patch。

---

## 附：证据来源

本次排查全部结论都可由只读检查复现：

| 结论 | 来源 |
|---|---|
| 挂载点缺失 | 皮肤 `lib/client.js:2379`、`:2546`、`:3031`；皮肤内联样式表 |
| 桌面 frame 替换 root | `resources/app/lib/client.js:12030-12082`、`:15321`；`DesktopOwnedFrame` `:11700-11800` |
| 黑色从何而来 | `.dshDesktopConversationSurface{background:var(--dsw-alias-bg-base)}` + `body{background:transparent!important}` + `windowsMaterial: off` 的实心窗口底色 |
| 0.2 适配无问题 | npm tarball `0.1.6` vs `0.1.7` diff（`skin.json` + `lib/client.js`） |
| 静默失败 | `dsh-client-modules/lib/index.js` `exactPackageSpecifier` `:82`、`locatePkgJson` `:743-773`、`resolveMeta` `:701-731`、`processOne` `:833`、`reconcilePackage` `:871`、`flush` `:893`、`compose` `:649` |
| 桌面注入通道 | `dsh-host-webserver/lib/index.js:34-40`；`dsh-whale-widget/lib/index.js:706-744` |
| 验收数据 | 渲染进程 HTTP 缓存字节比对 |
