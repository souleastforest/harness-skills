# SDK 与安全边界

需要判断“这次调用实际允许什么、记录什么、证明什么”时读取。命令步骤见
[安装与调用](install-and-run.md)，当前阻塞问答见 [交互引导](onboarding.md)。

## 三个目录，不要混用

| 位置 | 用途 | 不应误认为 |
| --- | --- | --- |
| 安装的 `<skill>/` | 技能正文、脚本和固定隐私补丁 | 当前任务项目或所有项目共享的记忆位置 |
| `<project-root>/.dsh-invoker-profile/` | 项目路径绑定与隔离工具链/cache/tmp | DSH profile 副本、凭据库或聊天归档 |
| 已选 `<dsh_home>/` | DSH 原有 profile、session 和自身持久状态 | 所有文件都由 uv 隔离管理的目录 |

`binding.json` 最小且严格的字段为 `schema_version`、绝对 `home`、`profile` 名；保存
不是批准状态。`runtime.json` 与受管 Python 等是技术资源，和路径绑定分开。prompt、
response、密钥、身份资料、完整 profile 和日志都不复制进项目绑定目录。
Git 忽略不加密数据，也不能阻止同一用户运行的进程访问它。

有效保存的绑定优先于环境变化。损坏 JSON、未知字段、失效 home、自定义 profile
缺失应阻塞修复，不能自动回退到默认候选、迁移旧目录或删除后重建。只有明确修复/
换绑许可后才 `configure --reconfigure --confirmed`。独立的非秘密
`profile-lifecycle.json` 仅记录已初始化的 home/profile；此前已初始化的目录/标记消失时，
不会再当作尚未首次初始化。工作进程使用父进程校验的绑定快照，发现并发换绑就拒绝。
无关且无效的 `DSH_HOME` 候选只作诊断，不覆盖有效保存的绑定。

## Home、profile 与静态发现

没有绑定时，有界发现顺序是用户明确指定绝对路径、非空白 `DSH_HOME`、操作系统
用户主目录的 `.dsh`。`profile` 只接受名称，对应 `<home>/profiles/<name>`；完整目录
只能按该精确结构拆成二者再确认。

通用 DSH 的默认路径规则，不等于 SDK/wheel 自动采用它：调用必须显式传 `dsh_home`。
默认完整 `sdk` 尚不存在时，可标为待获准首次运行初始化；缺失自定义 profile 不自动
创建。`desktop`、`web` 或名称叫 `sdk` 本身都不能证明当前配置适合 SDK 或权限安全。
用户修改的 profile、home 补丁和环境覆盖仍可能改变实际行为。

只读发现只看目录、profile 名及 `package.json` / `cordis.patch.yml` 等标记存在性。
不运行配置/schema 转储或试探式启动，不导入自定义插件；这些动作可能写入或执行
代码。标记存在不证明握手成功，凭据标记存在不证明认证有效。

`status` 的凭据信息是来源存在性：环境 key 是否非空、home `.env` / `.credentials.yaml`
和当前检查工作目录 `.env` 是否存在，且 `verified: false`。不检查秘密内容，也不把
“环境无 key”误报为“所有来源都没有凭据”。其他调用目录的来源需按实际 `--cwd`
理解；环境继承问题由用户在自己的终端处理，见
[安全输入步骤](install-and-run.md#在自己的终端准备凭据)。

## 项目安装的隔离承诺

安装只在明确许可后进行；首次 home/profile 选择先于安装。`setup` 只准备项目工具链，
不配置、初始化或启动 DSH。固定 SDK/runtime `0.1.5rc1`，受管 Python 请求 3.12，实际
补丁版本由工具链记录；这不表示所有传递依赖都锁到不可变版本。

- 已有合适 uv 复用，不 self-update。本机研究曾核对 uv 0.9.20 所需能力，不表示任意
  旧版都可用；能力不足报告并暂停，不偷偷升级。
- 无 uv 时下载校验固定 0.12.24 官方 standalone；校验摘要和归档路径后才解压，不
  执行远程 shell/PowerShell 安装器。
- 新 uv/Python/venv、下载缓存和临时资源限定于项目隔离目录，不修改全局 PATH、
  shell 配置、注册表、系统 pip 或已有 Python 环境。
- uv 使用 `--no-config`；受管 Python 安装不建全局 bin/注册表项，venv 不发现其他
  项目或借用系统 Python，SDK 使用 binary wheel。`exec` 使用绝对 venv Python
  `-I -B`，没有依赖时直接失败，不安装。
- 在首次启动受管 Python **之前**，Bash/PowerShell 原生检查 `python`、`venv` 启动树
  的 link/junction，含可达的内部目录别名；安装后写 ready 记录前也重检。仅允许最终
  目标仍在项目隔离目录内，拒绝逃逸、悬空或循环目标链。正常内部相对链接可用。
  这只是链接包含性检查，不是防止同一用户任意篡改代码/`.pth` 的完整性或沙箱保证。
- 来源不明、不完整或不兼容的工具链不自动覆盖；ready 工具链先验证再复用。首次
  setup 拒绝已占用的保留技术目标，允许 `binding.json` 与无关开发 scratch 共存；
  路径冲突需先检查归属，不扩大写入边界或删除重建。

这些约束不把 DSH 工具变成无副作用：其本地 session 在已选 home，工具对工作区和
网络的行为仍取决于实际 profile、插件及宿主权限。

## 内部批准失败不能靠外层问答补齐

默认完整 `sdk` 保留其审批策略，需批准动作但没有 approval answerer 时失败关闭。
宿主的 AskUserQuestion/普通聊天负责安装、绑定、当前委派范围，**不是 DSH 内部
工具审批桥**。一次外层“同意运行”不代表每个工具动作已批准。

遇到 rejected、cancelled 或 unavailable，报告未完成范围，缩小任务或让用户修复
真实审批集成。不要修改 profile 消除批准，不自动切 `sdk-minimal`、`headless`、CLI
或关闭沙箱重试。`sdk-minimal` 固定更宽的 `danger-full-access`，不是这里默认 `sdk`
的等价修复。提示词“不要用工具”也不是硬性工具禁用。

## 关闭会话贡献，不等于离线或无日志

内置 `assets/no-session-log-upload.patch.yml` 的目标行是：

```yaml
- id: session-log-deepseek
  name: '@deepseek-ai/dsh-session-log-deepseek'
  disabled: true
```

SDK 的 `patches` 是**绝对文件路径的 tuple**，不是 YAML 字符串或 Python 字典。最小
衔接示意如下；变量必须来自已确认绑定及获准任务，不是新的自动执行入口：

```python
with DeepSeekHarness(
    dsh_home=confirmed_absolute_home,
    profile=confirmed_profile_name,
    cwd=approved_absolute_workspace,
    patches=(*other_approved_patch_paths, absolute_privacy_patch_path),
) as harness:
    result = harness.run(approved_prompt)
```

只有一个文件时为 `patches=(absolute_privacy_patch_path,)`。补丁按顺序在 profile/home
层之后应用，内置 opt-out 放最后，并保持到 harness 退出；不修改用户
`cordis.patch.yml`。自定义 profile 的其他贡献插件不因这一行而自动受控，不为“关闭”
而添加新插件或声称覆盖全部通道。

边界如下：

1. 关闭的是这个 runtime 内的 `session-log-deepseek` 额外请求贡献；普通模型输入仍
   发到 provider。不是离线模式，也不是所有网络被禁用。
2. 独立本地 session persistence 仍运行；不删除、匿名化或加密已有/新建日志。
3. 其他入口没有这层补丁、重新启用贡献时，可能上传未接受积压，**包括关闭期间保存
   的事件**；不能承诺这些记录永久不上传。
4. 独立 OpenTelemetry（OTel）、反馈或自定义插件不由这个单一开关保证关闭。

若业务要求严格不出网、永不贡献某些事件或硬性禁用工具，需要额外可验证的运行环境
与策略；不能仅靠此 skill 或 prompt 作保证。

## 调用结果与超时怎么解释

helper `invoke` 使用已确认绝对 home、profile 名和 `--cwd`，通过文件或 stdin 输入
prompt。无论 profile 名称如何，helper 都显式默认 provider `deepseek-official`、model
`deepseek-v4-flash`；选择自定义 profile 不会继承它的 provider/model。其他已选路由须
显式传 `--provider` / `--model`（示例见 [安装与调用](install-and-run.md)），不自动改变
正常 base URL 环境/配置行为。仅实质不明的自定义路由需要补问，默认 `sdk` 不加问卷。
每次 helper 调用始终新建独立 session，返回的 session ID 仅供诊断，不是恢复入口。
固定发行 SDK/runtime `0.1.5rc1` 的 SDK 协议只有
`initialize`、`session/prompt`、`shutdown`；`session/prompt` 不恢复磁盘历史。
因此 helper 对 `--session-id` 在读取 prompt 和启动 SDK 前明确报
`UNSUPPORTED_CONTINUATION`，不会把“相同 ID”冒充历史续接，也不会改走 CLI/ACP/headless。
官方 `DeepSeekHarness` 实例内的多次 `run` 可以同进程续接，但不是本 helper 的命令能力。
只报告 `session_id`、`finish_reason`、`final_response`，默认不转储原始事件、通知、配置或异常文本。

同时检查退出状态和 `finish_reason == 'completed'`。有最终文本但输出耗尽/失败仍
不是完成；`completed` 也只证明 runtime 本轮结束，不证明任务质量或用户验收通过。
依赖、静态配置、握手、真实服务调用、质量检查应分别说明。

SDK `request_timeout_seconds` 限制请求等待，不是整轮 deadline。helper 的
`--timeout-seconds` 从 prompt 获取前开始计时；只接受普通 UTF-8 文件，stdin 的读取等待
也受限，worker 输出在消费时限制到 8 MiB。到期后有有界的强制清理宽限，不是零延迟退出。
SIGINT/SIGTERM/SIGHUP 会经清理路径退出（分别 130/143/129），随后恢复原信号处理器。
Windows 在释放 worker 输入门之前绑定 kill-on-close Job；POSIX 在输入门之前取得创建
身份，再跟踪原组/后代与已观测的其他进程组，清理时核对 PID 与创建身份，不按程序名杀进程。
这能清理正常 DSH shell 后代和已观测的长驻 detached 子进程，**不是内核沙箱**：恶意进程
若在两次采样间立刻 setsid、双重 fork 并重挂父进程，可能逃出便携跟踪；macOS 无 pidfd，
创建身份复检与发送信号之间仍有极小竞态。需要对任意恶意逃逸作强保证时应另用获准的内核隔离。
限时/信号结束不保证服务端未接受请求、不计费或事务已回滚；不要为探测自动重试模型请求。

## 按症状恢复，不扩大权限

| 症状 | 当前行动 | 可观察结果 / 仍未证明 |
| --- | --- | --- |
| 只有全局 CLI，无项目 SDK | 保留 CLI，选择位置后单独确认项目安装 | 项目环境准备好；尚未调用模型 |
| 安装失败/摘要不匹配 | 停止下载或解压；保留安全错误类别 | 没有用不可信资产继续执行；不全局安装 |
| 工具链不完整或来源不明 | 先检查归属和冲突，让用户确认修复范围 | 不自动覆盖其他环境；修复不是运行许可 |
| 绑定失效/损坏 | 保留原信息，明确选择修复或换绑 | 新绑定静态有效；未证明协议/认证 |
| 没发现凭据来源 | 用户在自己的终端隐藏输入，再从该终端启动宿主 | 只能复检存在性；不索要 key |
| 认证拒绝 | 用户在自己的终端更新来源，明确选择是否重试 | 不回显原始服务端错误/密钥；不盲重试 |
| 自定义 profile 握手失败 | 保留原配置，修复后复检或明确改绑 `sdk` | 不加插件、不放宽权限 |
| 内部批准不可用 | 停止受阻动作，缩小任务或修复审批集成 | 不绕过 answerer；外层许可不能替代它 |
| 网络/外层限时 | 说明调用状态不确定，检查连接/端点/范围 | 不能据此断言服务端没收到或自动再发 |

### 平台或安装被阻塞

固定 runtime wheel 的原生范围：macOS 14+ x64/arm64、Linux glibc 2.28+ x64/arm64、
Windows x64。Windows 使用 `pwsh`（入口要求 PowerShell 7.2+ / .NET 6）；uv 本身能在其他平台
安装不代表 DSH wheel 可用。不承诺 Alpine/musl 或 Windows ARM 原生支持。

受执行策略/组织策略阻止时，由用户按其信任流程处理，不使用执行策略绕过。不自动
安装 PowerShell、启用 WSL、源码构建、替换全局 uv 或换 CLI 来规避当前阻塞。

## 验证声明到哪一层

文件和源码核对只证明实现/静态契约。离线 fake SDK 测试、原生平台的本地 mock API
集成、真实 provider 调用，是不同测试；mock 不验证真实账户认证。fixture 访谈评估
更只检验问答顺序和边界，不能当实际安装或模型调用的证据。发布状态以真实测试/CI
结果为准，不在此文预先宣称通过。固定依据见 [来源](sources.md)。
