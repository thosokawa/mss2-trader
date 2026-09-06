<#
.SYNOPSIS
  run_all.ps1 で起動した backend / bridge を止める。Excel は手動で閉じる。
  重複起動した bridge もまとめて終了する。
#>
$ErrorActionPreference = 'SilentlyContinue'

$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match 'app\.main' -or $_.CommandLine -match 'bridge\.py' }

if (-not $procs) {
  Write-Host 'backend / bridge は動いていません。'
} else {
  foreach ($p in $procs) {
    $kind = if ($p.CommandLine -match 'app\.main') { 'backend' } else { 'bridge ' }
    Write-Host ("stop {0} PID {1}" -f $kind, $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force
  }
  Start-Sleep -Milliseconds 500
  $left = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'app\.main' -or $_.CommandLine -match 'bridge\.py' }
  if ($left) { Write-Warning ("まだ残っています: PID {0}" -f ($left.ProcessId -join ', ')) }
  else { Write-Host '停止しました。' }
}
Write-Host 'Excel（rss_bridge.xlsx）は手動で閉じてください。'
