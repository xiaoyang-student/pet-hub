#requires -Version 5.1
<#
  PetHub 卸载脚本：停止进程 → 删除程序与开机自启 → 摘掉 MCP 注册 → 移除技能
  默认保留宠物存档（设置/语言/使用时长），加 -Purge 一并删除。
#>
[CmdletBinding()]
param([switch]$Purge)

function Say($m, $c = "Cyan") { Write-Host $m -ForegroundColor $c }
Say "== PetHub 卸载 ==" "Yellow"

Say "[1/5] 停止进程"
Get-Process PetHubLight -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*pet-hub*bridge*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Say "      ✓"

Say "[2/5] 删除开机自启与快捷方式"
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PetHub" -ErrorAction SilentlyContinue
Remove-Item -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub" -Recurse -Force -ErrorAction SilentlyContinue
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "PetHub.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force }
Say "      ✓"

Say "[3/5] 摘除 MCP 注册"
foreach ($f in @("$HOME\.workbuddy\mcp.json", "$HOME\.qoder\mcp.json",
                 "$HOME\.claude\mcp.json", "$HOME\.codex\mcp.json")) {
    if (Test-Path $f) {
        try {
            $cfg = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
            if ($cfg -and $cfg.ContainsKey("mcpServers") -and $cfg["mcpServers"].ContainsKey("pethub")) {
                $cfg["mcpServers"].Remove("pethub")
                ($cfg | ConvertTo-Json -Depth 6) | Set-Content $f -Encoding UTF8
                Say "      ✓ $f"
            }
        } catch { }
    }
}

Say "[4/5] 移除技能"
$skillDir = Join-Path $HOME ".workbuddy\skills\pethub-notify"
if (Test-Path $skillDir) { Remove-Item $skillDir -Recurse -Force; Say "      ✓" }

Say "[5/5] 删除程序文件"
$Install = Join-Path $env:LOCALAPPDATA "PetHub"
if ($Purge) {
    if (Test-Path $Install) { Remove-Item $Install -Recurse -Force }
    Say "      ✓ 已删除 $Install（含宠物存档）"
} else {
    # 保留存档与设置，其余删除
    $keep = @("settings.json", "usage-2026-10-26.json")
    if (Test-Path $Install) {
        Get-ChildItem $Install -Force | Where-Object {
            -not ($_.Name -eq "settings.json" -or $_.Name -like "usage-*.json")
        } | Remove-Item -Recurse -Force
    }
    Say "      ✓ 已删除程序（保留宠物存档，如需彻底清除请加 -Purge）"
}
Say "卸载完成。" "Green"
