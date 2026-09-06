<#
.SYNOPSIS
  mss2-trader を一括起動する： backend → Excel(RSS ブック) → bridge。
  Windows ログオン時のタスクに登録して使う（install_autostart.ps1）。
  手動でも実行可: 2つ目の PowerShell で  .\bridge\run_all.ps1

.PARAMETER SetId
  build_workbook.py に渡す銘柄セットID（既定 1）。

.PARAMETER SkipWorkbook
  rss_bridge.xlsx を作り直さない（銘柄セットを変えていないとき / 手動管理のとき）。

.PARAMETER NoBridge
  bridge.py を起動しない（backend と Excel だけ立てる）。

.PARAMETER RssWaitSec
  Excel を開いてから bridge を起動するまでの待ち秒数（RSS が値を返すまでの猶予、既定 25）。
#>
param(
  [int]$SetId = 1,
  [switch]$SkipWorkbook,
  [switch]$NoBridge,
  [int]$RssWaitSec = 25
)

$ErrorActionPreference = 'Stop'
$root     = Split-Path -Parent $PSScriptRoot            # bridge\ の親 = リポジトリルート
$py       = Join-Path $root '.venv\Scripts\python.exe'
$logs     = Join-Path $root 'logs'
$workbook = Join-Path $root 'bridge\rss_bridge.xlsx'
$stamp    = Get-Date -Format 'yyyyMMdd'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

if (-not (Test-Path $py)) {
  throw "venv が見つかりません: $py`n先に初回セットアップ（python -m venv .venv / pip install）を実行してください。"
}

function Test-Backend {
  try { return [bool](Invoke-RestMethod 'http://127.0.0.1:8000/healthz' -TimeoutSec 2).ok }
  catch { return $false }
}

# バックグラウンドで Python を起動し、全出力をログファイルへ（隠しウィンドウ）
# -u = 出力を即フラッシュ（でないとログが溜まるまで書かれない）
function Start-Bg([string]$scriptArgs, [string]$logPath) {
  $env:PYTHONUNBUFFERED = '1'
  Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -WorkingDirectory $root -ArgumentList @(
    '-NoProfile', '-Command', "& '$py' -u $scriptArgs *> '$logPath'"
  )
}

# ---- 1. backend --------------------------------------------------------------
if (Test-Backend) {
  Write-Host '[backend] 既に起動済み'
} else {
  Write-Host '[backend] 起動中...'
  $env:MSS2_RELOAD = '0'
  Start-Bg '-m app.main' (Join-Path $logs "backend-$stamp.log")
  $ready = $false
  foreach ($i in 1..30) { Start-Sleep -Seconds 1; if (Test-Backend) { $ready = $true; break } }
  if (-not $ready) { throw "backend が 30 秒以内に応答しません（$logs\backend-$stamp.log を確認）" }
  Write-Host '[backend] OK  http://127.0.0.1:8000'
}

# ---- 2. RSS ブックを生成 ----------------------------------------------------
if ($SkipWorkbook) {
  Write-Host '[workbook] スキップ'
} else {
  $wbOut = & $py (Join-Path $root 'bridge\build_workbook.py') --set-id $SetId 2>&1
  if ($LASTEXITCODE -eq 0) {
    Write-Host "[workbook] 生成: $workbook"
  } elseif (Test-Path $workbook) {
    Write-Warning '[workbook] 作り直せませんでした（Excel で開いたまま？）。既存のブックを使います。銘柄を変えたら Excel を閉じて再実行してください。'
  } else {
    $wbOut | ForEach-Object { Write-Host $_ }
    throw '[workbook] 生成に失敗し、既存のブックもありません。Excel を閉じて再実行してください。'
  }
}
if (-not (Test-Path $workbook)) { throw "RSS ブックがありません: $workbook" }

# ---- 3. Excel で開く（RSS 関数が評価され始める） --------------------------
$excelHasIt = $false
try {
  $xl = [Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
  foreach ($wb in $xl.Workbooks) { if ($wb.FullName -eq $workbook) { $excelHasIt = $true } }
} catch { }
if ($excelHasIt) {
  Write-Host '[excel] 既に開いています'
} else {
  Write-Host '[excel] rss_bridge.xlsx を開きます（マーケットスピードII にログイン済みであること）'
  Start-Process -FilePath $workbook
  Write-Host "[excel] RSS の初期値待ち $RssWaitSec 秒..."
  Start-Sleep -Seconds $RssWaitSec
}

# ---- 4. bridge -------------------------------------------------------------
if ($NoBridge) {
  Write-Host '[bridge] スキップ（--NoBridge）'
} else {
  $running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -match 'bridge\.py' -and $_.CommandLine -notmatch 'simulate' }
  if ($running) {
    Write-Host "[bridge] 既に起動済み（PID $($running.ProcessId -join ', ')）"
  } else {
    Write-Host '[bridge] 起動中...'
    Start-Bg ("bridge\bridge.py --workbook '{0}'" -f $workbook) (Join-Path $logs "bridge-$stamp.log")
    Write-Host "[bridge] OK  ログ: $logs\bridge-$stamp.log"
  }
}

Write-Host ''
Write-Host '完了。 http://127.0.0.1:8000/live で気配、 /data で足、 /signals でシグナルを確認。'
Write-Host '停止:  .\bridge\stop_all.ps1'
