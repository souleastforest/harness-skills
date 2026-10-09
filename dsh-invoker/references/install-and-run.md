# 安装与调用：只在确认过的范围内执行

需要手动入口或核对 agent 的命令时阅读。下面是 **Bash / PowerShell** 示例，不是让
agent 自动执行的一张清单。按当前状态选择一步；`setup`、`configure` 和 `invoke`
之前分别需要对应的真实用户许可。只读 `check/status` 不构成这些许可。

## 1. 指向任务项目并只读检查

在任何目录执行都可以，但两个变量都要换成实际绝对路径。`SkillDir` 指安装好的
skill，`ProjectRoot` 指要处理的项目；不要把两者混为一谈。

### macOS / Linux：Bash

```bash
SKILL_DIR='/absolute/path/to/dsh-invoker'
PROJECT_ROOT='/absolute/path/to/project'
bash "$SKILL_DIR/scripts/bootstrap.sh" check --project-root "$PROJECT_ROOT"
```

### Windows：PowerShell 7.2+（pwsh）

```powershell
$SkillDir = 'C:\Agent Skills\dsh-invoker'
$ProjectRoot = 'C:\Work\Demo Project'
& "$SkillDir\scripts\bootstrap.ps1" -Action check -ProjectRoot $ProjectRoot
```

**结果**：只读报告平台、路径、已有 uv 和项目环境的存在性；没有 Python/uv 也可以做
这一步。不下载、不建目录、不加载 profile。若路径不是已有目录，先纠正项目选择。

SDK 环境已经就绪时，用 helper 深入检查；仍不启动 DSH：

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" status --project-root "$PROJECT_ROOT"
```

```powershell
& "$SkillDir\scripts\bootstrap.ps1" -Action exec -ProjectRoot $ProjectRoot -PythonArgs @(
  "$SkillDir\scripts\dsh_invoker.py", 'status', '--project-root', $ProjectRoot
)
```

`discover` 可替换 `status` 来发现候选。不要用 `dsh --dump-config`、
`--dump-default-config` 或 schema dump 检查存在性：它们可能初始化文件、加载插件或
泄露配置。只读检查结果不等于兼容性、认证或任务完成证明。

### 首次预检后，先确认位置

先展示候选 home 的绝对路径与 profile 名称，推荐完整 `sdk`，说明最小绑定与 Git
忽略影响并等待真实选择；**这发生在任何依赖安装之前**。不存在的内置 `sdk` 标为
“首次获准运行时可初始化”，不能写成已发现或已经兼容。

若尚无受管 Python，先在本轮对话保留已确认选择，不为保存绑定抢先安装。依赖就绪
后在第 3 步保存它，不重复问同一位置。有效项目绑定直接复用；改绑才重新确认。

## 2. 位置已确认后，单独允许项目 SDK 安装

说明具体缺项、版本及下载范围并取得对应许可，再运行其中一种：

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" setup --project-root "$PROJECT_ROOT"
```

```powershell
& "$SkillDir\scripts\bootstrap.ps1" -Action setup -ProjectRoot $ProjectRoot
```

**结果**：在任务项目的 `.dsh-invoker-profile/` 准备 Python 3.12、venv、固定
`deepseek-harness-sdk==0.1.5rc1` 与 `deepseek-harness-runtime-bin==0.1.5rc1`；不启动
DSH，不保存 home/profile，也不写用户 DSH 配置。

入口复用现有可用 uv；缺少时才下载固定 `0.12.24` 官方 standalone，核对 SHA-256
并校验归档条目后解压。不会执行远程安装脚本、全局 pip、uv 自更新，也不改全局
PATH、shell 配置或 Windows 注册表。新的工具链/cache/tmp 都留在项目隔离目录，
`runtime.json` 记录技术资源，与 `binding.json` 的选择记忆分开。

已有 ready 工具链会先验证再复用；来源不明或不完整的安装不覆盖。首次 setup 只拒绝
已存在的保留技术目标（`bin`、`python`、`python-downloads`、`python-bin`、`tools`、
`tool-bin`、`cache`、`credentials`、`tmp`、`staging`、`venv`、`.bootstrap-owner`、
`runtime.json`、`.bootstrap-lock`）；`binding.json` 和
无关的开发/测试 scratch 可共存，不要求整个隔离目录为空。

实现隔离时，每次 uv 调用使用 `--no-config` 并限定项目缓存/受管 Python 目录；安装
Python 不建全局 bin 链接/注册表项，venv 不借用系统 Python，SDK 使用固定 binary
wheel。不要把这些约束替换成系统 `pip install`、自动升级或临时全局环境。

**恢复**：下载/摘要不匹配就停止；现有 uv 缺必要能力就报告，不擅自升级它。用户可
决定修复现有 uv 后再试。`exec` 不会替你修复缺失依赖，安装必须回到明确许可的
`setup`。不要删除其他项目或已有用户环境来“清理”。

支持边界由 runtime wheel 决定：

| 系统 | 可承诺的目标 |
| --- | --- |
| macOS | 14+，x64 / arm64 |
| Linux | glibc 2.28+，x64 / arm64 |
| Windows | x64，PowerShell 7.2+（`pwsh`，不是 Windows PowerShell 5.1） |

不承诺 Alpine/musl 或原生 Windows ARM。SDK 正常调用不需要系统 Node CLI；管理
外部插件是另一类任务，不属于这里的自动安装流程。Windows 若受脚本执行策略阻止，
交给用户/组织的信任流程处理，不加 `ExecutionPolicy Bypass`。

## 3. 保存此前已确认的 home/profile

复用首次只读阶段的明确选择及保存许可，不因安装完成而重复问位置。若尚未取得
保存许可，只确认这部分影响。下面的 home 都是占位示例，不表示自动获准使用该
位置；`--confirmed` 只在真实确认之后添加。

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" configure --project-root "$PROJECT_ROOT" \
  --home '/absolute/path/to/dsh-home' --profile sdk --confirmed
```

```powershell
$DshHome = 'C:\DSH Homes\Team One'
& "$SkillDir\scripts\bootstrap.ps1" -Action exec -ProjectRoot $ProjectRoot -PythonArgs @(
  "$SkillDir\scripts\dsh_invoker.py", 'configure', '--project-root', $ProjectRoot,
  '--home', $DshHome, '--profile', 'sdk', '--confirmed'
)
```

**结果**：项目保存最小 `binding.json`，并在根 `.gitignore` 增加
`/.dsh-invoker-profile/`。最小结构为：

```json
{
  "schema_version": 1,
  "home": "/absolute/path/to/dsh-home",
  "profile": "sdk"
}
```

home 是 DSH 数据根目录；`profile` 是名字，不是整个目录。helper 可用
`--profile-dir <absolute-home>/profiles/<name>` 代替 `--home/--profile`，不要把两种
形式混用；先向用户解释拆解结果再确认。

**恢复**：绑定损坏或要改 home/profile 时，加 `--reconfigure`，但仍先获得更改许可。
已有有效绑定不因 `DSH_HOME` 变化而自动改写。缺失的内置 `sdk` 可在获准首次运行时
初始化；缺失自定义 profile 不自动创建。不要手写 binding 来绕过校验。

隔离目录同时含 uv/Python/venv 等技术资源，但不要将用户 profile、密钥、身份、
prompt 或 response 复制进去。DSH 自己的 profile/session 位于选定 home。

## 在自己的终端准备凭据

下列输入应由 **用户在自己的终端** 完成，不是在 agent 的工具日志里代输，也不是
让用户把 key 回复到聊天。不要打印该环境变量或开启 shell tracing 来验证实值。

### Bash 中隐藏输入

```bash
set +x
IFS= read -r -s -p 'DeepSeek API key: ' DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
```

### PowerShell 中隐藏输入

```powershell
$SecureKey = Read-Host 'DeepSeek API key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $SecureKey).Password
Remove-Variable SecureKey
```

**结果**：key 留在这个 shell 及其之后启动的子进程环境中；命令历史不含实值。环境
变量在运行时仍是敏感明文，不是加密凭据库。不要复制 shell 转储或截图到聊天。

从同一终端启动宿主，再检查“是否设置”即可。另开终端设置的变量不会回流到已运行
的宿主；这种情况需要重新启动，而不是再次粘贴 key。已有 DSH 凭据文件也可以作为
来源，但不要让 agent 读取/输出内容；来源存在不证明认证通过。

**恢复**：没有可用来源时不发送试探请求；认证被拒绝时在自己的终端更新凭据，再
明确决定是否重试。兼容端点设置遵循用户现有 base URL 环境/配置，不自动改写；
helper 的 provider/model 默认值和自定义路由参数见第 4 步。

## 4. 授权当前任务后调用

运行前确认当前工作区、任务输入的发送范围，以及首次启动可能创建 `sdk` 与本地
session。默认日志 patch 的边界见 [SDK 与安全边界](sdk-and-safety.md)。安装/绑定成功
不代表已经获得运行许可。

把已获准的任务放到隔离目录之外的 UTF-8 文件中；路径本身不是 prompt 的内容：

```bash
PROMPT_FILE='/absolute/path/to/approved-prompt.txt'
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" invoke --project-root "$PROJECT_ROOT" \
  --cwd "$PROJECT_ROOT" --prompt-file "$PROMPT_FILE"
```

```powershell
$PromptFile = 'C:\Work\Approved Prompts\review.txt'
& "$SkillDir\scripts\bootstrap.ps1" -Action exec -ProjectRoot $ProjectRoot -PythonArgs @(
  "$SkillDir\scripts\dsh_invoker.py", 'invoke', '--project-root', $ProjectRoot,
  '--cwd', $ProjectRoot, '--prompt-file', $PromptFile
)
```

文件和目录需实际存在；创建 prompt 文件本身也应符合用户授权，不往绑定目录存
prompt。helper 参数始终独立传递，不拼接待执行的 shell 字符串。

也可使用 `--stdin`（与 `--prompt-file` 二选一）。例如已存在文件的 Bash 输入重定向：

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" invoke --project-root "$PROJECT_ROOT" \
  --cwd "$PROJECT_ROOT" --stdin < "$PROMPT_FILE"
```

Windows 含空格路径优先用上面的 `--prompt-file`，不需要套用 POSIX 的 `<` 或 `export`。

`exec` 使用受管 venv 的**绝对 Python**并加 `-I -B`；Python 文件参数必须显式给 helper
的绝对路径。它不隐式安装，不使用 PATH 上另一个 Python。每次 SDK 都使用绑定的
显式绝对 home、profile 名称与绝对 `cwd`，不会依赖 SDK/wheel 找默认 home。

**自定义路由**：helper 即使选择了自定义 profile，也显式默认传入
`--provider deepseek-official --model deepseek-v4-flash`；只换 profile **不会继承**它的
provider/model。用户已指定其他路由时，两项一起显式传入，例如 Bash：

```bash
bash "$SKILL_DIR/scripts/bootstrap.sh" exec --project-root "$PROJECT_ROOT" -- \
  "$SKILL_DIR/scripts/dsh_invoker.py" invoke --project-root "$PROJECT_ROOT" \
  --cwd "$PROJECT_ROOT" --prompt-file "$PROMPT_FILE" \
  --provider 'user-chosen-provider' --model 'user-chosen-model'
```

PowerShell 在上述 `-PythonArgs` 数组中加入 `'--provider', 'user-chosen-provider',
'--model', 'user-chosen-model'`。这些名称是占位符，不自动改 profile 或 base URL。
正常 base URL 环境/配置规则仍生效；只有自定义路由对数据去向或凭据选择有实质不明时
才问一个当前问题。默认 `sdk` 路由无需新增一张 provider/model 问卷。

**结果**：检查退出状态，并读取 `session_id`、`finish_reason`、`final_response`。
仅 `completed` 表示 runtime 本轮完成，不证明任务已验收、代码正确或测试通过。
安装、保存、握手、真实认证和任务质量分别报告。输出示意（非实测记录）：

```json
{"session_id":"example-session-id","finish_reason":"completed","final_response":"任务的最终响应"}
```

**恢复**：非完成结果/脱敏错误均不当作成功。内部审批失败不要切到 `sdk-minimal`；
认证/网络错误不盲重试。每次 helper 调用都创建独立新 session。固定 SDK/runtime
`0.1.5rc1` 没有跨进程恢复方法；`--session-id` 会在读取 prompt / SDK 启动前报
`UNSUPPORTED_CONTINUATION`，不会发送第二次请求或偷偷改用 CLI。返回的 session ID 仅供
诊断，不是恢复入口；新任务不继承旧历史，不能仅把旧 ID 交给 `session/prompt`。
`--timeout-seconds` 包含普通 prompt 文件/stdin 获取和 worker 执行，到期后另有有界清理宽限；
拒绝 FIFO/设备等非普通 prompt 文件。SDK request timeout 不是整轮时限，POSIX 进程跟踪
也不是对任意恶意脱离/重挂父进程的内核隔离，详见安全边界。
