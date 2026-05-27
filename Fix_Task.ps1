# 以系統管理員身份執行此腳本
# 右鍵 PowerShell -> 以系統管理員身份執行
# 然後輸入: & "C:\Users\U01572\Documents\EFplant\Fix_Task.ps1"

$t = Get-ScheduledTask "EFplant AutoUpdate" -ErrorAction Stop

$t.Settings.ExecutionTimeLimit = "PT0S"   # 不限執行時間

if ($t.Settings.RestartOnFailure) {
    $t.Settings.RestartOnFailure.Interval = "PT3M"
    $t.Settings.RestartOnFailure.Count    = "99"
} else {
    $t.Settings.CimInstanceProperties["RestartInterval"].Value = "PT3M"
    $t.Settings.CimInstanceProperties["RestartCount"].Value    = 99
}

$t | Set-ScheduledTask

$t2 = Get-ScheduledTask "EFplant AutoUpdate"
Write-Host "Restart Count: $($t2.Settings.RestartCount)" -ForegroundColor Green
Write-Host "Execution Time Limit: $($t2.Settings.ExecutionTimeLimit)" -ForegroundColor Green
Write-Host "Task settings updated successfully." -ForegroundColor Green
