---
name: dsh-invoker
description: 通过官方 DeepSeek Harness Python SDK 委派任务，并以逐步问答完成首次初始化或故障诊断。当用户明确要用 DSH、DeepSeek Harness 执行/审查任务，或提到其安装、home/profile、项目绑定、SDK 调用失败时使用；也支持“带我初始化 DSH”“一步一步诊断”。不因普通 Python 开发、通用 LLM 问题或仅讨论 DeepSeek 模型而触发。
---

# DSH 任务委派

先查明当前状态，再解决一个卡点；确认后才安装、保存或启动。默认通过项目隔离的
Python SDK 使用 `sdk`，不自动换成 CLI 或权限更宽的 `sdk-minimal`。

按 `DISCOVER → WAIT（一个当前问题）→ ACT（已授权的有界动作）→ 重新检查` 推进。
准备就绪不等于允许运行；拒绝、取消、无交互通道或安全检查不明时暂停该分支，给一个
可继续的下一步。当前明确任务请求已授权其陈述范围，不重复问同一许可；新增副作用
另行确认，不能把初始化请求扩张为模型试跑许可。

## 先读哪一页

- 正在初始化、用户不懂 profile、或调用失败：读 [逐分支引导](references/onboarding.md)。
- 要执行命令或处理 PowerShell 路径：读 [安装与调用](references/install-and-run.md)。
- 要判断权限、日志、会话或超时边界：读 [SDK 与安全边界](references/sdk-and-safety.md)。
- 要核对技术依据或方法来源：读 [来源](references/sources.md)。
- 要继续 Windows x64 / PowerShell 原生 SDK 验证：读 [Windows 开发交接](references/windows-handoff.md)；该路径目前未验证且默认 CI 暂缓。

这些页面按需读取。不要用接口清单或整张安装问卷代替引导。

## 1. 只读确定起点

1. 确定任务的项目根目录：优先当前工作区的 Git root；非 Git 项目使用用户指定或
   已明确的工作目录。记录绝对路径。skill 的安装目录不是任务项目。
2. 定位本 skill 的绝对目录，运行下面相应的 `check`。它不要求已有 Python/uv，
   不安装、不建目录、不初始化 DSH。
3. 项目受管 SDK 已就绪时，再经 `exec` 运行 helper 的 `discover` 或 `status`。
   缺依赖时停在 shell 预检，不用系统 Python/pip 或全局 `dsh` 绕过它。
4. 只展示平台/架构、依赖是否存在、绑定的 home/profile、候选名称与标记文件存在性。
   不读取或输出密钥；不运行 `--dump-config`、`--dump-default-config` 或 schema dump。

POSIX Bash（将变量替换为实际绝对路径）：

```bash
SKILL_DIR='/absolute/path/to/dsh-invoker'
PROJECT_ROOT='/absolute/path/to/project'
bash "$SKILL_DIR/scripts/bootstrap.sh" check --project-root "$PROJECT_ROOT"
```

Windows PowerShell：

```powershell
$SkillDir = 'C:\Skills\dsh-invoker'
$ProjectRoot = 'C:\Work\Demo Project'
& "$SkillDir\scripts\bootstrap.ps1" -Action check -ProjectRoot $ProjectRoot
```

只读发现不证明启动成功、远端认证成功或自定义 profile 与 SDK 兼容。

### 选 home 与 profile 的规则

- 已保存的 `.dsh-invoker-profile/binding.json` 优先于本次环境变量。先校验它；不要因
  `DSH_HOME` 改变就覆盖绑定。损坏、失效或需要改绑时，进入修复问答。
- 尚无绑定时：用户明确指定的绝对 home → 非空白 `DSH_HOME` → `Path.home() / '.dsh'`。
  这些只是候选，不是批准。相对路径先解析并展示绝对结果，确认前不保存。
- `profile` 是名称；目录是 `<home>/profiles/<name>`。用户给完整目录时拆成 home 与
  名称再确认，不把整个目录当名称。推荐 `sdk`。
- 没有 `sdk` 时标为“确认运行后可初始化”，不冒充已存在。`desktop`、`web` 或自定义
  名称存在，不代表可以替代 SDK 服务；缺失的自定义 profile 不擅自创建。

## 2. 一轮只推进一个决定

使用宿主实际可用的提问工具，例如 `AskUserQuestion`；不存在时就在普通对话中问，
**结束本轮并等待用户真实回复**。没有回复、自动化环境无法回复、默认选项或 fixture
中的假想答复都不是同意。不要自行续写用户回答。

每轮用简短的“观察 → 推荐 → 一个问题 → 同意后的下一动作”：

> 首次检查发现还没有项目绑定。建议使用候选 home `<绝对路径>` 与 `sdk`，并在本项目
> 只记住路径和名称、补根 Git 忽略；`sdk` 尚不存在时仅标为待初始化。使用这个组合吗？
> 可以改位置或暂停。我会等待答复；这一步不会安装或启动 DSH。

首次 home/profile 选择先于任何依赖安装。绑定失效时也先确认修复/换绑，再问缺项安装。
若受管 Python 尚不可用，先在对话保留已确认选择与保存许可；安装后保存，不重复问位置。
`check` 的 `uv.path` / `runtime.python_path` 是静态存在性证据，不代表 SDK 导入或启动通过。

这只是当前分支的示例，不是每次重问的模板。自行检查能得到的事实不要问用户。

| 当前证据 | 这轮处理 | 下一步的门槛 |
| --- | --- | --- |
| 首次无绑定，无论 SDK 是否可用 | 先展示候选 home + `sdk`，说明保存位置和 Git 忽略 | 明确确认组合；缺 Python 时暂存对话选择，不抢先安装 |
| 已确认位置，uv/受管 Python/SDK 缺失 | 说明下载和项目隔离范围，推荐项目安装 | 用户另行同意安装后才 `setup` |
| `dsh` CLI 存在，项目 SDK 不存在 | 解释 CLI 不等于 SDK 可用，不重装全局 CLI | 先确认未定位置，再按缺项安装；不走 CLI fallback |
| 依赖已就绪，位置已确认但未保存 | 保存先前已确认的选择 | 用 `configure`，不重复问位置 |
| 凭据未就绪 | 引导用户在自己的终端配置；只检查存在性 | 凭据就绪且当前运行获准后再调用 |
| 绑定损坏、目标丢失或要求改绑 | 展示旧值与候选，推荐最小修复 | 确认后 `configure --reconfigure` |
| 自定义 profile 启动不兼容 | 停止；推荐另选内置 `sdk`，保留原配置 | 选择和改绑须重新确认 |
| DSH 内部审批失败 | 如实说明未获批准；缩小任务或停止 | 不放宽权限、不改成 `sdk-minimal` 重试 |
| 有效绑定与环境已就绪 | 复用已定选择，执行已获准的当前委派 | 不重复初始化问卷；新风险才重新确认 |

安装许可、保存绑定许可和任务运行许可是不同范围。可以复用用户已明确给出的对应
许可，但不能从“发现了一个路径”或“想初始化”推导出全部许可。用户拒绝某一步时，
停止该步及其依赖步骤，交代当前未改动什么和以后如何继续。

## 3. 确认后安装并保存最小绑定

安装使用 `bootstrap.sh setup --project-root <abs>` 或 PowerShell
`-Action setup -ProjectRoot <abs>`。它只准备依赖，不运行 DSH、不保存选择。

- 复用现有可用 uv，不升级它；没有 uv 才下载固定 `0.12.24` standalone 并校验摘要。
- 受管 Python 为 3.12，SDK 与 runtime 固定 `0.1.5rc1`。新 uv/Python/venv/cache/tmp
  都在任务项目的 `.dsh-invoker-profile/`；不改全局 PATH、shell 配置、注册表或已有环境，
  不运行全局 pip/self-update。现有 uv 能力不足就报告，不偷偷替换。
- 官方 wheel 发布范围：macOS 14+ x64/arm64、Linux glibc 2.28+ x64/arm64、Windows x64；
  Windows 入口使用 PowerShell 7.2+（`pwsh`）。这不等于本项目的平台验证结论：当前验证范围仅
  Linux x64 与 macOS 14 ARM64。Windows PowerShell 实现和示例保留，但 SDK 启动原生验证未通过，
  Windows CI 按用户要求暂缓；不将 Windows 宣称为已验证的生产支持。不要绕过执行策略；不承诺 Alpine/musl 或原生 Windows ARM。

绑定由 helper 保存，不手写配置。以下命令仅在用户确认 home/profile 和保存之后执行：

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" configure --project-root "$PROJECT_ROOT" \
  --home '/absolute/path/to/dsh-home' --profile sdk --confirmed
```

`--confirmed` 是对已有真实答复的记录，不是制造同意的开关。修复或更改已有绑定还需
`--reconfigure`。PowerShell 对应参数见 [安装与调用](references/install-and-run.md)。

`binding.json` 只包含 `schema_version`、绝对 `home` 和 `profile`；确认保存时 helper
在项目根 `.gitignore` 补充 `/.dsh-invoker-profile/`。同目录的 `runtime.json` 和工具链资源
是另一类技术文件，不是 profile 副本。不要向该目录复制凭据、身份、用户 profile、
prompt 或 response，不把它当会话归档。DSH 自己的 profile/session 留在已选 home。

## 4. 凭据留在用户终端

推荐用户在自己的终端用隐藏输入或既有凭据管理机制设置 `DEEPSEEK_API_KEY`。
不要求粘贴到聊天，不放 argv、绑定、日志或示例实值；不打印环境/凭据文件内容。
凭据文件存在或环境变量已设置只证明候选存在，不证明远端认证有效。

新终端设置的环境变量不会回流到已运行的宿主。必要时请用户从该终端重新启动宿主，
再做仅存在性的检查；不要以索取密钥来“解决”继承问题。安全输入示例见
[安装与调用](references/install-and-run.md#在自己的终端准备凭据)。

## 5. 执行明确的任务并报告

首次启动前说明：DSH 可能在所选 home 创建 `sdk` 和本地 session；任务输入仍会发给
模型服务。默认仅关闭本次 runtime 的 session-log 贡献，不是全面禁网或永久不上传。
其他入口以后重新启用贡献，可能上传此前保存的事件；其他插件/OTel 不受此开关保证。
确认授权覆盖当前工作区、任务与这些影响后才启动。不要顺便运行付费 smoke test。

通过 `bootstrap exec` 调 helper；它使用受管 venv 的绝对 Python 路径并加 `-I -B`，
不隐式安装。始终把 helper 绝对路径作为 Python 的第一个文件参数。

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" invoke --project-root "$PROJECT_ROOT" \
  --cwd "$PROJECT_ROOT" --prompt-file '/absolute/path/to/approved-prompt.txt'
```

- prompt 用 UTF-8 文件或 `--stdin` 输入，不塞进 argv；不要把密钥写进 prompt。
- helper 从绑定取显式绝对 `dsh_home`、profile 名称，并传绝对 `cwd`；不依赖 SDK/wheel
  自动找 home。每次调用都新建独立 session；返回的 ID 仅供诊断。固定 SDK/runtime
  `0.1.5rc1` 不支持跨进程恢复，`--session-id` 会在读取输入和启动 SDK 前报
  `UNSUPPORTED_CONTINUATION`；不要承诺续接，也不改用 CLI 或其他 profile。
- helper 默认显式传 provider `deepseek-official`、model `deepseek-v4-flash`，自定义
  profile 不会继承其路由。用户另选路由时显式加 `--provider/--model`；只在其数据去向
  或凭据选择实质不明时补问，默认 `sdk` 不额外问卷；正常 base URL 环境规则不变。
- 保留 helper 最后一层隐私 patch：`assets/no-session-log-upload.patch.yml`。SDK 的
  `patches` 是绝对文件路径 tuple，不是内联 YAML；不修改用户 profile。
- `AskUserQuestion` 只处理这里的 onboarding，不是 DSH 工具审批桥。内部缺少 approval
  answerer 就失败关闭；不能把外层安装/运行确认当作每个工具动作的批准。
- SDK 的 `request_timeout_seconds` 不是整轮 deadline；只有实际外层进程限时才能作此
  承诺。超时/认证/网络/审批故障分别诊断，不盲目重试或升级权限。

检查退出状态，并报告 `session_id`、`finish_reason`、`final_response`；只有
`finish_reason == 'completed'` 表示 runtime 本轮完成，不代表用户任务已验收、代码正确
或测试通过。区分依赖就绪、静态绑定、runtime 握手、真实服务调用和任务质量。离线/
mock 不证明真实认证；“不要使用工具”的 prompt 也不是硬性工具隔离。

失败说明脱敏的原因类别、已完成范围和一个可继续动作；不要转储原始事件、配置、
凭据或原始异常。没有真实调用就明确“仅检查/已配置，尚未运行”，不编造结果。
