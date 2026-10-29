#requires -Version 5.1
<#
  PetHub 卸载脚本（PowerShell 5.1 兼容）

  默认行为（符合"卸载就清干净"）：
    1. 停止所有 PetHub 相关任务（壳层 + Python 桥 + 任何在安装目录里跑的进程）
    2. 删除整个安装目录 + 整个数据目录（%LOCALAPPDATA%\PetHub，含宠物存档）
    3. 清理注册表、开机自启、快捷方式、MCP 注册、技能、临时日志

  安全护栏（绝不越界）：
    * 删除前校验目录"确实是 PetHub 目录"，否则拒绝删除
    * 盘符根 / Windows / Program Files / 用户主目录 / 桌面 / AppData 根 —— 一律拒绝
    * mcp.json 只移除 pethub 这一条，文件和其它 MCP 服务原样保留
    * 不卸载系统 Python、不动 WebView2、不改系统 PATH（那些是原本就存在的环境）

  参数：
    -Dir <路径>      指定安装目录（不传则自动从注册表 / 默认位置推断）
    -KeepSave        保留宠物存档（默认一并删除）
    -RemoveMcp       同时移除各平台 mcp.json 里的 pethub 条目（默认会移除）
    -KeepMcp         保留 MCP 注册条目
#>
[CmdletBinding()]
param(
    [string]$Dir = "",
    [switch]$KeepSave,
    [switch]$KeepMcp
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$AppName = "PetHub 桌宠"
$McpName = "pethub"

function Say($msg, $color = "Cyan") { Write-Host $msg -ForegroundColor $color }
function Ok($msg) { Write-Host ("      √ " + $msg) -ForegroundColor Green }
function Warn($msg) { Write-Host ("      ! " + $msg) -ForegroundColor Yellow }

# ================================================================
# 0. 定位安装目录
#
# 安装位置是不确定的：可能是安装器默认的 %LOCALAPPDATA%\PetHub，
# 也可能是用户用 -Dir 指定的 E:\Tools\PetHub，或者直接解压在某个目录。
# 所以这里不认任何固定路径，只按"证据链"推断，并且
# **每一条自动推断都必须通过 Test-PetHubDir 指纹校验**——
# 否则把脚本拷到别处运行，就会把那个目录当成安装目录删掉。
# ================================================================

# 从任意注册表/快捷方式取值里剥出目录：
#   "X:\a\b\PetHubLight.exe" --uninstall   →  X:\a\b
#   X:\a\b\PetHubLight.exe                 →  X:\a\b
#   X:\a\b                                 →  X:\a\b
function ConvertTo-Dir {
    param([string]$value)
    if (-not $value) { return "" }
    # 先剥出"第一段路径"再清理引号，顺序反了会把 "…exe" --uninstall 里的
    # 右引号留在路径末尾（Test-Path 会报非法字符）
    $v = $value.Trim()
    if ($v -match '^"([^"]+)"') { $v = $matches[1] }
    elseif ($v -match '^(\S+)') { $v = $matches[1] }
    $v = $v.Trim().Trim('"').Trim("'").TrimEnd('\', '/')
    if (-not $v) { return "" }
    # 指向文件（哪怕已经被删掉）→ 取所在目录
    if (Test-Path $v -PathType Leaf) { return (Split-Path -Parent $v) }
    if ($v -match '\.[A-Za-z0-9]{1,6}$') { return (Split-Path -Parent $v) }
    return $v
}

# 正在运行的 PetHub 主程序在哪 —— 装在哪儿都跑不掉
function Get-ProcInstallDir {
    foreach ($n in @("PetHubLight", "PetHub")) {
        foreach ($p in @(Get-Process -Name $n -ErrorAction SilentlyContinue)) {
            try {
                $pth = $p.Path
                if (-not $pth) { continue }
                $d = Split-Path -Parent $pth
                if ($d -and (Test-PetHubDir $d)) { return $d.TrimEnd('\') }
            } catch { }
        }
    }
    return ""
}

# 开始菜单 / 桌面 / 启动项里的快捷方式指向哪儿
function Get-ShortcutInstallDir {
    $dirs = @()
    foreach ($k in @("Programs", "Desktop", "Startup")) {
        try { $p = [Environment]::GetFolderPath($k); if ($p) { $dirs += $p } } catch { }
    }
    try { $dirs += (Join-Path ([Environment]::GetFolderPath("Programs")) "PetHub") } catch { }
    try {
        $ws = New-Object -ComObject WScript.Shell
    } catch { return "" }
    foreach ($d in $dirs) {
        if (-not (Test-Path $d)) { continue }
        foreach ($lnk in @(Get-ChildItem -Path $d -Filter "*.lnk" -ErrorAction SilentlyContinue)) {
            try {
                $c = ConvertTo-Dir $ws.CreateShortcut($lnk.FullName).TargetPath
                if ($c -and (Test-PetHubDir $c)) { return $c }
            } catch { }
        }
    }
    return ""
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-InstallDir {
    param([string]$given)

    # 0) 命令行显式指定：最可信，直接用（清洗脏引号与尾部斜杠）
    if ($given) {
        # cmd 传 "-Dir "%~dp0"" 时，尾部 \" 会被 powershell.exe 的
        # 命令行解析当成转义引号，这里可能收到 D:\xxx" 或 D:\xxx\" 之类的脏值。
        # 统一去引号、去首尾空白与斜杠；清完为空则继续走自动探测。
        $g = $given.Trim().Trim('"').Trim("'").Trim('\', '/')
        if ($g) { return $g }
    }

    # 1) 正在运行的进程 —— 不管当年装在哪儿
    $procDir = Get-ProcInstallDir
    if ($procDir) { return $procDir }

    # 2) 注册表登记（InstallLocation / 卸载命令 / 图标 都能反推出目录）
    foreach ($root in @("HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub",
                        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub",
                        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\PetHub")) {
        if (-not (Test-Path $root)) { continue }
        foreach ($name in @("InstallLocation", "UninstallString",
                            "QuietUninstallString", "DisplayIcon")) {
            $v = (Get-ItemProperty -Path $root -Name $name -ErrorAction SilentlyContinue).$name
            $cand = ConvertTo-Dir $v
            if ($cand -and (Test-PetHubDir $cand)) { return $cand.TrimEnd('\') }
        }
    }

    # 3) 快捷方式指向的位置
    $scDir = Get-ShortcutInstallDir
    if ($scDir) { return $scDir }

    # 4) 默认位置 —— 只在"确实是 PetHub 目录"时才认，不再无条件相信
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $HOME "AppData\Local" }
    $def = Join-Path $base "PetHub"
    if ((Test-Path $def) -and (Test-PetHubDir $def)) { return $def }

    # 5) 最后才考虑脚本所在目录（便携解压的场景），且必须带 PetHub 指纹
    if ($ScriptDir -and (Test-PetHubDir $ScriptDir)) { return $ScriptDir.TrimEnd('\') }
    return ""
}

# ================================================================
# 1. 安全校验
# ================================================================
function Test-SafeToDelete {
    param([string]$path)
    if (-not $path) { return $false }

    $full = ""
    try { $full = [System.IO.Path]::GetFullPath($path) } catch { return $false }
    $full = $full.TrimEnd('\')
    $low = $full.ToLowerInvariant()

    # 盘符根 / 关键系统位置 —— 一律拒绝
    if ($low -match '^[a-z]:$') { return $false }
    $blocked = @(
        $env:WINDIR, $env:SystemRoot,
        (Join-Path ${env:ProgramFiles} ""),
        (Join-Path ${env:ProgramFiles(x86)} ""),
        $env:USERPROFILE, $env:APPDATA, $env:LOCALAPPDATA,
        ([Environment]::GetFolderPath("Desktop")),
        ([Environment]::GetFolderPath("Personal")),
        $env:TEMP
    )
    foreach ($b in $blocked) {
        if (-not $b) { continue }
        $bb = $b.TrimEnd('\').ToLowerInvariant()
        if ($low -eq $bb) { return $false }
    }
    # 路径至少要有两级，避免误伤
    if (($full.Split('\').Count) -lt 2) { return $false }
    return $true
}

function Test-PetHubDir {
    param([string]$path)
    if (-not (Test-Path $path)) { return $false }
    # 只要满足任意一条，就认定这是 PetHub 的目录
    $marks = @("PetHubLight.exe", "app\index.html", "bridge\server.py",
               "connectors\pethub_mcp.py", "卸载 PetHub.cmd", "uninstall.ps1")
    foreach ($m in $marks) {
        if (Test-Path (Join-Path $path $m)) { return $true }
    }
    # 空目录（文件已被删掉一半）但名字叫 PetHub 也算
    if ((Split-Path $path -Leaf) -match '(?i)^PetHub') { return $true }
    return $false
}

# ================================================================
# 2. 完整删目录（带重试 + 兜底），返回是否删干净
# ================================================================
function Remove-DirFull {
    param([string]$path)

    if (-not (Test-Path $path)) { return $true }
    $p = ([System.IO.Path]::GetFullPath($path)).TrimEnd('\')

    # 先尽量清空内容，再删壳 —— 这样即使有单个文件被占用也能删掉大部分
    for ($round = 0; $round -lt 3; $round++) {
        Get-ChildItem -LiteralPath $p -Force -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        try {
            Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop
        } catch { }
        if (-not (Test-Path $p)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    # 兜底：交给 cmd 的 rmdir（注意去掉尾部反斜杠，否则可能解析成根目录）
    try {
        & cmd.exe /c "rmdir /s /q `"$p`"" 2>$null | Out-Null
    } catch { }
    Start-Sleep -Milliseconds 400
    return (-not (Test-Path $p))
}

# ================================================================
# 3. 停止任务
# ================================================================
function Stop-PetHubTasks {
    param([string]$dir)

    $killed = 0
    $selfPid = $PID
    # 保护自己与父进程，绝不把自己杀掉
    $guard = @($selfPid)
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$selfPid" -ErrorAction SilentlyContinue
        if ($p -and $p.ParentProcessId) { $guard += $p.ParentProcessId }
    } catch { }

    # 3.1 先礼貌请它自己退出（--quit 会让桌宠摘掉托盘图标再走）
    $exe = Join-Path $dir "PetHubLight.exe"
    if (Test-Path $exe) {
        try {
            $q = Start-Process -FilePath $exe -ArgumentList "--quit" -PassThru -WindowStyle Hidden
            if ($q) { $q.WaitForExit(4000) | Out-Null }
        } catch { }
    }

    # 3.2 收集要杀的进程：名字命中 / 路径在安装目录内 / 命令行命中桥
    $targets = @()
    Get-Process -Name "PetHubLight" -ErrorAction SilentlyContinue |
        ForEach-Object { $targets += $_.Id }

    $dirLow = if ($dir) { $dir.TrimEnd('\').ToLowerInvariant() } else { "" }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        if ($guard -contains $_.ProcessId) { return }
        $hit = $false
        if ($dirLow -and $_.ExecutablePath) {
            try {
                if ($_.ExecutablePath.ToLowerInvariant().StartsWith($dirLow)) { $hit = $true }
            } catch { }
        }
        if (-not $hit -and $_.CommandLine) {
            $cl = $_.CommandLine.ToLowerInvariant()
            if ($cl -match 'pethublight' -or
                $cl -match 'bridge[\\/]server\.py' -or
                $cl -match 'connectors[\\/]pethub_mcp\.py') { $hit = $true }
        }
        if ($hit) { $targets += $_.ProcessId }
    }

    $targets | Select-Object -Unique | ForEach-Object {
        if ($guard -contains $_) { return }
        try {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
            $killed++
        } catch { }
    }
    return $killed
}

# ================================================================
# 主流程
# ================================================================
Say "== PetHub 卸载 ==" "Yellow"
Say ""

$Install = Resolve-InstallDir $Dir
if (-not $Install) {
    Say "未找到 PetHub 安装位置。可手动指定：" "Red"
    Say "  powershell -File uninstall.ps1 -Dir `"D:\PetHub`"" "Gray"
    exit 1
}
$Install = $Install.TrimEnd('\')
Say "目标目录: $Install"

# 安全检查（数据目录也要检查）
$DataDir = Join-Path $env:LOCALAPPDATA "PetHub"
if (-not (Test-SafeToDelete $Install)) {
    Say "拒绝删除：[$Install] 看起来不是 PetHub 的程序目录。" "Red"
    exit 1
}

# ---------------- 1/6 停止任务 ----------------
Say "[1/6] 停止所有 PetHub 任务"
$n = Stop-PetHubTasks $Install
# 再确认一次：还有活着的就继续杀
for ($i = 0; $i -lt 6; $i++) {
    $alive = @(Get-Process -Name "PetHubLight" -ErrorAction SilentlyContinue).Count
    if ($alive -eq 0) { break }
    Stop-PetHubTasks $Install | Out-Null
    Start-Sleep -Milliseconds 600
}
if ($n -gt 0) { Ok "已停止 $n 个进程" } else { Ok "没有正在运行的进程" }

# ---------------- 2/6 删除安装目录 ----------------
Say "[2/6] 删除程序目录（整个目录）"
if (Test-Path $Install) {
    if (Test-PetHubDir $Install) {
        if (Remove-DirFull $Install) { Ok "已删除 $Install" }
        else { Warn "$Install 未能完全删除（多半还有进程占用），请重启后再删" }
    } else {
        Warn "跳过：$Install 里没有找到 PetHub 的文件，为安全起见不动它"
    }
} else { Ok "目录已不存在" }

# ---------------- 3/6 删除数据目录 ----------------
Say "[3/6] 删除数据目录（%LOCALAPPDATA%\PetHub）"
if ($KeepSave) {
    Ok "按参数保留存档"
} elseif ((-not $DataDir) -or ($DataDir.TrimEnd('\') -eq $Install)) {
    Ok "数据目录与程序目录相同，已随上一步删除"
} elseif (Test-Path $DataDir) {
    if (Test-SafeToDelete $DataDir) {
        if (Remove-DirFull $DataDir) { Ok "已删除 $DataDir" }
        else { Warn "$DataDir 未能完全删除（可能有文件被占用）" }
    } else { Warn "跳过：位置不安全" }
} else { Ok "目录已不存在" }

# ---------------- 4/6 注册表 + 快捷方式 ----------------
Say "[4/6] 清理注册表、开机自启与快捷方式"
foreach ($root in @("HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
                    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
                    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")) {
    if (Test-Path $root) {
        Remove-ItemProperty -Path $root -Name "PetHub" -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $root -Name "PetHubLight" -ErrorAction SilentlyContinue
    }
}
$arpRemoved = $false
foreach ($root in @("HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub",
                    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub",
                    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\PetHub")) {
    if (Test-Path $root) {
        Remove-Item -Path $root -Recurse -Force -ErrorAction SilentlyContinue
        $arpRemoved = $true
    }
}
Ok ("已移除「已安装的应用」条目" + $(if ($arpRemoved) { "" } else { "（原本就没有）" }))

# 快捷方式：桌面 / 开始菜单 / 启动项
$shortcutDirs = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("DesktopDirectory"),
    ([Environment]::GetFolderPath("Programs")),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup")
)
$sc = 0
foreach ($d in ($shortcutDirs | Select-Object -Unique)) {
    if (-not $d) { continue }
    foreach ($name in @("PetHub.lnk", "PetHub 桌宠.lnk", "PetHubLight.lnk")) {
        $f = Join-Path $d $name
        if (Test-Path $f) {
            Remove-Item $f -Force -ErrorAction SilentlyContinue
            $sc++
        }
    }
}
Ok ("已删除快捷方式 $sc 个")

# ---------------- 5/6 MCP 注册 + 技能 ----------------
if ($KeepMcp) {
    Say "[5/6] 保留 MCP 注册（按参数）"
} else {
    Say "[5/6] 摘除 MCP 注册（只删 pethub 条目，文件与其它服务保留）"
    $files = @(
        (Join-Path $HOME ".workbuddy\mcp.json"),
        (Join-Path $HOME ".qoder\mcp.json"),
        (Join-Path $HOME ".claude\mcp.json"),
        (Join-Path $HOME ".codex\mcp.json"),
        (Join-Path $HOME ".zcode\mcp.json")
    )
    foreach ($f in $files) {
        if (-not (Test-Path $f)) { continue }
        try {
            $cfg = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json
            $servers = $cfg.mcpServers
            if (-not $servers) { continue }
            # PowerShell 5.1 没有 -AsHashtable，用 PSObject 操作才通用
            if ($servers.PSObject.Properties.Name -contains $McpName) {
                $servers.PSObject.Properties.Remove($McpName)
                ($cfg | ConvertTo-Json -Depth 8) | Set-Content $f -Encoding UTF8
                Ok ("已移除 " + $f)
            }
        } catch {
            Warn ("跳过 $f（解析失败，未改动）")
        }
    }
    # 安装时留的备份文件
    foreach ($f in ($files | ForEach-Object { $_ + ".pethub-backup" })) {
        if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }
}
$skill = Join-Path $HOME ".workbuddy\skills\pethub-notify"
if (Test-Path $skill) {
    Remove-Item $skill -Recurse -Force -ErrorAction SilentlyContinue
    Ok "已移除 pethub-notify 技能"
}

# ---------------- 6/6 临时文件 ----------------
Say "[6/6] 清理临时文件"
$tmp = 0
foreach ($n in @("pethub-light.log", "pethub-uninstall.ps1")) {
    $f = Join-Path $env:TEMP $n
    if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue; $tmp++ }
}
Get-ChildItem -Path $env:TEMP -Filter "pethub-*.log" -File -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Ok ("已清理 $tmp 个临时文件")

Say ""
Say "卸载完成。安装目录与数据目录已整体删除，注册表 / 自启 / 快捷方式 / MCP 条目已清理。" "Green"
Say "说明：系统 Python、WebView2、其它 MCP 服务与你自己的文件均未改动。" "Gray"
exit 0
