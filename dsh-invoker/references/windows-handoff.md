# Windows SDK 验证交接

这是一份供接手开发者在原生 Windows 上继续验证的 runbook，不是新增诊断实现，也不代表 Windows 已获生产支持。当前默认 CI 只验证 Linux 与 macOS；PowerShell 脚本和 Windows 测试仍保留。Windows 验证按用户决定暂缓，接手者可在 Windows 环境继续。

## 当前状态与证据边界

- 当前分支：`feat/dsh-invoker`；PR：<https://github.com/souleastforest/harness-skills/pull/1>。交接时的基线是 `47ef6ad`。开始前在仓库根目录运行 `git rev-parse HEAD`，并以当时实际 HEAD 记录后续测试结果；不要假设这里的基线仍是最新。
- 已验证：Linux x64、macOS 14 ARM64 上的原生 `uv` 复用与真正缺少 `uv` 的路径（含受管 Python 3.12、`deepseek-harness-sdk==0.1.5rc1` / `deepseek-harness-runtime-bin==0.1.5rc1`、离线单元和必需的 loopback 原生集成）。此外，曾有一次最小真实 provider E2E 成功；它是单独、已授权的历史运行，不是本 runbook 的步骤，也不应在 Windows 排障时重跑。
- 最近已知的 Windows 原生执行来自 GitHub Actions run `37978460779`、job `113982608078`（[job link](https://github.com/souleastforest/harness-skills/actions/runs/37978460779/job/113982608078)），对应 HEAD `47ef6ad`（PR `https://github.com/souleastforest/harness-skills/pull/1`）。该 job 的 Windows 离线单测通过；当时要求执行的 native 集成套件有 7 个用例，SDK 初始化失败发生在 HTTP/provider 请求之前。Windows-owned Job 的生命周期/清理完成。不要将“7 个用例”误写为已验证通过，也不要从当前证据推出 SDK 或 OS 不支持。
- Windows 离线单元测试此前通过，但这是单元测试结果，不是 Windows SDK/native 初始化通过的证据。当前 Windows 根因仍 **未确定**；不能从通用 `JsonRpcError`、某个 OS 或原生 addon 推断根因，也不能据此称 SDK 或 Windows 不受支持。
- SDK 的 `JsonRpcError.message` 常是通用的 `Internal error`；实际服务端原因可能在 `error.data.details`。现有诊断只接受有限、来源明确的签名，未识别内容会归为 `unknown`。这意味着原因未知，不意味着没有原因。
- 过往开发已修正若干测试/可移植性问题：uv 项目隔离、ASCII JSON 与 UTF-8/CRLF 路径处理、Windows 被动 PID 检查、BSD `chmod` / GNU `gzip` fixture 行为，以及读取实际 `JsonRpcError.message` / `data.details` 的边界。它们不是 Windows SDK 启动根因结论，也不应被回归。
- 一次未提交的 Windows 初始化 probe 实验失败后已从源码和 workflow 移除；其文件与 workflow diff 只保存在本机忽略的交接备份中，不是交付内容。不要恢复或继续堆叠那份大型实验改动；先在原生 Windows 上按下文最小复现并观察官方异常字段。

## 平台与隔离前提

目标只限原生 Windows x64、PowerShell 7.2+（`pwsh`）、uv 可运行、受管 Python 3.12，以及匹配固定版本 `0.1.5rc1` 的 SDK/runtime wheel。Windows ARM、PowerShell 5.1、WSL 或模拟执行均不能代替这个目标。通过 uv 安装依赖不等于 SDK 启动已验证。

仅在 disposable checkout/临时目录执行 setup 和测试。切勿将真实用户 home、已有 DSH profile、API key、代理凭据或生产配置指向这些检查。示例路径均为占位符；把含空格/Unicode 的测试目录完整引用。bootstrap `exec` 使用受管 venv 的绝对 Python 并加 `-I -B`，不要以系统 Python、全局 CLI 或 `sdk-minimal` 绕过失败。没有任何 SDK 排障步骤需要真实模型请求或真实 key；安装阶段若需网络下载固定工具/wheels，应单独确认。

以下测试与 CI-driver 命令块只在新开的专用 PowerShell 子进程中执行（在新终端输入 `pwsh -NoProfile`）；不要粘贴到日常 PowerShell 会话。它们会临时覆盖进程级环境变量（包括 `HOME`、`USERPROFILE`、`TMP` 等），退出该子进程即可恢复父进程环境，不会改用户/机器级设置。先按变量名称筛除子进程继承的密钥、代理凭据和 DSH 覆盖项；不会输出这些变量的值：

```powershell
$SensitivePattern = '(?i)(api.?key|token|secret|password|credential|access.?key)'
Get-ChildItem Env: | Where-Object { $_.Name -match $SensitivePattern } | ForEach-Object {
  Remove-Item -LiteralPath "Env:$($_.Name)" -ErrorAction SilentlyContinue
}
foreach ($Name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','DEEPSEEK_API_KEY','DEEPSEEK_BASE_URL','DSH_HOME','DSH_RUNTIME_MODE','DSH_PERMISSION_MODE','DSH_BIN','DSH_INVOKER_REQUIRE_RUNTIME','DSH_INVOKER_TEST_ROOT','DSH_INVOKER_TEST_TMP')) {
  Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
}
```

后续命令不依赖用户/机器级 PATH 改动。若安装所需下载必须经过企业代理，请在受控 disposable 环境中遵循组织策略；不要为测试改变安全策略或把代理凭据写进输出。

## 逐步复现

### 1. 只读检查

在 `pwsh` 中进入仓库根目录并填入当前仓库与一个新建的 disposable 项目根目录。此检查不需要 Python/uv，不写入该项目：

```powershell
$Repo = (Resolve-Path -LiteralPath 'C:\work\harness-skills').Path
$Project = 'C:\dsh-test\项目 空格'
New-Item -ItemType Directory -Path $Project -Force | Out-Null
$Bootstrap = Join-Path $Repo 'dsh-invoker\scripts\bootstrap.ps1'
& $Bootstrap -Action check -ProjectRoot $Project
if ($LASTEXITCODE -ne 0) { throw 'read-only check failed' }
```

检查 JSON 中的 `platform`、`supported`、uv 来源，以及 runtime 是否存在。不要复制实际用户路径或环境信息到公开 issue/PR。首次 `check` 不会证明 SDK 导入、启动或认证成功。

### 2. 明确同意后安装项目隔离 SDK

setup 会下载/复用工具链并安装 wheel，只能在 disposable 项目里进行；先确认当前工作区和下载动作已获许可。它不保存 DSH home/profile、不启动 SDK、不调用模型：

```powershell
& $Bootstrap -Action setup -ProjectRoot $Project
if ($LASTEXITCODE -ne 0) { throw 'project-isolated setup failed' }
```

当前接口固定为 `-Action check|setup|exec -ProjectRoot <绝对路径>`，没有隐含的 `--` 或猜测性参数。若复用现有 uv，记录其路径/版本；不要卸载、替换或改动全局 uv。若没有 uv，可另在干净 disposable 副本验证项目本地 uv 分支，但不要为了模拟缺失而改机器级 PATH 或卸载 uv。

### 3. 运行离线单元测试

使用 CI driver 的 `unit` lane；它从 `ci_smoke.py` 的 `TEST_MODULES` 中排除 `test_runtime_integration.py`，不会启动 SDK runtime fixture 或模型请求。输出是经过校验的 JSON 摘要。`DSH_INVOKER_TEST_ROOT` 将测试临时文件限制在 disposable 项目的 ignored state 目录：

```powershell
$CiSmoke = Join-Path $Repo 'dsh-invoker\tests\ci_smoke.py'
$env:DSH_INVOKER_TEST_ROOT = Join-Path $Project '.dsh-invoker-profile\tmp\windows-unit-tests'
New-Item -ItemType Directory -Path $env:DSH_INVOKER_TEST_ROOT -Force | Out-Null
$UnitExit = 1
try {
  & $Bootstrap -Action exec -ProjectRoot $Project -PythonArgs @(
    $CiSmoke, '--lane', 'unit', '--project-root', $Project
  )
  $UnitExit = $LASTEXITCODE
} finally {
  Remove-Item Env:DSH_INVOKER_TEST_ROOT -ErrorAction SilentlyContinue
}
if ($UnitExit -ne 0) { throw 'offline unit tests failed' }
```

成功时输出 JSON 的 `lane` 为 `unit`、`ok` 为 `true`，并含 `counts` / `failed_test_ids`；失败时 `ok` 为 `false` 且退出码非零。此 lane 不包含 native integration，不向任何模型端点请求。若缺少 pinned wheels，不应因此把 unit lane 说成 native 已验证。

### 4. 运行必需的原生 SDK 集成（禁止真实 provider 流量）

原生测试自行生成临时 home/profile/项目、假 key 和 loopback 模型端点，不读取或使用当前用户的 DSH home、凭据或 profile；模型请求仅发往测试创建的 `127.0.0.1` listener，永不改成 provider URL 或真实 key。虽然不发真实 provider 请求，必须已有固定 `0.1.5rc1` wheels 才能运行；其他依赖安装只由前述 `setup` 执行。

```powershell
$env:DSH_INVOKER_REQUIRE_RUNTIME = '1'
$env:DSH_INVOKER_TEST_ROOT = Join-Path $Project '.dsh-invoker-profile\tmp\windows-native-tests'
New-Item -ItemType Directory -Path $env:DSH_INVOKER_TEST_ROOT -Force | Out-Null
$TestExit = 1
try {
  & $Bootstrap -Action exec -ProjectRoot $Project -PythonArgs @(
    $CiSmoke, '--lane', 'native', '--project-root', $Project
  )
  $TestExit = $LASTEXITCODE
} finally {
  Remove-Item Env:DSH_INVOKER_REQUIRE_RUNTIME -ErrorAction SilentlyContinue
  Remove-Item Env:DSH_INVOKER_TEST_ROOT -ErrorAction SilentlyContinue
}
if ($TestExit -ne 0) { throw 'REQUIRED native SDK tests failed' }
```

`bootstrap exec` 会用项目 venv 的绝对 Python 并加 `-I -B`，driver 也会验证 Python 3.12、SDK/runtime 版本与受管 venv。成功输出 `lane: native`, `ok: true` 和测试 counts；native lane 的 7 个用例必须全部运行、零 skip、零失败/错误。失败输出会限制为安全 JSON：允许列出的测试 ID、异常类型、阶段、退出码和经过 allowlist 的类别；bootstrap 自身错误也只返回安全错误码。非零退出即失败；缺 wheel、错误版本、native shell 不可用或任何 required skip 均不能降级或静默忽略。

7 个 native 用例覆盖：initialize/shutdown 零模型请求；默认 `sdk` helper synthetic loopback 请求、持久化、隐私字段缺席及不支持的跨进程恢复；同 runtime 的 continuation；恢复方法能力检查；假 loopback 认证错误脱敏；whole-turn timeout 与进程清理；以及只在隔离测试中显式选用 `sdk-minimal` 的 persistent shell。该测试中的 loopback 请求是合成流量，不等于真实 provider E2E，也不授权生产默认切换到 `sdk-minimal`。

测试或 driver 失败时，先在本机看 `lane`、`ok`、`counts`、安全 `failed_test_ids` / native `stage` / allowlisted code 与退出码。不要在 issue、PR、日志或聊天中粘贴原始 stdout/stderr、环境 dump、key、真实 home/路径、完整 provider 请求或异常详情。公开问题只附精确 SHA、run/job URL 和脱敏 JSON 摘要。

## 针对 initialize 失败的本机诊断方向

先复现上面的零 provider 流量生命周期测试，确认失败 stage 是 `initialize`，以及外层 watchdog 与 owned process cleanup 的结果。不要把一次 initialize 异常和进程泄漏混为一谈；测试 harness 在 Windows 会先将 gated Python worker 放入 kill-on-close Job 再放行子进程，并在结束时检查 Job 进程计数/清理。若报告 Job assignment、watchdog 或 cleanup 阶段错误，应优先独立处理该测试基础设施问题。

如生命周期仍在 initialize 失败，下一步由接手者在完全合成的临时 fixture 中做一次 Windows 本机观察，不使用真实 API key、不发送真实 provider/外网请求。现有 pinned SDK 的公开入口名称已在 `tests/test_runtime_integration.py` 验证：`from deepseek_harness.client import HarnessClient, HarnessConfig`；该测试的 `lifecycle` 分支用 `HarnessClient(HarnessConfig(**common))`、`client.initialize(cwd=..., provider="deepseek-official", model="ci-mock-model")` 和上下文管理退出完成 initialize/shutdown。该 lifecycle 分支不配置模型 HTTP listener，且不会发出 provider/model HTTP 请求；不要混淆它与另外会向 loopback mock 发送 HTTP 的集成用例。先沿用测试的 `RuntimeFixture` 构造 synthetic `sdk` profile、synthetic `dsh_home` 和隔离工作区，并在 fixture stage 记录点观察异常类型、`code`、`message` 及 `data` 中受限字符串；不得序列化整个 `data`，也不得公开原始异常/路径。对照 SDK/runtime `0.1.5rc1` 行为，区分 import/runtime-resolve/client-enter/initialize/shutdown 阶段。若仍需要新增 probe，先作为小型、本地、隔离且可清理的实验审查使用，不要未经审查加入正常 CI 或源码。

目标是取得一条能安全复现且证据足够的原因，不是扩大分类器。若原因未知，报告 unknown 和准确阶段，保留秘密，不根据通用 `-32603` 添加盲目 error map。不要将根因标为 SDK bug、Windows unsupported、provider 问题或 native addon 问题，除非新的原生证据能支持该结论。

## CI driver 与 uv 两条路径

CI 当前 driver 接口定义在 `dsh-invoker/tests/ci_smoke.py`，真实参数为 `--project-root`、`--uv-path`、`--expected-os Windows`、`--expected-arch x64`。它必须由受管 `bootstrap exec` 启动，在其约束的 `.dsh-invoker-profile/ci-scenarios/` 目录工作。现有 uv 分支先复用 runner 提供的绝对 uv，不更改它；再为第二个全新 scenario 建立不含 uv 的受控子进程 PATH，用固定版本 standalone uv 做 absent-uv setup/native 测试。driver 仅在已验证全新目录中清理自己创建的临时树。

如需单独核对 CI driver 两条 `uv` 分支，只在 disposable checkout 执行 workflow 同等命令；它会联网下载/安装固定工具链，并在第二个全新 scenario 执行真正的 absent-uv setup。先确认该安装范围获准。以下命令复用当前 checkout 中的现有 `uv`，不卸载或改系统 PATH；`ci_smoke.py` 会为 absent 分支构造隔离的子进程 PATH、用 Windows PowerShell 验证其中找不到 `uv`，最后确认现有 uv 文件摘要未变化：

```powershell
$Repo = (Resolve-Path -LiteralPath 'C:\work\harness-skills').Path
$CiRoot = Join-Path $Repo '.dsh-invoker-profile\ci-scenarios\existing uv 项目'
New-Item -ItemType Directory -Path (Join-Path $CiRoot 'os home'), (Join-Path $CiRoot 'bootstrap tmp') -Force | Out-Null
$UvPath = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
if (-not [IO.Path]::IsPathRooted($UvPath)) { throw 'uv path must be absolute' }
$env:HOME = Join-Path $CiRoot 'os home'
$env:USERPROFILE = $env:HOME
$env:APPDATA = Join-Path $env:HOME 'AppData/Roaming'
$env:LOCALAPPDATA = Join-Path $env:HOME 'AppData/Local'
$env:TMP = Join-Path $CiRoot 'bootstrap tmp'
$env:TEMP = $env:TMP
$env:TMPDIR = $env:TMP
$env:DSH_HOME = Join-Path $CiRoot 'unused disposable home'
foreach ($Name in @('DEEPSEEK_API_KEY','DEEPSEEK_BASE_URL','DSH_PERMISSION_MODE','DSH_BIN','NODE_OPTIONS')) {
  Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
}
& (Join-Path $Repo 'dsh-invoker\scripts\bootstrap.ps1') -Action setup -ProjectRoot $CiRoot
if ($LASTEXITCODE -ne 0) { throw 'existing-uv scenario setup failed' }
& (Join-Path $Repo 'dsh-invoker\scripts\bootstrap.ps1') -Action exec -ProjectRoot $CiRoot -PythonArgs @(
  (Join-Path $Repo 'dsh-invoker\tests\ci_smoke.py'), '--project-root', $CiRoot,
  '--uv-path', $UvPath, '--expected-os', 'Windows', '--expected-arch', 'x64'
)
if ($LASTEXITCODE -ne 0) { throw 'existing-uv and absent-uv CI driver failed' }
```

driver 参数必须使用真实的 `--project-root`, `--uv-path`, `--expected-os Windows`, `--expected-arch x64`；setup 创建 uv 与受管 venv 的版本由代码固定。运行成功会报告 Windows/x64 native units、existing-uv、absent-uv 和 required runtime 都通过；这比离线 native suite 重，但仍没有真实 provider 调用。只在临时/专用 disposable checkout 运行：项目技术状态位于该 checkout 的 `.dsh-invoker-profile/`；运行后确认它是你新建的验证目录，再按组织约定清理，不要删除未知内容。

注意：这份本地命令重放 CI driver 的两条验证路径，但真正恢复矩阵之前仍需用 GitHub Actions runner 验证完整 workflow。不要直接对日常工作区运行完整 driver。

## 修复后恢复 Windows CI 的门槛

默认分支矩阵当前只有 Linux x64 与 macOS 14 ARM64，这是有意按用户要求暂缓 Windows，不是成功的 Windows skip。若接手者在 Windows 修好并完成验证：

1. 在 disposable Windows x64 环境通过 `ci_smoke.py --lane unit` 离线单测；以 pinned wheel、假 key 与 synthetic home 运行原生集成的全部 7 个用例，零 skip、零失败/错误、零真实 provider/外网请求（测试所需的 loopback mock HTTP 是预期流量），并通过隐私与 fixture 清理断言。
2. 通过 workflow 完整 PowerShell job，既验证复用 uv，也验证真正没有 uv 的 isolated branch；`ci_smoke.py` 的 required native tests 不可 skip，检查临时树清理和已存在 uv 未变化。
3. 记录 Windows runner/PowerShell/uv/Python/SDK/runtime 版本、精确分支 SHA、run/job URL、安全 JSON 摘要和真实退出码。不得发布原始路径、stderr、环境或凭据。
4. 检查当时 workflow 中 Linux/macOS 的 matrix 约定，并恢复 Windows 项：`runner: windows-2022`、`os: Windows`、`arch: x64`；使用一致的 `label`（例如原标签 `Windows x64 (pwsh)`）。保持既有 PowerShell step 与 required gates，不增 skip 或 `continue-on-error`。
5. 在 GitHub Actions 上重新验证完整三平台 matrix 全绿后，才更新文档为三平台已验证。Windows 本机通过不等于 Windows CI 通过；不要在此之前写“支持三端”。

**交接完成条件**：根因由原生 Windows 证据确认；本机 required native suite 与两条 uv CI scenario 均通过；PR CI 重启后 Linux、macOS、Windows 三 lane 全绿；仅据此更新验证范围。除此以外继续维持本仓库当前 Linux/macOS 默认矩阵和 Windows 未验证说明。
