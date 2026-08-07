# EFplant - 以管理員身份執行此腳本以完善工作排程器設定
# 右鍵 → 以系統管理員身份執行 PowerShell，然後執行此腳本

$taskName = "EFplant AutoUpdate"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$mainPath   = Join-Path $projectDir "main.py"
$runAsUser  = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "找不到虛擬環境 Python：$pythonPath"
}

Write-Host "=== EFplant 工作排程器設定 ===" -ForegroundColor Cyan

# 移除舊任務並重建（含完整設定）
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action   = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$mainPath`"" `
    -WorkingDirectory $projectDir
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  ([TimeSpan]::Zero) `
    -RestartCount        99 `
    -RestartInterval     (New-TimeSpan -Minutes 3) `
    -StartWhenAvailable  `
    -MultipleInstances   IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId    $runAsUser `
    -LogonType S4U `
    -RunLevel  Highest

Register-ScheduledTask `
    -TaskName    $taskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Principal   $principal `
    -Description "EFplant 廠務儀表板 - 開機自動啟動，登出後持續運行，崩潰自動重啟" `
    -Force

Write-Host ""
Write-Host "=== 設定結果 ===" -ForegroundColor Green
$t = Get-ScheduledTask -TaskName $taskName
Write-Host "觸發條件 : $($t.Triggers[0].CimClass.CimClassName)  (應為 MSFT_TaskBootTrigger)"
Write-Host "執行身份 : $($t.Principal.UserId) / $($t.Principal.LogonType)"
Write-Host "失敗重啟 : $($t.Settings.RestartCount) 次，間隔 $($t.Settings.RestartInterval)"

Write-Host ""
Write-Host "完成！現在立即啟動，之後每次開機也會自動啟動。" -ForegroundColor Green
Start-ScheduledTask -TaskName $taskName
