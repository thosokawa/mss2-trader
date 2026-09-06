# bridge/ — RSS ブリッジ

「価格を運ぶだけ」の薄い層。売買ロジックは一切持たない。

1. `build_workbook.py` … 銘柄セット（または `--codes`）から RSS 関数を敷いた `rss_bridge.xlsx` を生成
2. `bridge.py` … MarketSpeed II ログイン後にそのブックを開き、xlwings でセルを
   ポーリング読み取り → backend の `/api/ingest` へ POST
   - `--simulate` … Excel 不要のシミュレーションモード（Mac で疎通確認できる）
3. （P4）発注指令を受け取り、指定セルに `RssOrder` を書いて結果を読み戻す

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
- 発注系関数（`RssOrder` ほか）の正確な引数・戻り値、RSS注文の有効化手順（P4）
