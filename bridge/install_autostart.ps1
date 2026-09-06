<#
.SYNOPSIS
  Windows ログオン時に run_all.ps1 を自動実行する。
  スタートアップフォルダにショートカットを置くだけなので管理者権限は不要。

.PARAMETER SetId
  run_all.ps1 に渡す銘柄セットID（既定 1）。

.PARAMETER Uninstall
  登録を解除する（ショートカットを削除）。

.EXAMPLE
  .\bridge\install_autostart.ps1
  .\bridge\install_autostart.ps1 -SetId 2
  .\bridge\install_autostart.ps1 -Uninstall
#>
param(
  [int]$SetId = 1,
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent $PSScriptRoot
$script  = Join-Path $PSScriptRoot 'run_all.ps1'
$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup 'mss2-trader.lnk'

if ($Uninstall) {
  if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "解除しました: $lnk" }
  else { Write-Host '登録されていません。' }
  return
}

if (-not (Test-Path $script)) { throw "run_all.ps1 が見つかりません: $script" }

$psExe = (Get-Command powershell.exe).Source
$ws = New-Object -ComObject WScript.Shell
$s  = $ws.CreateShortcut($lnk)
$s.TargetPath       = $psExe
$s.Arguments        = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -SetId {1}' -f $script, $SetId)
$s.WorkingDirectory = $root
$s.WindowStyle      = 7          # 7 = 最小化
$s.Description       = 'mss2-trader 自動起動 (backend + Excel/RSS + bridge)'
$s.Save()

Write-Host "登録しました: $lnk"
Write-Host "  → 次回ログオン時に run_all.ps1 -SetId $SetId が走ります（マーケットスピードII のログインだけ手動）"
Write-Host "今すぐ試す: & `"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"$script`""
Write-Host "解除:       .\bridge\install_autostart.ps1 -Uninstall"
