<#
.SYNOPSIS
Windows + Clash Verge (Rev) 全自动一键安装与自启脚本。

.DESCRIPTION
无需手动复制脚本、无需手动改配置，一键完成：
1. 自动将本项目 (cproxy) 安装到当前 Python 环境；
2. 自动定位本机 Clash Verge (Rev) 目录，将全局扩展脚本直接注入 `profiles\Script.js` 并在 `profiles.yaml` 中自动挂载；
3. 自动读取 Clash Verge 的 `external-controller`、`secret` 与 `mixed-port` 生成旁路配置；
4. 自动注册 Windows 登录无黑框后台自启（支持计划任务 + 启动文件夹双回退，无需管理员权限也能成功）并立即拉起守护进程；
5. 立即执行一次首轮状态巡检。
#>

param(
    [string]$Controller = "",
    [string]$Secret = "",
    [int]$MixedPort = 0,
    [string]$VergeDir = "",
    [switch]$SkipPipInstall,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$TaskName = "cproxy-verge-daemon"
$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupVbs = Join-Path $StartupDir "$TaskName.vbs"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Stop-ExistingVergeDaemon {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe' OR Name = 'cproxy-daemon.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "cproxy\.cli\s+verge\s+daemon" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step 失败，退出码: $LASTEXITCODE"
    }
}

if ($Uninstall) {
    Write-Host "[1/3] 正在停止运行中的旁路守护进程..." -ForegroundColor Cyan
    Stop-ExistingVergeDaemon
    Write-Host "[2/3] 正在移除计划任务与开机启动项..." -ForegroundColor Cyan
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $StartupVbs) {
        Remove-Item $StartupVbs -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[3/3] 已完成卸载: $TaskName" -ForegroundColor Green
    exit 0
}

# 1. 检测 Python 环境
$PyCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $PyCmd) {
    $PyCmd = Get-Command py.exe -ErrorAction SilentlyContinue
}
if (-not $PyCmd) {
    Write-Error "未检测到 Python (>=3.11)，请先安装 Python 并勾选 Add Python to PATH。"
    exit 1
}
$PythonInfo = & $PyCmd.Source -c "import json,sys; print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:2])}))"
Assert-NativeSuccess "Python 环境检测"
$PythonInfo = $PythonInfo | ConvertFrom-Json
if ($PythonInfo.version[0] -lt 3 -or ($PythonInfo.version[0] -eq 3 -and $PythonInfo.version[1] -lt 11)) {
    throw "cproxy 需要 Python >= 3.11，当前为 $($PythonInfo.version -join '.')"
}
$PythonExe = $PythonInfo.executable
$PythonDir = Split-Path -Parent $PythonExe
$PythonwCandidate = Join-Path $PythonDir "pythonw.exe"
if (Test-Path $PythonwCandidate) {
    $PythonwExe = $PythonwCandidate
} else {
    $PwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    $PythonwExe = if ($PwCmd) { $PwCmd.Source } else { $PythonExe }
}

# 2. 自动安装 cproxy 包
if (-not $SkipPipInstall -and (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    Write-Host "==> [1/4] 正在安装 cproxy Python 包..." -ForegroundColor Cyan
    & $PythonExe -m pip install --upgrade $RepoRoot
    Assert-NativeSuccess "cproxy 安装"
} else {
    Write-Host "==> [1/4] 跳过 pip 安装，使用现有 cproxy 环境..." -ForegroundColor Cyan
}

# 3. 一键注入 Clash Verge 全局扩展脚本 + 生成旁路配置
Write-Host "==> [2/4] 正在自动对接 Clash Verge 并注入全局扩展脚本..." -ForegroundColor Cyan
$InstallArgs = @("-m", "cproxy.cli", "verge", "install")
if ($Controller) { $InstallArgs += @("--controller", $Controller) }
if ($Secret) { $InstallArgs += @("--secret", $Secret) }
if ($MixedPort -gt 0) { $InstallArgs += @("--mixed-port", "$MixedPort") }
if ($VergeDir) { $InstallArgs += @("--verge-dir", $VergeDir) }
& $PythonExe @InstallArgs
Assert-NativeSuccess "Clash Verge 旁路集成"

# 4. 注册开机自启并立即拉起后台无窗口守护进程
Write-Host "==> [3/4] 正在注册后台常驻守护进程 ($TaskName)..." -ForegroundColor Cyan
Stop-ExistingVergeDaemon
$DaemonExe = Join-Path $PythonDir "cproxy-daemon.exe"
Copy-Item -Path $PythonwExe -Destination $DaemonExe -Force

$RegisteredTaskOk = $false
try {
    $Action = New-ScheduledTaskAction -Execute $DaemonExe -Argument "-m cproxy.cli verge daemon"
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $RegisteredTaskOk = $true
    if (Test-Path $StartupVbs) {
        Remove-Item $StartupVbs -Force -ErrorAction SilentlyContinue
    }
    Write-Host "    已注册并启动 Windows 计划任务: $TaskName" -ForegroundColor Green
} catch {
    # 非管理员权限下若计划任务受限，自动回退到用户 Startup 启动文件夹 + 立即后台拉起
    $VbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$DaemonExe"" -m cproxy.cli verge daemon", 0, False
"@
    Set-Content -Path $StartupVbs -Value $VbsContent -Encoding Unicode
    Start-Process -FilePath $DaemonExe -ArgumentList "-m cproxy.cli verge daemon" -WindowStyle Hidden
    Write-Host "    已写入用户登录自启脚本并拉起后台守护进程: $StartupVbs" -ForegroundColor Green
}

# 5. 执行一次初始状态检查与调度健康核验
Write-Host "==> [4/4] 正在验证与 Clash Verge 内核的调度健康状态..." -ForegroundColor Cyan
& $DaemonExe -m cproxy.cli verge once --no-rebalance
Assert-NativeSuccess "首轮 AI 看护"
$StatusReady = $false
for ($Attempt = 0; $Attempt -lt 15; $Attempt++) {
    & $PythonExe -m cproxy.cli verge status | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $StatusReady = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $StatusReady) {
    throw "Verge 状态核验在 30 秒内未通过"
}
& $PythonExe -m cproxy.cli verge status
Assert-NativeSuccess "Verge 状态展示"

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host "  Windows + Clash Verge 调度机制一键部署完成！" -ForegroundColor Green
Write-Host "  提示：若上方提示未挂载策略组，请在 Clash Verge 中点一次「订阅 -> 刷新」" -ForegroundColor Yellow
Write-Host "        刷新后可随时运行 'cproxy verge' 查看调度健康状态。" -ForegroundColor Yellow
Write-Host "==============================================================" -ForegroundColor Green
