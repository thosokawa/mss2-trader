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

**P4（実発注）は実機でステージ1まで確認済み（2026-09-28）。** MarketSpeed II の
発注機能 OFF のまま、シグナル → RiskEngine → Order → bridge → `RssStockOrder` →
「発注ロック中」→ backend で `rejected` の一連が動くことを確認した。**発注機能 ON
（実弾・ステージ2）はまだ未検証。** リスク管理は `config.trading.enabled` と `/risk`
画面の ARMED トグルの両方が true でないと発注しない設計（二重の安全弁、backend
再起動で ARMED は自動 OFF）。詳細・安全な確認手順は `bridge/README.md`「実発注（P4）」
を必ず読むこと。

**銘柄の指定（2026-09-29 に銘柄セットを廃止）**: 戦略ごとに対象銘柄を `Strategy.symbols`（カンマ区切り）で
直接指定。RSS で株価を取り込む銘柄は「有効な戦略の対象銘柄 ∪ 監視銘柄（`Symbol.watch`、/live で編集）」
（`app/symbols.quote_codes`）。bridge が30秒ごとに `/api/quote-codes` を見て `rss_bridge.xlsx` の quotes
シートを書き換える。`run_all.ps1` は `build_workbook.py --auto`（`-SetId` は登録済みスタートアップとの互換の
ため受け取るだけ）。SymbolSet テーブルは旧 DB の移行（`app/db.py _migrate_symbol_sets`）用に定義だけ残す。
建玉を持つ live 戦略は削除・銘柄の除外ができない（`/risk` で手仕舞い or「手動決済を記録」してから）。

戦略共通パラメータ: 損切り/利確（`app/engine/stops.py`）、大引けをまたぐか
（`hold_overnight`、`app/engine/eod.py`）、売買方向（`direction`=long/short/both。旧 `allow_short`）、取引区分（`trade_type`=cash/margin）。
通知は Slack / メール（Gmail SMTP、`app/notify.py`）。

**信用取引（2026-09-29 から新規建てを停止中）**: RSS の一覧関数（建玉一覧など）は MarketSpeed II の
更新アイコンを押さないと最新にならず、bridge が返済に必要な建単価を取れないと判明（bot の返済が失敗）。
`live.MARGIN_ORDERS_SUPPORTED=False` で信用の新規建ては見送り（現物は動く）。詳細 `bridge/README.md`。
空売りの仕組み自体は戦略・バックテスト・ペーパー・live・bridge まで対応
（`Position.qty` マイナス＝売建、Order.side は BUY/SHORT/EXIT/COVER、Order.trade_type=cash/margin）。
bridge は `RssMarginOpenOrder`（信用区分 4=いちにち / 2=一般無期限）と `RssMarginCloseOrder`
（返済。建日・建単価・建市場は `rss_orders.xlsx` の `margin_positions` シート＝`RssMarginPositionList`
から、銘柄・売買・信用区分・口座区分・**建日＝bot の新規建ての日（Order.open_date）**が一致する建玉を
選ぶ）で発注する。シート構成は `bridge/rss_layout.py`。建玉一覧の値の形式は 2026-09-28 に実機で確認済み
（`tests/test_bridge_margin.py` に実データ）。止めたいときは `app/engine/live.py` の
`MARGIN_ORDERS_SUPPORTED=False`（信用の新規建てだけ止まり、手仕舞いは出る）。

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
- **時刻は DB・足の ts とも naive UTC、取引時間や大引けの判定は JST。** 比較するときは
  必ず +9h してから（ステージ1検証で RiskEngine が UTC を JST として比べて全シグナルを
  「取引時間外」にしていたバグがあった）。テストで取引時間を `00:00-23:59` にするとこの種の
  ずれを見逃すので、JST の時刻で書いたテストも置くこと。
- live の発注は足の確定から `trading.max_signal_age_sec`（既定120秒）を超えたシグナルでは
  行わない（backend 停止後の追いつき評価対策）。live のテストで足をまとめて入れると途中の
  シグナルが「古い」扱いになるので、`run_once(now=...)` を渡し足を1本ずつ評価する。
- Windows 機の git 作者は `thosokawa <t.hosokawa.pc@gmail.com>`（リポジトリローカル設定）。
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
  代わりに受理（`sent`）を「`Order.ref_price`＝シグナル価格で約定」とみなして建玉・損切り・
  日次損益を回している（概算）。`timeout`/`error` は自動 DISARM。詳細は `bridge/README.md`。
