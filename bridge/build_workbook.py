"""銘柄セットから RSS 関数を敷いた Excel ブック（rss_bridge.xlsx）を生成する。

生成物の quotes シート:

  A列: code   B列: 現在値               C列: 出来高               ...
  2:   7203   =RssMarket(A2,"現在値")   =RssMarket(A2,"出来高")   ...
  3:   6501   ...

Windows でこのブックを Excel で開くと（マーケットスピードII にログイン済みなら）
RSS 関数が値を返し、bridge.py がその表を読んで backend に送る。

  python build_workbook.py --auto        # 有効な戦略の対象銘柄 ∪ 監視銘柄（run_all.ps1 が使う）
  python build_workbook.py --codes 7203,6501,9984 --out rss_bridge.xlsx

※ フィールド名（"現在値" 等）は楽天証券公式の RSS リファレンスで要確認。
   VBA を足したい場合は Excel で .xlsm として保存し直す。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rss_layout import (  # noqa: E402
    N_ROWS,
    ORDER_SHEETS,
    POSITION_ITEMS,
    POSITIONS_SHEET,
    col_letter,
    order_formula,
    positions_formula,
)
from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402

# bridge.py の FIELDS と必ず一致させること
FIELDS = ["現在値", "出来高", "前日比", "最良買気配値", "最良売気配値"]

# --probe 用。RssMarket の項目名候補を総当たりで並べ、Windows で開いてどれが
# 数値を返すか目視確認する（楽天証券 RSS リファレンス未確定のため）。
PROBE_FIELDS = [
    "銘柄名称", "現在値", "現在値時刻", "出来高", "売買代金",
    "始値", "高値", "安値", "前日終値", "前日比", "前日比率",
    "最良売気配値", "最良買気配値",
    "最良売気配値1", "最良買気配値1",
    "売気配値1", "買気配値1",
    "最良売気配数量", "最良買気配数量",
    "VWAP", "約定回数", "市場コード",
]



# --probe-account 用。口座系一覧関数の取得項目（公式オンラインヘルプ「取得項目一覧」の表記どおり）。
# 値の形式（建日が日付か文字列か、建市場が「東証」かコードか等）を実機で確認するためのもの。
ACCOUNT_PROBE_SHEETS = {
    "positions": ("RssMarginPositionList", [
        "銘柄コード", "銘柄名称", "口座区分", "建市場", "信用区分", "弁済期限", "売買", "建玉数量",
        "発注数量", "建値", "建日", "最終返済日", "時価", "前日比", "前日比率", "時価評価額",
        "評価損益額", "評価損益率", "保証金率", "現金保証金率",
    ]),
    "executions": ("RssExecutionList", [
        "約定日", "受渡日", "銘柄コード", "銘柄名称", "口座区分", "市場名称", "信用区分", "弁済期限",
        "取引", "売買", "約定数量", "約定単価", "約定代金", "税区分", "特別空売り料",
    ]),
    "orders": ("RssOrderList", [
        "注文番号", "受付No", "通常注文状況", "逆指値注文状況", "アルゴ注文状況", "銘柄コード",
        "銘柄名称", "口座区分", "市場名称", "信用区分", "弁済期限", "発注/受注日時", "売買", "取引",
        "執行条件", "注文期限", "注文数量", "約定数量", "注文単価", "注文区分", "逆指値条件",
        "セット注文", "セット注文条件", "税区分", "注文失効日時", "注文失効理由", "入力経路",
        "アルゴ注文条件", "SOR判定時刻", "SOR判定時主市場情報/対象外理由",
    ]),
}


def auto_codes() -> list[str]:
    """株価を取り込む銘柄 = 有効な戦略の対象銘柄 ∪ 監視銘柄（app.symbols.quote_codes）。"""
    from app.symbols import quote_codes

    init_db()
    with Session(engine) as s:
        return quote_codes(s)


def build(codes: list[str], out_path: Path) -> None:
    if not codes:
        raise SystemExit("銘柄が空です")
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise SystemExit("openpyxl が必要です: pip install openpyxl") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "quotes"
    ws.append(["code", *FIELDS])
    for i, code in enumerate(codes, start=2):
        ws.cell(row=i, column=1, value=str(code))
        for j, f in enumerate(FIELDS, start=2):
            ws.cell(row=i, column=j, value=f'=RssMarket(A{i},"{f}")')
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 10
    for col in ("B", "C", "D", "E", "F"):
        ws.column_dimensions[col].width = 14

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"生成: {out_path}  ({len(codes)} 銘柄)")
    print("Windows の Excel で開くと RSS 関数が評価されます（マーケットスピードII ログイン必須）。")


def build_probe(code: str, out_path: Path) -> None:
    """1銘柄ぶん、項目名候補を縦に並べたブックを生成する。"""
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise SystemExit("openpyxl が必要です: pip install openpyxl") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "probe"
    ws.append(["項目名", f'RssMarket("{code}", 項目名)'])
    for i, f in enumerate(PROBE_FIELDS, start=2):
        ws.cell(row=i, column=1, value=f)
        ws.cell(row=i, column=2, value=f'=RssMarket("{code}","{f}")')
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 22
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"生成: {out_path}  (probe: {code}, {len(PROBE_FIELDS)} 項目)")
    print("Excel で開き、B列に数値/文字が返る項目名をメモ → FIELDS を修正する。")


def build_account_probe(out_path: Path) -> None:
    """信用建玉一覧・約定一覧・注文一覧を表示するブック（信用返済の実装に必要な値の形式確認用）。"""
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise SystemExit("openpyxl が必要です: pip install openpyxl") from e

    wb = Workbook()
    wb.remove(wb.active)
    for sheet, (func, items) in ACCOUNT_PROBE_SHEETS.items():
        ws = wb.create_sheet(sheet)
        ws.append(items)
        last = get_column_letter(len(items))
        # 1行目の項目名をヘッダー行として渡し、2行目から一覧が展開される
        ws["A2"] = f"={func}($A$1:${last}$1)"
        for c in range(1, len(items) + 1):
            ws.column_dimensions[get_column_letter(c)].width = 12
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"生成: {out_path}  (シート: {', '.join(ACCOUNT_PROBE_SHEETS)})")
    print("Excel で開き、信用建玉・約定・注文が1件以上ある状態で値を確認する:")
    print("  python bridge\\bridge.py --dump --workbook bridge\\rss_account_probe.xlsx --sheet positions")


def build_orders(out_path: Path, n_rows: int = N_ROWS) -> None:
    """発注専用ブック（1回だけ作って使い回す。quotes ブックとは別ファイル）。

    quotes ブックは銘柄セットを変えるたびに作り直すが、orders ブックは
    発注中の状態を持つので自動では作り直さない・書き換えない設計にしてある。
    シート構成は rss_layout.py（現物・信用新規・信用返済・信用建玉一覧）。既存のブックに
    信用のシートが無ければ bridge.py が起動時に追加するので、作り直しは不要。
    """
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise SystemExit("openpyxl が必要です: pip install openpyxl") from e

    wb = Workbook()
    wb.remove(wb.active)
    for sheet, (_func, args) in ORDER_SHEETS.items():
        ws = wb.create_sheet(sheet)
        ws.append([*args, "ステータス"])
        n_args = len(args)
        for row in range(2, n_rows + 2):
            ws.cell(row=row, column=n_args + 1, value=order_formula(sheet, row))
        for c in range(1, n_args + 1):
            ws.column_dimensions[col_letter(c)].width = 11
        ws.column_dimensions[col_letter(n_args + 1)].width = 45
        ws.freeze_panes = "A2"
    ws = wb.create_sheet(POSITIONS_SHEET)
    ws.append(POSITION_ITEMS)
    ws["A2"] = positions_formula()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"生成: {out_path}  (シート: {', '.join([*ORDER_SHEETS, POSITIONS_SHEET])}、各{n_rows}行)")
    print("このファイルは1回作って Excel で開いたままにする（quotes ブックと違い自動では作り直さない）。")
    print("※ まず MarketSpeed II 側の「発注機能」を OFF のままテストし、「発注ロック中」が"
          "正しく検出できることを確認してから有効化すること。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="有効な戦略の対象銘柄 ∪ 監視銘柄でブックを作る（run_all.ps1 が使う）")
    ap.add_argument("--codes", help="カンマ区切り 例: 7203,6501")
    ap.add_argument("--probe", help="項目名の実地確認用ブックを作る（1銘柄コード）")
    ap.add_argument("--orders", action="store_true", help="発注用ブック（rss_orders.xlsx）を作る")
    ap.add_argument("--probe-account", action="store_true",
                    help="信用建玉/約定/注文一覧の確認用ブック（rss_account_probe.xlsx）を作る")
    ap.add_argument("--out", default=str(Path(__file__).parent / "rss_bridge.xlsx"))
    args = ap.parse_args()

    if args.orders:
        default_out = Path(__file__).parent / "rss_orders.xlsx"
        out = Path(args.out) if args.out != str(Path(__file__).parent / "rss_bridge.xlsx") else default_out
        build_orders(out)
        return

    if args.probe_account:
        default_out = Path(__file__).parent / "rss_account_probe.xlsx"
        out = Path(args.out) if args.out != str(Path(__file__).parent / "rss_bridge.xlsx") else default_out
        build_account_probe(out)
        return

    if args.probe:
        default_out = Path(__file__).parent / "rss_probe.xlsx"
        out = Path(args.out) if args.out != str(Path(__file__).parent / "rss_bridge.xlsx") else default_out
        build_probe(args.probe.strip(), out)
        return

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.auto:
        codes = auto_codes()
        if not codes:
            raise SystemExit("取り込む銘柄がありません（有効な戦略の対象銘柄も監視銘柄も空）。"
                             "/strategies で戦略を有効にするか /live で監視銘柄を追加してください")
    else:
        raise SystemExit("--auto か --codes か --probe か --orders を指定してください")
    build(codes, Path(args.out))


if __name__ == "__main__":
    main()
