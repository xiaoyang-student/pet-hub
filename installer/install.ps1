#requires -Version 5.1
<#
  PetHub 安装脚本（中文输出）

  做四件事：
    1. 把程序装到 %LOCALAPPDATA%\PetHub
    2. 注册 MCP 工具服务（让 Qoder / WorkBuddy 等平台能主动上报）
    3. 装上 pethub-notify 技能（教 Agent 在干活时上报）
    4. 开机自启 + 桌面快捷方式

  卸载：运行 uninstall.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoAutoStart,        # 不设置开机自启
    [switch]$NoShortcut          # 不创建桌面快捷方式
)

$ErrorActionPreference = "Continue"
$Src      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Install  = Join-Path $env:LOCALAPPDATA "PetHub"
$AppName  = "PetHub 桌面宠物"

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
        $cfg = @{}
        if (Test-Path $f) {
            try { $cfg = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable } catch { $cfg = @{} }
            Copy-Item $f "$f.pethub-backup" -Force -ErrorAction SilentlyContinue
        }
        if (-not $cfg.ContainsKey("mcpServers")) { $cfg["mcpServers"] = @{} }
        $cfg["mcpServers"]["pethub"] = $srv
        ($cfg | ConvertTo-Json -Depth 6) | Set-Content $f -Encoding UTF8
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
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PetHub" -Value $exe
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

# ---------- 6. 登记到 Windows「已安装的应用」（设置里能看到、能标准卸载） ----------
$arp = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub"
$bytes = (Get-ChildItem -Path $Install -Recurse -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
if (-not $bytes) { $bytes = 0 }
New-Item -Path $arp -Force -ErrorAction SilentlyContinue | Out-Null
New-ItemProperty -Path $arp -Name "DisplayName"     -Value "PetHub 桌宠"              -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "DisplayVersion"  -Value "1.0"                     -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "Publisher"       -Value "PetHub Contributors"     -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "InstallLocation" -Value $Install                     -PropertyType String -Force | Out-Null
New-ItemProperty -Path $arp -Name "UninstallString" -Value "$Install\卸载 PetHub.cmd"   -PropertyType String -Force | Out-Null
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
