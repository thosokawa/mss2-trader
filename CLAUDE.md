# CLAUDE.md — mss2-trader

Claude Code がこのディレクトリで起動すると自動で読み込むプロジェクトブリーフ。
Mac（開発）・Windows（本番稼働）どちらで開いても、まずこれで全体像を掴む。
会話の生の履歴はマシン間で同期されないので、経緯や注意点はここと各 README に書く。

## 何のプロジェクトか

楽天証券 MarketSpeed II の RSS（Excel連携）で株価を取得し、自作の売買ロジックで
シグナル通知・将来は自動売買まで行う個人用ツール。Mac で開発、Windows 1台
（MarketSpeed II + Excel/RSS + backend + Web UI 同居）で本番稼働。

## 全体構成

```
[Windows] MarketSpeed II →(RSS関数)→ Excel(rss_bridge.xlsx)
                                          │ xlwings
                                     bridge/bridge.py ──POST──> backend /api/ingest
                                                        <── 発注リレー ── /api/orders/*
[backend = app/] FastAPI + SQLite(SQLModel)。DB は naive UTC、UI は JST 表示
  ingest → bars(足集約) → strategy.on_bar() → Signal → notify / paper擬似約定 / 実発注
[Mac] 同じ strategy.on_bar() を過去足で回して検証（app/engine/backtest.py）
```

## フェーズ状況（詳細は README.md、使い方は `/help` 画面）

P0〜P3 済（雛形・バックテスト・RSSブリッジ・tick→足集約・ライブ気配・Slack通知・
ペーパートレード）。UI改善（用語集・パラメータ入力フォームの自動生成）、損切り/利確
（stop_loss_pct/take_profit_pct、全戦略共通）も実装済み。戦略は7種類
（トレンドフォロー/逆張り/ブレイクアウト/フィルタ付き、詳細 `app/strategy/README.md`）。

**P4（実発注）はコード実装済みだが Windows実機で未検証。** リスク管理は
`config.trading.enabled` と `/risk` 画面の ARMED トグルの両方が true でないと
発注しない設計（二重の安全弁、backend 再起動で ARMED は自動 OFF）。詳細・安全な
確認手順は `bridge/README.md`「実発注（P4）」を必ず読むこと。

## 開発上の重要な注意点

- **PowerShell スクリプト（`bridge/*.ps1`）は UTF-8 BOM + CRLF 必須。**
  BOM 無しだと Windows PowerShell 5.1 が cp932 として読み、日本語コメントが
  文字化けして構文エラーになる。Write/Edit ツールは BOM 無し UTF-8 で書くので、
  `.ps1` を編集したら BOM を付け直す（`.gitattributes` で `eol=crlf` 指定済み）。
- **DB マイグレーション**: `SQLModel.metadata.create_all()` は新規テーブルは
  作るが、既存テーブルへの列追加はしない。既存テーブル（`Order` 等、P0 で空のまま
  作成済み）にモデル側で列を追加したら `app/db.py` の `_ADD_COLUMNS` に
  `(table, column, sqlite型)` を追記すること（`ALTER TABLE` で安全に追加、
  既存データは失われない）。
- **`git push` はこのリポジトリでは許可設定済み**（`.claude/settings.local.json`、
  gitignore 済み）。基本は Mac で実装 → push、Windows は `git pull` で受け取る運用。
- Windows 側の運用は `bridge/run_all.ps1`（一括起動）/ `stop_all.ps1` /
  `install_autostart.ps1`（スタートアップ登録、管理者権限不要）。
- コミット前に必ず: `.venv/bin/pytest -q` と
  `.venv/bin/ruff check app/ tests/ bridge/ scripts/` の両方を通す。

## P4（実発注）に手を入れるときの注意

- RSS 発注関数（`RssStockOrder`/`RssOrderStatus`/`RssOrderList` 等）の仕様は
  楽天証券公式リファレンス PDF で確認済み（PDF 自体は著作物のため `.gitignore` 済みで
  リポジトリには無い。仕様の要点は `bridge/README.md` に転記済み）。
- 実弾が絡む。変更・拡張するときは ARMED / `config.trading.enabled` の二重ゲートを
  弱めない。試すときは必ず「まず MarketSpeed II の発注機能を OFF のまま、
  `RssStockOrder` が『発注ロック中』を返し backend 側で `rejected` として正しく
  検出できることを確認してから有効化する」手順を踏む（`bridge/README.md` 参照）。
- 現状の制限: 成行注文のみ（指値・信用・逆指値は未対応）。「発注済み（受理）」までしか
  自動確認しない — 実際の約定確認（`RssOrderStatus`/`RssOrderList` の追跡）は未実装。
