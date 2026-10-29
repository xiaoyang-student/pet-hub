#requires -Version 5.1
<#
  PetHub 安装脚本（中文输出）

  做四件事：
    1. 把程序装到指定目录（默认 %LOCALAPPDATA%\PetHub，可用 -Dir 改到任意位置）
    2. 注册 MCP 工具服务（让 Qoder / WorkBuddy 等平台能主动上报）
    3. 装上 pethub-notify 技能（教 Agent 在干活时上报）
    4. 开机自启 + 桌面快捷方式

  参数：
    -Dir <路径>      安装到哪儿（不传则用 %LOCALAPPDATA%\PetHub）
    -NoAutoStart     不设置开机自启
    -NoShortcut      不创建桌面快捷方式

  用法：
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Dir "E:\Tools\PetHub"

  卸载：运行 uninstall.ps1（它会从注册表 InstallLocation 找到真实安装位置，
        不依赖任何固定路径）
#>
[CmdletBinding()]
param(
    [string]$Dir = "",           # 安装目录（留空 = 当前用户的默认位置）
    [switch]$NoAutoStart,        # 不设置开机自启
    [switch]$NoShortcut          # 不创建桌面快捷方式
)

$ErrorActionPreference = "Continue"
$Src      = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName  = "PetHub 桌面宠物"

# ------------------------------------------------------------------
# 安装目录：不写死任何盘符/用户目录。
#   1) -Dir 显式指定 → 用它（清洗引号与尾部斜杠后取绝对路径）
#   2) 没指定       → %LOCALAPPDATA%\PetHub（当前用户的默认位置）
#   3) 危险位置（盘符根 / Windows / Program Files / 用户主目录本身）→ 拒绝
# ------------------------------------------------------------------
function Resolve-TargetDir {
    param([string]$given)

    $d = ""
    if ($given) { $d = $given.Trim().Trim('"').Trim("'").TrimEnd('\', '/') }
    if (-not $d) {
        $base = $env:LOCALAPPDATA
        if (-not $base) { $base = Join-Path $HOME "AppData\Local" }
        $d = Join-Path $base "PetHub"
    }
    try { $d = [System.IO.Path]::GetFullPath($d) } catch { }
    return $d.TrimEnd('\')
}

function Test-DirSafe {
    param([string]$path)
    if (-not $path) { return $false }
    if ($path -match '^[A-Za-z]:$') { return $false }        # 盘符根
    if ($path.Split('\').Count -lt 2) { return $false }      # 至少两级
    $low = $path.ToLowerInvariant()
    foreach ($b in @($env:WINDIR, $env:SystemRoot, ${env:ProgramFiles},
                     ${env:ProgramFiles(x86)}, $env:USERPROFILE, $env:APPDATA,
                     $env:LOCALAPPDATA, $env:TEMP)) {
        if (-not $b) { continue }
        if ($low -eq $b.TrimEnd('\').ToLowerInvariant()) { return $false }
    }
    return $true
}

$Install = Resolve-TargetDir $Dir
if (-not (Test-DirSafe $Install)) {
    Write-Host "拒绝安装到 $Install —— 该位置不安全（盘符根 / 系统目录 / 用户主目录）。" -ForegroundColor Red
    Write-Host "请用 -Dir 指定一个普通文件夹，例如 -Dir `"$env:LOCALAPPDATA\PetHub`"" -ForegroundColor Yellow
    exit 1
}

function Say($msg, $color = "Cyan") { Write-Host $msg -ForegroundColor $color }

Say "== PetHub 安装 ==" "Green"

# ---------- 1. 复制文件 ----------
Say "[1/5] 安装到 $Install"
New-Item -ItemType Directory -Force -Path $Install | Out-Null
foreach ($d in @("app", "bridge", "connectors")) {
    $from = Join-Path (Split-Path -Parent $Src) $d
    if (Test-Path $from) {
        Copy-Item -Path $from -Destination (Join-Path $Install $d) -Recurse -Force
        Say "      + $d"
    }
}
# 壳层程序（publish 输出）
$exeSrc = Join-Path (Split-Path -Parent $Src) "PetHubLight.exe"
if (Test-Path $exeSrc) {
    Copy-Item -Path (Join-Path (Split-Path -Parent $Src) "*") -Destination $Install -Recurse -Force -Exclude "installer"
    Say "      + 主程序"
} else {
    $pub = Join-Path (Split-Path -Parent $Src) "light\bin\Release\net10.0-windows10.0.19041.0\win-x64\publish"
    if (Test-Path $pub) { Copy-Item -Path (Join-Path $pub "*") -Destination $Install -Recurse -Force; Say "      + 主程序" }
    else { Say "      ! 未找到主程序（PetHubLight.exe），请确认安装包完整" "Yellow" }
}

# ---------- 2. Python（桥接服务需要） ----------
Say "[2/5] 检查 Python"
# 注意：`??` 是 PowerShell 7 才有的写法，Windows 自带的 5.1 会报语法错，这里用兼容写法
$py = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if ($py) {
    Say "      ✓ $($py.Source)"
} else {
    Say "      ! 未检测到 Python：桌宠本体可用，但外部应用上报（MCP/CLI/桥接）需要 Python" "Yellow"
    Say "        建议安装 Python 3.10+ 并勾选 Add to PATH" "Yellow"
}

# ---------- 3. 注册 MCP 工具服务 ----------
Say "[3/5] 注册 MCP 工具服务"
if ($py) {
    $pyExe = $py.Source -replace 'pythonw\.exe$', 'python.exe'
    if (-not (Test-Path $pyExe)) { $pyExe = $py.Source }
    $mcp = Join-Path $Install "connectors\pethub_mcp.py"
    $srv = @{ command = $pyExe; args = @($mcp) }
    $targets = @(
        (Join-Path $HOME ".workbuddy\mcp.json"),
        (Join-Path $HOME ".qoder\mcp.json"),
        (Join-Path $HOME ".claude\mcp.json"),
        (Join-Path $HOME ".codex\mcp.json")
    )
    foreach ($f in $targets) {
        if (-not (Test-Path (Split-Path -Parent $f))) { continue }
        # 注意：ConvertFrom-Json -AsHashtable 是 PowerShell 6+ 才有的参数，
        # Windows 自带的 5.1 上会直接报错，以前会把用户已有的 mcpServers 全清掉。
        # 这里改成 5.1 / 7 都通用的写法，只往里加 pethub，其它服务原样保留。
        $cfg = $null
        if (Test-Path $f) {
            try { $cfg = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $cfg = $null }
            if ($cfg) { Copy-Item $f "$f.pethub-backup" -Force -ErrorAction SilentlyContinue }
        }
        if (-not $cfg) { $cfg = [pscustomobject]@{} }
        if (-not ($cfg.PSObject.Properties.Name -contains "mcpServers")) {
            $cfg | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value ([pscustomobject]@{})
        }
        $servers = $cfg.mcpServers
        if ($servers -is [System.Collections.IDictionary]) {
            $servers["pethub"] = $srv
        } else {
            $servers | Add-Member -MemberType NoteProperty -Name "pethub" `
                                  -Value ([pscustomobject]$srv) -Force
        }
        ($cfg | ConvertTo-Json -Depth 8) | Set-Content $f -Encoding UTF8
        Say "      ✓ $f"
    }
} else {
    Say "      跳过（无 Python）" "Yellow"
}

# ---------- 4. 安装技能 ----------
Say "[4/5] 安装 pethub-notify 技能"
$skillDir = Join-Path $HOME ".workbuddy\skills\pethub-notify"
New-Item -ItemType Directory -Force -Path $skillDir | Out-Null
$skillSrc = Join-Path $Src "skill\SKILL.md"
if (Test-Path $skillSrc) {
    Copy-Item $skillSrc (Join-Path $skillDir "SKILL.md") -Force
    Say "      ✓ $skillDir"
}

# ---------- 5. 自启 + 快捷方式 ----------
Say "[5/5] 开机自启与快捷方式"
$exe = Join-Path $Install "PetHubLight.exe"
if (-not $NoAutoStart -and (Test-Path $exe)) {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PetHub" -Value ('"' + $exe + '"')
    Say "      ✓ 开机自启"
}
if (-not $NoShortcut -and (Test-Path $exe)) {
    try {
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "PetHub.lnk"))
        $sc.TargetPath = $exe
        $sc.Description = $AppName
        $sc.Save()
        Say "      ✓ 桌面快捷方式"
    } catch { Say "      ! 快捷方式创建失败：$_" "Yellow" }
}

# ---------- 5.5 装卸载入口（卸载逻辑统一在 uninstall.ps1） ----------
$unSrc = Join-Path $Src "uninstall.ps1"
if (Test-Path $unSrc) {
    Copy-Item $unSrc (Join-Path $Install "uninstall.ps1") -Force
}
# .cmd 只做壳：内容全 ASCII，避免编码问题；真正逻辑在 PetHubLight.exe --uninstall
$cmdBody = @'
@echo off
rem PetHub uninstaller entry.
rem The real uninstall logic is built into PetHubLight.exe --uninstall,
rem which is also the UninstallString registered in Apps & features.
"%~dp0PetHubLight.exe" --uninstall
exit /b %ERRORLEVEL%
'@
try { Set-Content -Path (Join-Path $Install "卸载 PetHub.cmd") -Value $cmdBody -Encoding ASCII } catch { }

# ---------- 6. 登记到 Windows「已安装的应用」（设置里能看到、能标准卸载） ----------
$arp = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub"
$bytes = (Get-ChildItem -Path $Install -Recurse -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
if (-not $bytes) { $bytes = 0 }
New-Item -Path $arp -Force -ErrorAction SilentlyContinue | Out-Null
New-ItemProperty -Path $arp -Name "DisplayName"     -Value "PetHub 桌宠"              -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "DisplayVersion"  -Value "1.2.1"                   -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "Publisher"       -Value "PetHub Contributors"     -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "InstallLocation" -Value $Install                     -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "UninstallString" -Value "`"$exe`" --uninstall"      -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "QuietUninstallString" -Value "`"$exe`" --uninstall --silent" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "DisplayIcon"     -Value "$Install\PetHubLight.exe"   -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "EstimatedSize"   -Value ([int][Math]::Min([int]::MaxValue, $bytes / 1KB)) -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $arp -Name "NoModify"        -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $arp -Name "NoRepair"        -Value 1 -PropertyType DWord -Force | Out-Null

# ---------- 启动 ----------
if (Test-Path $exe) {
    Say "启动中..." "Green"
    Start-Process $exe
}
Say "安装完成！桌宠应该已经出现在桌面底部。" "Green"
Say "提示：右键宠物可切换中文 / English。" "Green"
