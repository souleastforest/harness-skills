# 用 DSH 完成一次任务

这个 skill 让支持 `SKILL.md` 的智能体通过官方 **DeepSeek Harness（DSH）Python SDK**
委派任务。第一次使用时，它会先检查环境，再带你解决当前一个卡点；不会先抛出整张
安装问卷，也不会把“发现了配置”当作你已批准安装或运行。

你需要一个明确的任务项目和可用的 DeepSeek 凭据。**不必先装 Node 版 DSH CLI**；
SDK 自带匹配的原生 runtime。默认使用保守的 `sdk`，不会为了跑通而改成
`danger-full-access` 的 `sdk-minimal`。

## 从你现在的状态开始

- **还没装过 DSH**：从[第一次初始化](#第一次初始化先检查再决定)开始。
- **`dsh` 命令有了，但委派失败**：看[已有安装的诊断](#有-dsh-命令却调用失败)。
- **已经能用，想换项目或委派新任务**：看[复用绑定与独立任务](#以后怎样复用)。
- **在 Windows 上使用**：看 [PowerShell 入口](#windows-与含空格路径)；原生 SDK 验证交接见[Windows runbook](references/windows-handoff.md)。
- **想知道保存或发送了什么**：看[数据与权限边界](#哪些数据会保存或发送)。

## 第一次初始化：先检查，再决定

### 1. 让宿主找到 skill

把完整的 `dsh-invoker/` 目录放到所用宿主支持的技能目录，例如 Codex 的用户技能目录
`~/.codex/skills/`。若已有同名目录，先比较再替换。保留 `scripts/`、`assets/` 和
`references/`，不要只复制 `SKILL.md`；安装 skill 文件不等于安装 DSH 软件。

在真正要处理的项目里对智能体说：

> 我想用 DSH 审查这个项目。先带我初始化，安装和发请求之前问我。

**你应看到**：当前任务项目的绝对路径、平台、已有依赖和绑定的只读检查，然后只有
一个关于当前卡点的问题。这个过程不应要求你提供密钥，也不应从 skill 安装目录创建
项目环境。若宿主没有识别 skill，可明确给出 `dsh-invoker/SKILL.md` 的路径让它读取。

### 2. 先选择 DSH 的数据位置

DSH 的数据根目录叫 **home**；它下面可以有多个具名配置，叫 **profile**。例如：

```text
/absolute/path/to/dsh-home/              ← home
└── profiles/
    └── sdk/                            ← profile 名称是 sdk，不是整条路径
```

智能体会优先复用项目已确认的绑定；首次使用才把非空 `DSH_HOME` 或用户主目录的
`.dsh` 作为候选展示给你。推荐 `sdk`。若这个内置 profile 还不存在，会明确说明它可在
你批准首次启动后创建；只发现 `desktop` 或 `web` 不算 SDK 已就绪。

**你应看到**：在任何依赖安装前，一个关于绝对 home + profile 名称的选择问题，连同
最小保存和根 Git 忽略影响。位置不对时可以改选或取消；候选事实不是批准。

若尚无受管 Python，智能体先在本轮记住你已确认的选择，不能为了保存绑定抢先安装。
选择、安装、保存和启动各有对应范围；这次位置确认不授权模型调用。

### 3. 位置选定后，决定是否安装缺少的依赖

项目还没有受管 SDK 时，智能体会解释范围：复用现有可用 uv；缺少 uv 才下载校验过的
固定 `0.12.24` standalone；再准备 Python 3.12、SDK 和 runtime `0.1.5rc1`。所有新工具链、
缓存和临时文件都在**任务项目**的 `.dsh-invoker-profile/`，不改全局 PATH、shell 配置、
注册表或已有 Python 环境，也不升级你的 uv。

**同意后的结果**：项目具备运行 helper 的受管解释器；安装本身没有运行 DSH 或发出
模型请求。随后 helper 按此前已确认的保存许可写入 `binding.json`，只记绝对 home、
profile 名称和格式版本；Git 项目根 `.gitignore` 补 `/.dsh-invoker-profile/`。不重复询问
已经确定的位置，不复制你的 profile、身份或凭据。

**如果拒绝安装**：此分支在这里结束；未创建新环境时也不抢先保存绑定。以后可以再说
“继续 DSH 初始化”。下载失败或平台不支持时应得到可读的原因，而不是全局重装建议。
平台与手动入口见 [安装与调用](references/install-and-run.md)。

**如果绑定待修复**：展示旧绑定的问题和新选择，确认后才换绑；不删除重建或自动退回
`DSH_HOME`。有效绑定在依赖已就绪时可直接保存/复用，不必重做安装。

### 4. 在自己的终端准备凭据，再授权任务

不要把 key 发到聊天、命令参数或问题文件里。用你自己的终端通过隐藏输入或已有
凭据管理方式设置 `DEEPSEEK_API_KEY`；[安全输入示例](references/install-and-run.md#在自己的终端准备凭据)
不会将实值写入命令历史。

**你应看到**：检查只说凭据来源是否存在，不显示实值。若是从另一个终端设置环境
变量，已经运行的宿主不会自动得到它；请从该终端重新启动宿主再检查。

准备好后，你可以说：

> 用已确认的 sdk 绑定审查这个项目，只给出建议，不修改文件。允许发送完成该任务所需的输入。

**完成的证据**：返回 session ID、`finish_reason` 和最终响应。`completed` 只表示
runtime 本轮完成，不证明任务已验收、代码正确或测试通过；“SDK 已安装”“绑定已保存”
也不是模型调用证明。依赖、静态配置、握手、真实服务调用和任务质量分别报告。
凭据来源存在不证明认证成功；认证失败后在自己的终端更新，再明确决定是否重试。

## 有 `dsh` 命令，却调用失败

对智能体说：

> 帮我一步一步诊断 DSH；不要重装全局软件，也不要让我把 key 贴出来。

它应先做只读检查，然后针对当前证据选择一个分支：

| 你遇到的症状 | 先做什么 | 应得到什么 / 如何恢复 |
| --- | --- | --- |
| CLI 存在，项目 SDK 缺失 | 检查项目受管环境，不卸载 CLI | 解释两者不同；确认后仅安装项目 SDK |
| SDK 有了，但不知道选哪个 home | 展示候选与标记文件的存在性 | 推荐 `sdk`，确认后保存；不要求你先懂配置结构 |
| 绑定路径丢失或 JSON 损坏 | 指出旧绑定失效，保留原信息 | 提议最小修复，确认后重新绑定；不因环境变化自动替换 |
| 没有凭据 / 认证失败 | 区分“没发现来源”和“远端拒绝” | 引导你在自己的终端处理；不读取 key |
| 自定义 profile 启动失败 | 不把目录存在当兼容性证明 | 建议另选 `sdk`；你确认前不改原 profile |
| 工具动作需要内部审批 | 停止该动作并说明限制 | 缩小任务或使用真正有审批桥的入口；不扩大权限 |
| 网络错误或超时 | 报告脱敏类别与未完成范围 | 先排连接/端点或任务范围，不盲目重复付费请求 |

没有 `AskUserQuestion` 一类专用提问工具也没关系：智能体应在聊天里问一个问题，等你
真实回复后再继续。外层问答只负责初始化和任务授权，**不是 DSH 内部工具审批桥**。

## 以后怎样复用

在同一项目明确委派新任务即可，例如“用 DSH 看一下这次改动”。有效绑定会跨会话复用，
不会每次重问 home、`sdk` 或默认隐私策略。后来改了 `DSH_HOME`，也不会覆盖已保存的选择。

想换位置时说“把此项目的 DSH 绑定改到这个 home”，给出路径；确认前不重写绑定。
新项目有自己的绑定和工具链，不能把 skill 安装目录的配置当成所有项目的默认记忆。

每次调用都创建独立新 session，不继承上次任务的历史。返回的 session ID 仅供诊断，
不是恢复入口：固定 SDK/runtime `0.1.5rc1` 不支持跨进程恢复；`--session-id` 会在读取
输入和启动 SDK 前报 `UNSUPPORTED_CONTINUATION`，不会发送第二次请求或改走 CLI。

## Windows 与含空格路径

使用 Windows x64 与 PowerShell，保持路径为绝对路径并正确引用。例如只读检查：

```powershell
$SkillDir = 'C:\Agent Skills\dsh-invoker'
$ProjectRoot = 'C:\Work\Demo Project'
& "$SkillDir\scripts\bootstrap.ps1" -Action check -ProjectRoot $ProjectRoot
```

**预期结果**：即使没有 Python/uv，也能获得路径和依赖的只读检查。之后的 `setup`、
`configure`、`invoke` 都有单独的确认边界；带空格的 home 不能被拆成多个参数。

若脚本被执行策略阻止，请遵循组织的脚本信任流程或在自己的终端处理；不要使用
`ExecutionPolicy Bypass`。完整 PowerShell 示例见[安装与调用](references/install-and-run.md)。

固定版本的官方 runtime wheel 发布范围为 macOS 14+ x64/arm64、Linux glibc 2.28+ x64/arm64
和 Windows x64；这不是本项目的平台验证结论。当前验证范围仅为 Linux x64 与 macOS 14 ARM64。
Windows PowerShell 实现和示例保留，但 SDK 启动原生验证未通过，Windows CI 按用户要求暂缓；
不将 Windows 宣称为已验证的生产支持。不承诺 Alpine/musl 或原生 Windows ARM；出现不兼容时停止，
不要自动降级到另一种未经确认的入口。准备在 Windows 环境继续验证时，按
[Windows 开发交接](references/windows-handoff.md) 使用合成项目和 loopback 测试；不要使用真实 home/key。

## 哪些数据会保存或发送

- **项目绑定**只保存 home、profile 和格式版本；同目录还存项目工具链/cache/tmp 等
  技术资源及非秘密的 `profile-lifecycle.json` 初始化标记，不是 DSH profile 或聊天记录
  的副本。不要将 prompt/response 存入绑定目录。
- **DSH home** 可以创建 profile 与本地 session。关闭日志贡献不会删除或匿名化这些
  本地数据，也不会阻止正常任务输入发给模型服务。
- **默认隐私 patch** 只关闭本次 SDK runtime 的 `session-log-deepseek` 贡献插件，放在
  最后一层，不修改用户 profile。其他入口若重新开启该插件，可能上传此前保存的事件。
  这不是永久不上传承诺，也不关闭所有网络、OTel 或自定义插件。
- **工具权限**继续由选定 profile 和运行环境决定。默认 `sdk` 的内部审批没有 answerer
  时会失败；skill 不用 `sdk-minimal` 或 CLI 绕过它。

涉及敏感资料时，先确定哪些输入允许发送、是否使用隔离工作区，以及如何管理本地
session；不要把“默认关闭日志贡献”理解成“适合发送任何秘密”。更精确的说明见
[SDK 与安全边界](references/sdk-and-safety.md)。

## 方法与验证范围

交互方式借鉴 [Matt Pocock 的 grill-me](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md)
及其当前 grilling 的调查/依赖分支思路；这里刻意改为每轮一个当前卡点，并给出推荐。
这不是声称上游固定只问一个问题。DSH 安装授权、凭据和 profile 流程是独立实现。
人类指南参考本仓库的
[reader-first-technical-docs](../reader-first-technical-docs/SKILL.md)，按行动、可观察结果与
恢复入口组织，没有复制两者全文。

`evals/evals.json` 提供三个 **fixture 模拟访谈**，不是真实安装或模型调用。发布版源码
依据、平台约束和隐私 patch 来源见[来源](references/sources.md)；源码核对不等于本机
E2E、原生 Windows 测试或三端 CI 已通过，运行状态以实际测试/CI 结果为准。
