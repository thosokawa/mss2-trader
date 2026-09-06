<#
.SYNOPSIS
  run_all.ps1 で起動した backend / bridge を止める。Excel は手動で閉じる。
#>
$ErrorActionPreference = 'SilentlyContinue'

$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match 'app\.main|bridge\\bridge\.py|bridge/bridge\.py' }

if (-not $procs) {
  Write-Host 'backend / bridge は動いていません。'
} else {
  foreach ($p in $procs) {
    Write-Host "kill PID $($p.ProcessId): $($p.CommandLine)"
    Stop-Process -Id $p.ProcessId -Force
  }
}
Write-Host 'Excel（rss_bridge.xlsx）は手動で閉じてください。'
