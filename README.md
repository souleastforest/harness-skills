# Harness Skills

这里保存可以跨项目使用的智能体技能。每项技能独立放在一个顶层目录，入口是其中的
`SKILL.md`，需要时再读取它链接的参考文件。除技能外，本仓库也存放随附的 DSH 插件，见下方「DSH 插件」。

## 选择技能

- [reader-first-technical-docs](reader-first-technical-docs/SKILL.md)：把技术文档整理成读者能理解和实践的教程、概念说明与查询参考。行文方法参考 DSH Cordis 教程。
- [dsh-invoker](dsh-invoker/README.md)：通过 DeepSeek Harness Python SDK 委派任务，逐步引导初始化与调用诊断。当前验证范围为 Linux x64 与 macOS 14 ARM64；Windows PowerShell 实现保留但原生 SDK 启动未验证，详见技能说明。
- [two-way-steelman](two-way-steelman/SKILL.md)：双向钢人化分析。
- [sleep-screenshot-log](sleep-screenshot-log/SKILL.md)：引导首用设置，手动读取睡眠长截图，保留原图并幂等写入个人中枢的睡眠月档。

## DSH 插件

这些不是技能形态（没有 `SKILL.md`），而是可直接安装的 DSH bundle（插件）：

- [maid-atelier-desktop-bridge](maid-atelier-desktop-bridge/README.md)：把 maid-atelier 皮肤的美术层重新挂载到 DSH Desktop 自有窗口布局的对话列，修复窗口模式为 `advanced`/`extended` 时对话区纯黑、宫殿背景与女仆立绘不显示的问题。README 含完整排障记录与复用教训，其中两条对任何 DSH 插件作者都适用：bundle 的 `exports` 必须导出 `./package.json`，以及新装 bundle 需要重启一次 DSH 才会进入 client 模块图。
- [deepseek-web-tools](deepseek-web-tools/README.md)：把 DeepSeek 官方 Search / Fetch 接口接到 DSH 的 `ctx.web`，搜索与抓取走专用端点，失败时回退到原有后端，并可用停用 bundle 一键回滚。

## 下载写作技能

[下载 reader-first-technical-docs v1.0.0](https://github.com/souleastforest/harness-skills/releases/download/reader-first-technical-docs-v1.0.0/reader-first-technical-docs.zip)。
压缩包只包含这项技能的正文和写作参考，解压后得到 `reader-first-technical-docs/`。
将这个目录放进所用智能体支持的技能目录即可；Codex 的用户公共目录通常是
`~/.codex/skills/`。已经存在同名技能时，先比较版本再更新。

若使用 Codex 的 skill-installer，也可提供
[该技能的仓库地址](https://github.com/souleastforest/harness-skills/tree/main/reader-first-technical-docs)
进行安装。阅读与下载都不依赖原作者的本地工作目录。

## 在 Hermes 使用睡眠入档技能

[下载 V1.1 安装包](dist/sleep-screenshot-log-v1.1.zip)，解压到空目录后得到完整的
`sleep-screenshot-log/`，内含 `SKILL.md`、数据合同和初始化参考。此前的
[V1 安装包](dist/sleep-screenshot-log-v1.zip) 保留。
将整个文件夹复制到 Linux 的 `~/.hermes/skills/sleep-screenshot-log/`；已有同名
目录时先比对再更新。Hermes 的技能目录与参考文件机制见[官方说明](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)。

首次入档会集中确认数据目录、时区、截图长期保留或复核天数、可选 ZIP 和 Git。
推荐复用已有 `self`；没有时推荐用户 home 下的 `personal-data/self`。确认前只做
只读探测，确认后将本机配置保存到 `$XDG_CONFIG_HOME/sleep-screenshot-log/config.json`，
未设置 XDG 时用 `$HOME/.config/sleep-screenshot-log/config.json`。以后正常入档
复用设置；只解析预览无需初始化。

也可在启动 Hermes 的终端提供本机实际图谱路径，例如：

```bash
export SLEEP_SELF_ROOT='/absolute/path/to/your/self'
```

再从该终端启动 Hermes，使进程继承变量。这是本技能约定的普通环境变量；也可
直接在本次请求中给出绝对 `self_root`。优先级为显式路径、实际环境变量、本机配置，
高优先级路径无效时不会静默回退；换新 root 会重新确认其策略。若终端沙箱看不到
目录，需提供该运行环境实际可访问的路径。个人配置不进入安装包，也不改 Hermes
原生配置。

默认月档和原图长期保留，关闭归档与删除。选择 7/30/90 天只表示复核或归档候选，
不授权删除；启用 ZIP 后只在手动运行时处理已结束月份，保留散图和证据链接。
同盘 ZIP 不是异地备份。Git 可复用现有仓库或经确认新建，推荐只跟踪结构化月档、
忽略截图与 ZIP，默认手动提交、不推送；本地 Git 不加密。

使用时手动附上同一晚的一张或多张截图，请 Agent 用 `sleep-screenshot-log` 入档。
Agent 优先直接读图，数据写入 `self/health/sleep/YYYY-MM.md`，原图保存在
`self/health/_raw/sleep/YYYY-MM-DD/`，`self/02-life.md` 只维护睡眠月档链接或索引。
此版本不做自动采集；包内没有真实健康记录、截图或个人配置。
