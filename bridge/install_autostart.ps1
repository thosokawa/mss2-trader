<#
.SYNOPSIS
  Windows ログオン時に run_all.ps1 を実行するタスクスケジューラ登録。
  管理者権限は不要（今ログオンしているユーザーの対話セッションで動く）。

.PARAMETER SetId
  run_all.ps1 に渡す銘柄セットID（既定 1）。

.PARAMETER DelaySec
  ログオンしてから起動するまでの遅延秒数（デスクトップ / ネットワークが整うまで、既定 60）。

.EXAMPLE
  .\bridge\install_autostart.ps1
  .\bridge\install_autostart.ps1 -SetId 2 -DelaySec 90

  # 手動で今すぐ実行:   Start-ScheduledTask -TaskName mss2-trader
  # 解除:               Unregister-ScheduledTask -TaskName mss2-trader -Confirm:$false
#>
param(
  [int]$SetId = 1,
  [int]$DelaySec = 60
)

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'run_all.ps1'
if (-not (Test-Path $script)) { throw "run_all.ps1 が見つかりません: $script" }

$taskName = 'mss2-trader'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -SetId {1}' -f $script, $SetId)

$trigger = New-ScheduledTaskTrigger -AtLogOn
try { $trigger.Delay = ('PT{0}S' -f $DelaySec) } catch { }

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "登録しました： タスク '$taskName'（ログオン $DelaySec 秒後 / SetId=$SetId）"
Write-Host "今すぐ試す：   Start-ScheduledTask -TaskName $taskName"
Write-Host "状態確認：     Get-ScheduledTask -TaskName $taskName ; Get-ScheduledTaskInfo -TaskName $taskName"
Write-Host "解除：         Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
