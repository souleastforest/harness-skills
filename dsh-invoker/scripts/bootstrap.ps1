#requires -Version 7.2
# Requires PowerShell 7.2+ on native Windows x64 (.NET 6 link resolution).
# Do not change execution policy
# to run this script; if organizational policy blocks it, report the limitation.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('check', 'setup', 'exec')][string]$Action,
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [string[]]$PythonArgs = @()
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:OwnLock = $false
$script:State = $null
$script:SafeCwd = $PSScriptRoot
$Marker = 'dsh-invoker-bootstrap-v1'

function Stop-Bootstrap([string]$Code, [string]$Message) {
    $exception = [System.InvalidOperationException]::new($Message)
    $exception.Data['BootstrapCode'] = $Code
    throw $exception
}
function Has-Control([string]$Value) { return $Value -match '[\x00-\x1f\x7f]' }
function Assert-PlainPath([string]$Path, [bool]$Directory) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.PSIsContainer -ne $Directory) {
            Stop-Bootstrap 'unsafe_path' 'A runtime path is a link, junction or has an unexpected type.'
        }
    } else {
        # Get-Item detects dangling links that Test-Path reports as absent.
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($null -ne $item) { Stop-Bootstrap 'unsafe_path' 'A runtime path is a dangling link or has an unexpected type.' }
    }
}
function Assert-StatePaths {
    Assert-PlainPath $script:State $true
    foreach ($name in @('bin', 'python', 'python-downloads', 'python-bin', 'tools', 'tool-bin', 'cache', 'credentials', 'tmp', 'staging', 'venv', '.bootstrap-lock')) {
        Assert-PlainPath (Join-Path $script:State $name) $true
    }
    foreach ($name in @('.bootstrap-owner', 'runtime.json', 'binding.json')) { Assert-PlainPath (Join-Path $script:State $name) $false }
}
function Test-Owned {
    $path = Join-Path $script:State '.bootstrap-owner'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -gt 128) { return $false }
    return [IO.File]::ReadAllText($path).Trim() -eq $Marker
}
function New-ExclusiveDirectory([string]$Path) {
    # Directory.CreateDirectory alone silently reuses an existing directory.
    if (Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue) { Stop-Bootstrap 'filesystem' 'A supposedly new toolchain directory already exists.' }
    $null = New-Item -ItemType Directory -Path $Path -ErrorAction Stop
}
function Write-Exclusive([string]$Path, [string]$Text) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $data = [Text.UTF8Encoding]::new($false).GetBytes($Text)
        $stream.Write($data, 0, $data.Length)
    } finally { $stream.Dispose() }
}
function Get-Candidate([string]$Source, [string]$HomePath) {
    $profilesPath = Join-Path $HomePath 'profiles'
    $profiles = @()
    if (Test-Path -LiteralPath $profilesPath -PathType Container) {
        $profiles = @(Get-ChildItem -LiteralPath $profilesPath -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { -not (Has-Control $_.Name) } | Sort-Object Name | ForEach-Object { $_.Name })
    }
    return [ordered]@{ source = $Source; home = $HomePath; exists = (Test-Path -LiteralPath $HomePath -PathType Container); profiles_dir_exists = (Test-Path -LiteralPath $profilesPath -PathType Container); profiles = $profiles }
}
function Get-IsolatedEnvironment {
    $result = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $name = [string]$entry.Key
        if ($name -match '^(UV_|PIP_|CONDA|_CE_CONDA$|_CE_M$|VIRTUAL_ENV|PYTHON|__PYVENV_LAUNCHER__$|NETRC$)') { continue }
        $result[$name] = [string]$entry.Value
    }
    $controlled = @{
        UV_NO_CONFIG = '1'; UV_NO_PROGRESS = '1'; UV_KEYRING_PROVIDER = 'disabled'; PYTHONDONTWRITEBYTECODE = '1'
        UV_CACHE_DIR = 'cache'; UV_PYTHON_INSTALL_DIR = 'python'; UV_PYTHON_CACHE_DIR = 'python-downloads'
        UV_PYTHON_BIN_DIR = 'python-bin'; UV_TOOL_DIR = 'tools'; UV_TOOL_BIN_DIR = 'tool-bin'; UV_CREDENTIALS_DIR = 'credentials'
        TMPDIR = 'tmp'; TMP = 'tmp'; TEMP = 'tmp'
    }
    foreach ($name in $controlled.Keys) {
        $value = $controlled[$name]
        if ($value -ne '1' -and $value -ne 'disabled') { $value = Join-Path $script:State $value }
        $result[$name] = $value
    }
    # Preserve proxy, normal SSL certificate settings and SystemRoot. Never
    # modify the calling process's environment, user PATH or PowerShell profile.
    return ,$result
}
function Invoke-Isolated([string]$Executable, [string[]]$Arguments, [switch]$Live, [switch]$UvProcess) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Executable
    $info.UseShellExecute = $false
    $info.WorkingDirectory = $script:SafeCwd
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $info.Environment.Clear()
    $environment = Get-IsolatedEnvironment
    foreach ($entry in $environment.GetEnumerator()) { $info.Environment[$entry.Key] = $entry.Value }
    if ($UvProcess) {
        foreach ($name in @('HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'APPDATA', 'LOCALAPPDATA')) {
            $info.Environment[$name] = Join-Path $script:State 'credentials'
        }
        $info.Environment['NETRC'] = Join-Path $script:State 'credentials/no-netrc'
    }
    # Validation/uv diagnostics stay private and are not saved to a log file.
    # Python exec inherits all three streams, including prompt data from stdin.
    if (-not $Live) { $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        if (-not $Live) {
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
        }
        $process.WaitForExit()
        if ($Live) { return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = '' } }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $null = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $stdout }
    } finally { $process.Dispose() }
}
function Invoke-Uv([string[]]$Arguments) {
    return Invoke-Isolated $script:Uv (@('--no-config', '--cache-dir', (Join-Path $script:State 'cache')) + $Arguments) -UvProcess
}
function Resolve-RuntimePath([string]$Path) {
    # Resolve one reparse target at a time, including linked parent components.
    # ResolveLinkTarget(false) is available in .NET 6 / PowerShell 7.2. Never
    # start the interpreter whose startup directories we are checking.
    $pending = $Path
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    for ($hops = 0; $hops -le 40; $hops++) {
        $full = [IO.Path]::GetFullPath($pending)
        if ((Has-Control $full) -or -not $seen.Add($full)) { Stop-Bootstrap 'unsafe_path' 'A runtime link is cyclic or has an unsupported target.' }
        $cursor = [IO.Path]::GetPathRoot($full)
        [string[]]$parts = $full.Substring($cursor.Length).Split([char[]]@('\', '/'), [StringSplitOptions]::RemoveEmptyEntries)
        $followed = $false
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $cursor = Join-Path $cursor $parts[$index]
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
            if ($null -eq $item) { Stop-Bootstrap 'unsafe_path' 'A runtime link is dangling or cannot be inspected.' }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                try { $target = $item.ResolveLinkTarget($false) }
                catch { Stop-Bootstrap 'unsafe_path' 'Cannot resolve a runtime link or junction.' }
                if ($null -eq $target) { Stop-Bootstrap 'unsafe_path' 'An unsupported runtime reparse point was found.' }
                $pending = $target.FullName
                for ($rest = $index + 1; $rest -lt $parts.Count; $rest++) { $pending = Join-Path $pending $parts[$rest] }
                $followed = $true
                break
            }
        }
        if (-not $followed) {
            $prefix = $script:State + [IO.Path]::DirectorySeparatorChar
            if (-not $full.Equals($script:State, [StringComparison]::OrdinalIgnoreCase) -and -not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Bootstrap 'unsafe_path' 'A runtime link escapes the project toolchain directory.'
            }
            return $item
        }
    }
    Stop-Bootstrap 'unsafe_path' 'A runtime link has a cyclic or excessive target chain.'
}
function Assert-RuntimeLinks([string[]]$Roots) {
    # Enumerate each directory without -Recurse: reparse points are resolved and
    # checked before traversal. Follow reachable internal aliases once, not the
    # unrelated cache tree; a visited set also terminates directory cycles.
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $visited = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($root in $Roots) { $pending.Push($root) }
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        if (-not $visited.Add($directory)) { continue }
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            if (Has-Control $item.FullName) { Stop-Bootstrap 'unsafe_path' 'Control characters in runtime paths are not supported.' }
            $target = $item
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { $target = Resolve-RuntimePath $item.FullName }
            if ($target.PSIsContainer) { $pending.Push($target.FullName) }
        }
    }
}
function Assert-ManagedPython {
    if (-not (Test-Path -LiteralPath $script:Python -PathType Leaf)) { Stop-Bootstrap 'runtime_missing' 'Approve setup to create the project-only managed runtime first.' }
    $interpreter = Resolve-RuntimePath $script:Python
    if ($interpreter.PSIsContainer) { Stop-Bootstrap 'unsafe_path' 'The venv interpreter must be a regular file.' }
    $config = Join-Path $script:State 'venv\pyvenv.cfg'
    Assert-PlainPath $config $false
    if (-not (Test-Path -LiteralPath $config -PathType Leaf) -or (Get-Item -LiteralPath $config).Length -gt 16384) { Stop-Bootstrap 'runtime_interpreter' 'The owned venv configuration is missing or invalid.' }
    $homes = @([IO.File]::ReadAllLines($config) | Where-Object { $_ -match '^home\s*=' })
    if ($homes.Count -ne 1) { Stop-Bootstrap 'runtime_interpreter' 'The owned venv has no single managed Python home.' }
    $baseHome = [IO.Path]::GetFullPath(($homes[0] -split '=', 2)[1].Trim())
    $prefix = (Join-Path $script:State 'python') + [IO.Path]::DirectorySeparatorChar
    if (-not $baseHome.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { Stop-Bootstrap 'unsafe_path' 'The venv must refer to Python owned by this project.' }
    $baseDirectory = Resolve-RuntimePath $baseHome
    if (-not $baseDirectory.PSIsContainer -or -not $baseDirectory.FullName.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Bootstrap 'unsafe_path' 'The venv must refer to Python owned by this project.'
    }
    foreach ($name in @('python.exe', 'python312.dll')) {
        $binary = Resolve-RuntimePath (Join-Path $baseHome $name)
        if ($binary.PSIsContainer) { Stop-Bootstrap 'unsafe_path' 'A managed Python binary has an unexpected type.' }
    }
}
function Install-LocalUv {
    $metadata = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\assets\uv-release.json') -Raw | ConvertFrom-Json
    if ($metadata.schema_version -ne 1 -or $metadata.version -ne '0.12.24') { Stop-Bootstrap 'release_metadata' 'The pinned uv release metadata is not recognized.' }
    $assetInfo = @($metadata.assets.'windows-x86_64')
    if ($assetInfo.Count -ne 2 -or $assetInfo[0] -ne 'uv-x86_64-pc-windows-msvc.zip' -or $assetInfo[1] -notmatch '^[0-9a-f]{64}$') { Stop-Bootstrap 'release_metadata' 'Invalid pinned Windows uv asset metadata.' }
    $asset = [string]$assetInfo[0]
    $operation = Join-Path $script:State ('staging\uv.' + [Guid]::NewGuid().ToString('N'))
    New-ExclusiveDirectory $operation
    $download = Join-Path $operation 'download'
    $extract = Join-Path $operation 'extract'
    New-ExclusiveDirectory $download
    New-ExclusiveDirectory $extract
    $archive = Join-Path $download $asset
    # HttpClient explicitly checks each redirect remains HTTPS. No network
    # installer scripts, no external extractor, and no Python dependency.
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(10)
    try {
        $url = [Uri]('https://github.com/astral-sh/uv/releases/download/0.12.24/' + $asset)
        for ($redirects = 0; ; $redirects++) {
            if ($url.Scheme -ne 'https' -or $redirects -gt 10) { Stop-Bootstrap 'uv_download' 'The pinned uv download attempted an unsafe redirect.' }
            $response = $client.GetAsync($url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            if ([int]$response.StatusCode -in @(301, 302, 303, 307, 308)) {
                $location = $response.Headers.Location
                if ($null -eq $location) { $response.Dispose(); Stop-Bootstrap 'uv_download' 'The uv download redirect has no location.' }
                $url = [Uri]::new($url, $location)
                $response.Dispose()
                continue
            }
            try {
                $null = $response.EnsureSuccessStatusCode()
                $target = [IO.File]::Open($archive, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $response.Content.CopyToAsync($target).GetAwaiter().GetResult() } finally { $target.Dispose() }
            } finally { $response.Dispose() }
            break
        }
    } finally { $client.Dispose(); $handler.Dispose() }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine $assetInfo[1]) { Stop-Bootstrap 'uv_digest' 'The uv archive SHA-256 differs from the pinned official release.' }
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $uvEntry = $null
        foreach ($entry in $zip.Entries) {
            # Official Windows assets contain root uv.exe/uvx.exe only. Strictly
            # allow those names: this rejects all absolute/traversal/ADS entries.
            if ($entry.FullName -cnotin @('uv.exe', 'uvx.exe', 'uvw.exe') -or -not $seen.Add($entry.FullName)) { Stop-Bootstrap 'uv_archive' 'The uv ZIP contains an unexpected, duplicate or unsafe entry.' }
            $unixType = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($unixType -notin @(0, 0x8000) -or ($entry.ExternalAttributes -band 0x410) -ne 0) { Stop-Bootstrap 'uv_archive' 'Links, directories and special ZIP entries are not allowed.' }
            if ($entry.Length -le 0 -or $entry.Length -gt 150MB) { Stop-Bootstrap 'uv_archive' 'A uv ZIP entry has an invalid size.' }
            if ($entry.FullName -eq 'uv.exe') { $uvEntry = $entry }
        }
        if ($null -eq $uvEntry) { Stop-Bootstrap 'uv_archive' 'No uv executable exists in the validated ZIP.' }
        $file = Join-Path $extract 'uv.exe'
        $stream = [IO.File]::Open($file, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $source = $uvEntry.Open()
        try { $source.CopyTo($stream) } finally { $source.Dispose(); $stream.Dispose() }
    } finally { $zip.Dispose() }
    $script:Uv = Join-Path $script:State 'bin\uv.exe'
    [IO.File]::Copy($file, $script:Uv, $false)
    $script:UvSource = 'project'
    $script:UvSha = (Get-FileHash -LiteralPath $script:Uv -Algorithm SHA256).Hash.ToLowerInvariant()
    # Delete only our known files; never recursively erase an arbitrary path.
    [IO.File]::Delete($archive); [IO.File]::Delete($file)
    [IO.Directory]::Delete($download); [IO.Directory]::Delete($extract); [IO.Directory]::Delete($operation)
}

$ExitCode = 1
try {
    if ($PSVersionTable.PSVersion -lt [Version]'7.2') { Stop-Bootstrap 'powershell_version' 'PowerShell 7.2 or newer (pwsh) is required; do not change execution policy.' }
    if (Has-Control $ProjectRoot) { Stop-Bootstrap 'project_root' 'Control characters in project paths are not supported.' }
    if (-not [IO.Path]::IsPathFullyQualified($ProjectRoot) -or -not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { Stop-Bootstrap 'project_root' 'An existing absolute project root is required.' }
    $rootItem = Get-Item -LiteralPath $ProjectRoot -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Stop-Bootstrap 'unsafe_path' 'Use the real project directory, not a link or junction.' }
    $cursor = $rootItem
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Stop-Bootstrap 'unsafe_path' 'Use a project path without linked or junction ancestors.' }
        $cursor = $cursor.Parent
    }
    $ProjectRoot = [IO.Path]::GetFullPath($rootItem.FullName).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($ProjectRoot.EndsWith(':')) { $ProjectRoot += [IO.Path]::DirectorySeparatorChar }
    if ($Action -ne 'exec' -and $PythonArgs.Count -gt 0) { Stop-Bootstrap 'arguments' 'PythonArgs is only valid for exec.' }
    $script:State = Join-Path $ProjectRoot '.dsh-invoker-profile'
    $script:Python = Join-Path $script:State 'venv\Scripts\python.exe'
    Assert-StatePaths
    $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    $supported = $IsWindows -and $architecture -eq 'X64' -and [Environment]::Is64BitProcess
    $platform = if ($supported) { 'windows-x86_64' } else { 'unsupported' }
    $script:Uv = $null; $script:UvSource = 'absent'; $script:UvSha = ''
    $existing = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $existing) { $script:Uv = $existing.Source; $script:UvSource = 'path' }
    elseif ((Test-Owned) -and (Test-Path -LiteralPath (Join-Path $script:State 'bin\uv.exe') -PathType Leaf)) {
        $script:Uv = Join-Path $script:State 'bin\uv.exe'; Assert-PlainPath $script:Uv $false; $script:UvSource = 'project'
    }
    if (Has-Control ([string]$script:Uv)) { Stop-Bootstrap 'unsafe_path' 'Control characters in executable paths are not supported.' }
    if ($Action -eq 'check') {
        $candidates = @()
        if (-not [string]::IsNullOrWhiteSpace($env:DSH_HOME) -and -not (Has-Control $env:DSH_HOME)) {
            $homePath = if ([IO.Path]::IsPathFullyQualified($env:DSH_HOME)) { [IO.Path]::GetFullPath($env:DSH_HOME) } else { [IO.Path]::GetFullPath((Join-Path $ProjectRoot $env:DSH_HOME)) }
            $candidates += Get-Candidate 'DSH_HOME' $homePath
        }
        $userHome = if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) { $env:USERPROFILE } else { [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile) }
        if (-not [string]::IsNullOrWhiteSpace($userHome)) { $candidates += Get-Candidate 'default' (Join-Path $userHome '.dsh') }
        $manifestExists = Test-Path -LiteralPath (Join-Path $script:State 'runtime.json') -PathType Leaf
        $pythonExists = Test-Path -LiteralPath $script:Python -PathType Leaf
        $status = if ($manifestExists -and $pythonExists) { 'unverified' } else { 'missing' }
        [ordered]@{
            schema_version = 1; action = 'check'; project_root = $ProjectRoot; state_dir = $script:State
            platform = $platform; supported = [bool]$supported
            conditions = 'Requires native Windows x64 and PowerShell 7.2+; macOS/Linux use bootstrap.sh.'
            uv = [ordered]@{ available = ($null -ne $script:Uv); source = $script:UvSource; path = $script:Uv; version = $null }
            runtime = [ordered]@{ manifest_exists = [bool]$manifestExists; python_exists = [bool]$pythonExists; python_path = $script:Python; status = $status }
            home_candidates = $candidates
        } | ConvertTo-Json -Depth 6 -Compress
        $ExitCode = 0
    } else {
        if (-not $supported) { Stop-Bootstrap 'unsupported_platform' 'The pinned DSH runtime requires native Windows x64; ARM64 and emulation are not promised.' }
        $manifest = Join-Path $script:State 'runtime.json'
        if ($Action -eq 'exec' -or (Test-Path -LiteralPath $manifest)) {
            if (-not (Test-Owned)) { Stop-Bootstrap 'unowned_runtime' 'Runtime ownership is missing; existing files will not be overwritten.' }
            if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { Stop-Bootstrap 'runtime_missing' 'No ready manifest exists. Approve setup first.' }
            if (Test-Path -LiteralPath (Join-Path $script:State '.bootstrap-lock')) { Stop-Bootstrap 'runtime_busy' 'Another setup is active or an interrupted lock needs inspection.' }
            Assert-RuntimeLinks @((Join-Path $script:State 'python'), (Join-Path $script:State 'venv'))
            Assert-ManagedPython
            $support = Join-Path $PSScriptRoot 'bootstrap_support.py'
            $validation = Invoke-Isolated $script:Python @('-I', '-B', $support, 'validate', '--quiet', '--project-root', $ProjectRoot)
            if ($validation.ExitCode -ne 0) { Stop-Bootstrap 'runtime_validation' 'Runtime ownership, paths or versions failed validation. No automatic repair was attempted.' }
            if ($Action -eq 'exec') {
                if ($PythonArgs.Count -eq 0) { Stop-Bootstrap 'arguments' 'exec requires an explicit Python script or Python arguments.' }
                $execution = Invoke-Isolated $script:Python (@('-I', '-B') + $PythonArgs) -Live
                $ExitCode = $execution.ExitCode
            } else {
                $report = Invoke-Isolated $script:Python @('-I', '-B', $support, 'validate', '--project-root', $ProjectRoot)
                if ($report.ExitCode -ne 0) { Stop-Bootstrap 'runtime_validation' 'Cannot verify the owned runtime.' }
                Write-Output $report.Output.TrimEnd()
                $ExitCode = 0
            }
        } else {
            if (Test-Path -LiteralPath $script:State -PathType Container) {
                if (Test-Owned) { Stop-Bootstrap 'incomplete_runtime' 'Incomplete owned setup exists. Inspect and explicitly remove its technical files before retrying; preserve binding.json.' }
                # binding.json, dev-tests and CI fixtures can coexist here.
                # Only exact bootstrap targets must be absent before claiming.
                foreach ($name in @('bin', 'python', 'python-downloads', 'python-bin', 'tools', 'tool-bin', 'cache', 'credentials', 'tmp', 'staging', 'venv', '.bootstrap-owner', 'runtime.json')) {
                    if (Get-Item -LiteralPath (Join-Path $script:State $name) -Force -ErrorAction SilentlyContinue) { Stop-Bootstrap 'unowned_runtime' 'A toolchain target already exists; setup will not replace it.' }
                }
            } else { New-ExclusiveDirectory $script:State }
            New-ExclusiveDirectory (Join-Path $script:State '.bootstrap-lock')
            $script:OwnLock = $true
            Write-Exclusive (Join-Path $script:State '.bootstrap-owner') ($Marker + "`n")
            foreach ($name in @('bin', 'python', 'python-downloads', 'python-bin', 'tools', 'tool-bin', 'cache', 'credentials', 'tmp', 'staging')) { New-ExclusiveDirectory (Join-Path $script:State $name) }
            if ($null -eq $script:Uv) { Install-LocalUv }
            $version = Invoke-Uv @('--version')
            if ($version.ExitCode -ne 0 -or $version.Output -notmatch '^uv\s+(\d+\.\d+\.\d+)(\s|$)') { Stop-Bootstrap 'uv_version' 'The selected uv version cannot be recognized. Existing uv was not replaced.' }
            $uvVersion = $Matches[1]
            if ([Version]$uvVersion -lt [Version]'0.9.20') { Stop-Bootstrap 'uv_version' 'uv 0.9.20 or newer is required; this skill never upgrades existing uv.' }
            $capabilities = @(
                @{ args = @('python', 'install', '--help'); flags = @('--install-dir', '--no-bin', '--no-registry') },
                @{ args = @('venv', '--help'); flags = @('--no-project', '--managed-python', '--no-python-downloads') },
                @{ args = @('pip', 'install', '--help'); flags = @('--python', '--no-python-downloads', '--only-binary', '--link-mode', '--prerelease', '--default-index', '--keyring-provider') }
            )
            foreach ($capability in $capabilities) {
                $help = Invoke-Uv $capability.args
                if ($help.ExitCode -ne 0) { Stop-Bootstrap 'uv_capability' 'The selected uv help command failed; no upgrade was attempted.' }
                foreach ($flag in $capability.flags) {
                    if (-not $help.Output.Contains($flag)) { Stop-Bootstrap 'uv_capability' 'The selected uv lacks a required isolation flag; no upgrade or fallback was attempted.' }
                }
            }
            $step = Invoke-Uv @('python', 'install', '--install-dir', (Join-Path $script:State 'python'), '--no-bin', '--no-registry', '3.12')
            if ($step.ExitCode -ne 0) { Stop-Bootstrap 'python_install' 'Project managed Python installation failed. Connectivity/platform diagnostics are withheld.' }
            Assert-StatePaths
            Assert-RuntimeLinks @((Join-Path $script:State 'python'))
            if (Get-Item -LiteralPath (Join-Path $script:State 'venv') -Force -ErrorAction SilentlyContinue) { Stop-Bootstrap 'existing_venv' 'An existing environment will not be replaced.' }
            $step = Invoke-Uv @('venv', '--no-project', '--managed-python', '--no-python-downloads', '--python', '3.12', (Join-Path $script:State 'venv'))
            if ($step.ExitCode -ne 0) { Stop-Bootstrap 'venv_create' 'Project venv creation failed. Incomplete files are left for inspection.' }
            Assert-StatePaths
            Assert-RuntimeLinks @((Join-Path $script:State 'python'), (Join-Path $script:State 'venv'))
            Assert-ManagedPython
            $support = Join-Path $PSScriptRoot 'bootstrap_support.py'
            $validation = Invoke-Isolated $script:Python @('-I', '-B', $support, 'check-python', '--project-root', $ProjectRoot)
            if ($validation.ExitCode -ne 0) { Stop-Bootstrap 'runtime_interpreter' 'The venv does not belong to this project-managed Python.' }
            $step = Invoke-Uv @('pip', 'install', '--python', $script:Python, '--no-python-downloads', '--link-mode', 'copy', '--only-binary', ':all:', '--prerelease', 'explicit', '--default-index', 'https://pypi.org/simple', '--keyring-provider', 'disabled', '--no-sources', 'deepseek-harness-sdk==0.1.5rc1', 'deepseek-harness-runtime-bin==0.1.5rc1')
            if ($step.ExitCode -ne 0) { Stop-Bootstrap 'sdk_install' 'Pinned SDK/runtime wheel installation failed. No source build, global pip or existing environment fallback was attempted.' }
            Assert-StatePaths
            Assert-RuntimeLinks @((Join-Path $script:State 'python'), (Join-Path $script:State 'venv'))
            Assert-ManagedPython
            $record = Invoke-Isolated $script:Python @('-I', '-B', (Join-Path $PSScriptRoot 'bootstrap_support.py'), 'record', '--project-root', $ProjectRoot, '--platform', $platform, '--uv-path', $script:Uv, '--uv-version', $uvVersion, '--uv-source', $script:UvSource, '--uv-sha256', $script:UvSha)
            if ($record.ExitCode -ne 0) { Stop-Bootstrap 'runtime_validation' 'Cannot record the new owned runtime; incomplete technical files are left for inspection.' }
            Write-Output $record.Output.TrimEnd()
            $ExitCode = 0
        }
    }
} catch {
    $code = 'bootstrap_failed'
    $message = 'Bootstrap failed. Inspect platform, paths, connectivity and local policy; sensitive diagnostics are withheld.'
    if ($_.Exception.Data.Contains('BootstrapCode')) { $code = [string]$_.Exception.Data['BootstrapCode']; $message = $_.Exception.Message }
    [Console]::Error.WriteLine((@{ ok = $false; error = $code; message = $message } | ConvertTo-Json -Compress))
    $ExitCode = 1
} finally {
    if ($script:OwnLock) {
        try { [IO.Directory]::Delete((Join-Path $script:State '.bootstrap-lock')) } catch { }
    }
}
exit $ExitCode
