# mss2-trader

楽天証券 マーケットスピードII RSS（Excel連携）で株価を取り込み、自作の売買ロジックで
**シグナル通知**、将来的に**自動売買**まで行う個人用ツール。

- 開発・バックテスト: Mac
- 本番稼働: Windows 1台（MarketSpeed II + Excel/RSS + backend + Web UI を同居）
- 言語: Python / Web UI: FastAPI + HTMX / DB: SQLite

## アーキテクチャ

```
[Windows]
  MarketSpeed II ──RSS関数──> Excel(rss_bridge.xlsm)
                                   │ xlwings
                              bridge/bridge.py ──POST──> backend /api/ingest
                                                  <──発注指令── (P4)
[backend = app/]  FastAPI
  ingest → bars(足集約) → strategy.on_bar() → Signal
     → notify(Slack) / (mode=live かつ ARMED なら) broker.place()
  Web UI: 銘柄セット / データ / バックテスト / 履歴 / シグナル
[Mac] 同じ strategy.on_bar() を過去足で回して検証（engine/backtest.py）
```

売買ロジックは `app/strategy/` に `Strategy.on_bar()` を実装するだけ。
バックテストとライブが同じコードを呼ぶので挙動が一致する。

## フェーズ

| | 内容 | 状態 |
|---|---|---|
| P0 | 雛形・DB・銘柄セットUI・履歴取得・バックテスター | 済 |
| P1 | `bridge/` 実装、tick→足 集約、ライブ気配画面 | 済（Windows 実機で RSS 疎通確認まで完了 2026-09） |
| P2 | live エンジンで足確定→Slack 通知（＝通知だけ完成） | 済（Slack Webhook を設定して実疎通確認が残り） |
| P3 | PaperBroker でペーパートレード、成績表示 | ← 次 |
| P4 | RssBroker で実発注、risk.py 完全実装、少額試験運用 | 未 |

## セットアップ（Mac）

```bash
cd /Users/t.hosokawa/Documents/project/mss2-trader
/usr/local/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/config.example.toml config/config.toml   # 任意（無くても example で動く）
```

## 使い方

```bash
# Web UI（バックグラウンドで tick→足 集約ループ と live シグナルループも回る）
.venv/bin/python -m app.main       # http://127.0.0.1:8000

# 過去足を取得（yfinance。日本株は自動で .T を付与）
.venv/bin/python scripts/fetch_history.py 7203 6501 --interval 5m --period 60d

# CLI でバックテスト
.venv/bin/python scripts/backtest.py 7203 --interval 5m --fast 5 --slow 20
```

### ライブ気配パイプラインの動作確認（Mac・Excel不要）

```bash
# 1. Web UI で銘柄セットを作り、銘柄を追加（または下の --codes を使う）
# 2. シミュレーションのブリッジを起動（ランダムウォークの気配を投げ続ける）
.venv/bin/python bridge/bridge.py --simulate --codes 7203,6501,9984
#   または  --set-id 1
```

→ Web UI の **ライブ** 画面に気配が流れ、数分で **データ** 画面に 1分足/5分足が溜まる。

### Windows 実機（RSS）

初回セットアップ:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-bridge.txt
Copy-Item config\config.example.toml config\config.toml
# config.toml の [bridge] workbook_path を .\bridge\rss_bridge.xlsx の絶対パスに
```

日常運用（PowerShell を2つ使う）:

```powershell
# ターミナル1: backend（起動しっぱなし）
.venv\Scripts\python -m app.main                   # http://127.0.0.1:8000

# 銘柄セットは Web UI (/symbol-sets) で編集。変更したらブックを作り直す:
.venv\Scripts\python bridge\build_workbook.py --set-id 1     # bridge\rss_bridge.xlsx

# マーケットスピードII にログイン（RSS 有効）→ rss_bridge.xlsx を Excel で開く

# ターミナル2: bridge（xlwings で読み取り → backend へ）
.venv\Scripts\python bridge\bridge.py

# RSS 項目名を実機確認したいとき:
.venv\Scripts\python bridge\build_workbook.py --probe 7203  # rss_probe.xlsx を Excel で開く
.venv\Scripts\python bridge\bridge.py --dump                # quotes シートの生値を表示
```

Web UI:
- **銘柄セット** … RSS で監視する銘柄グループ。`build_workbook.py` / `bridge.py --set-id` が参照
- **ライブ** … bridge から届く最新気配と生存監視（5秒自動更新、30秒無受信で「遅延」）
- **データ** … 蓄積済み足のカバレッジ（時刻は JST 表示、DB は UTC）
- **バックテスト / 履歴** … 戦略検証
- **戦略** … live エンジンで回すロジックの登録。有効化すると `live_interval_sec` ごとに
  対象銘柄セットの確定足へ `on_bar()` を流し、シグナルを記録して Slack 通知（発注はしない）。
  有効化した時点より後の足だけが対象
- **シグナル** … live エンジンが記録したシグナル履歴

### Slack 通知の設定

`config/config.toml` の `[notify]` に Incoming Webhook URL を入れて `dry_run = false`。
未設定・`dry_run = true` のときは送信せずログ出力のみ（`[notify dry_run] ...`）。

## テスト

```bash
.venv/bin/pytest
```

## 注意（現時点で未確定・実装前）

- RSS の `RssMarket` フィールド名、登録銘柄数上限、発注系関数の仕様は
  楽天証券の公式 RSS リファレンスで確認が必要（`bridge/README.md`）
- 発注機能（P4）は `config.trading.enabled=true` かつ 戦略 `mode=live` かつ
  UI で明示的に ARMED にしたときのみ動作。既定は全部オフ。
- yfinance の分足は取得期間に制約あり（1m=7日 / 5m=60日）。恒久ヒストリは
  P1 以降 RSS ブリッジで自前蓄積する。
