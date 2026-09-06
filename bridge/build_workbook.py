"""銘柄セットから RSS 関数を敷いた Excel ブック（rss_bridge.xlsx）を生成する。

生成物の quotes シート:

  A列: code   B列: 現在値               C列: 出来高               ...
  2:   7203   =RssMarket(A2,"現在値")   =RssMarket(A2,"出来高")   ...
  3:   6501   ...

Windows でこのブックを Excel で開くと（マーケットスピードII にログイン済みなら）
RSS 関数が値を返し、bridge.py がその表を読んで backend に送る。

  python build_workbook.py --set-id 1
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

from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import SymbolSet, SymbolSetItem  # noqa: E402

# bridge.py の FIELDS と必ず一致させること
FIELDS = ["現在値", "出来高", "前日比", "最良買気配値", "最良売気配値"]


def load_codes(set_id: int) -> list[str]:
    init_db()
    with Session(engine) as s:
        if not s.get(SymbolSet, set_id):
            raise SystemExit(f"symbol set id={set_id} が見つかりません")
        items = s.exec(
            select(SymbolSetItem).where(SymbolSetItem.set_id == set_id).order_by(SymbolSetItem.sort_order)
        ).all()
        return [it.symbol_code for it in items]


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-id", type=int)
    ap.add_argument("--codes", help="カンマ区切り 例: 7203,6501")
    ap.add_argument("--out", default=str(Path(__file__).parent / "rss_bridge.xlsx"))
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.set_id:
        codes = load_codes(args.set_id)
    else:
        raise SystemExit("--set-id か --codes を指定してください")
    build(codes, Path(args.out))


if __name__ == "__main__":
    main()
