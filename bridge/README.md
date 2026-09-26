# bridge/ — RSS ブリッジ

「価格を運ぶだけ」の薄い層。売買ロジックは一切持たない（backend の live エンジンが判断する）。

1. `build_workbook.py` … 銘柄セット（または `--codes`）から RSS 関数を敷いた `rss_bridge.xlsx` を生成
2. `bridge.py` … MarketSpeed II ログイン後にそのブックを開き、xlwings でセルを
   ポーリング読み取り → backend の `/api/ingest` へ POST
   - `--simulate` … Excel 不要のシミュレーションモード（Mac で疎通確認できる）
3. （P4・未検証）発注リレー: `build_workbook.py --orders` で発注専用ブックを1回作り、
   `bridge.py --orders-workbook <path>` で backend の発注キューを拾って
   `RssStockOrder` に書き込み・結果を報告する（下記「実発注（P4）」参照）

`bridge.py` の `FIELDS` と `build_workbook.py` の `FIELDS` は必ず一致させること。

## Windows 実機セットアップ

```
py -3.13 -m venv .venv
.venv\Scripts\pip install -r ..\requirements-bridge.txt
```

## 手順（Windows）

1. マーケットスピードII を起動しログイン、「RSS」を有効化
2. `python build_workbook.py --set-id 1` で `rss_bridge.xlsx` を生成
3. 生成された `rss_bridge.xlsx` を Excel で開く（RSS 関数が値を返し始める）
4. `python bridge.py` を起動 → backend にティックが流れる

## Mac での疎通確認（Excel 不要）

```
python bridge.py --simulate --codes 7203,6501,9984
python bridge.py --simulate --set-id 1
```

## RSS 項目名（実機確認済み 2026-09）

`=RssMarket(code, "項目名")` で以下が有効（`build_workbook.py --probe <code>` で確認できる）:

- 使用中: `現在値` `出来高` `前日比` `最良買気配値` `最良売気配値`
- 他に有効: `銘柄名称` `現在値時刻` `売買代金` `始値` `高値` `安値` `前日終値`
  `前日比率` `最良売気配数量` `最良買気配数量` `市場コード`
- 無効だったもの: `売気配値1` `買気配値1` `VWAP` `約定回数`

`.xlsx` のままで RSS 関数は自動更新される（`.xlsm` は VBA を足すとき）。

## 未確定事項（楽天証券の公式 RSS リファレンスで要確認）

- リアルタイム登録銘柄数の上限

## 実発注（P4・未検証 — Windows実機での確認が必須）

`RssStockOrder`（国内株式・現物注文）の引数・戻り値・注文一覧系関数は公式リファレンス
（PDF）で確認済み。コードは仕様どおりに実装したが、**Excel/MarketSpeed II との実際の
やり取りは未検証**。試す前に必ず以下を確認すること。

### セットアップ

```powershell
# 1回だけ発注専用ブックを作る（quotesブックと違い自動では作り直さない）
python bridge\build_workbook.py --orders          # bridge\rss_orders.xlsx を生成
# Excel で開いたままにする（run_all.ps1 は存在すれば自動で開く）

# bridge 起動時に発注リレーを有効化
python bridge\bridge.py --workbook rss_bridge.xlsx --orders-workbook rss_orders.xlsx
```

`config.toml` の `[bridge] orders_workbook_path` を設定しておけば `run_all.ps1` が
自動的に発注専用ブックを開き、`--orders-workbook` を付けて bridge を起動する。

### 安全な確認手順（推奨）

1. **MarketSpeed II の「発注機能」をまず OFF のまま**にする
2. `/risk` で ARM し、`/strategies` で1つだけ `mode=live` の戦略を有効化（少額のテスト銘柄で）
3. シグナルが出たら Order が作られ、bridge がセルに書き込む → `RssStockOrder` のセルが
   「発注ロック中（発注を行うには発注機能を有効にしてください）」を返すはず
   → backend 側で `status="rejected"` として正しく検出できるか確認する
4. ここまで確認できて初めて MarketSpeed II の発注機能を ON にする（少額・小口数で）
5. 注文確認画面が出る設定だと `status="cancelled"`（ダイアログを閉じただけ）になる。
   自動化するなら確認画面を出さない設定が必要（MarketSpeed II 側の設定を確認）

### 既知の制限（v1）

- 成行注文のみ（指値・逆指値・信用取引・セット注文は未対応。RssStockOrder の引数は
  すべて用意してあるので拡張は可能）
- 「発注済み」（受理）までしか自動確認しない。**実際に約定したかは `RssOrderStatus` /
  `RssOrderList` を見て手動で確認する**（自動フォローアップは未実装）
- 発注専用ブックの行は使い捨てで300行を使い切ると先頭から上書きする
  （少額試験運用の想定。本格運用するなら行数を増やすかログを別途保存する）
