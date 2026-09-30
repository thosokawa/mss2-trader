"""約定のない（出来高 0 の）RSS 由来の足を消す。

旧集計（2026-09-30 まで）は、取引時間外や Excel と MarketSpeed II の接続が切れている間も
RSS が返し続ける同じ値から足を作っていた（夜通し・休日・接続断の間の横ばいの足）。
それがバックテストや live の指標を歪めるので消す。今の集計は約定があった tick からしか
足を作らないので、source='rss' で出来高 0 の足は全部この残骸。yfinance の足には触れない。

  python scripts/clean_frozen_bars.py          # 件数を見るだけ
  python scripts/clean_frozen_bars.py --apply  # 実際に消す

backend を新しいコードで再起動してから実行すること（古い集計が動いていると作り直される）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlmodel import Session, delete, func, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Bar  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に削除する（無指定なら件数表示のみ）")
    args = ap.parse_args()
    init_db()
    junk = (Bar.source == "rss", Bar.volume == 0)
    with Session(engine) as s:
        rows = s.exec(
            select(Bar.symbol_code, Bar.timeframe, func.count()).where(*junk)
            .group_by(Bar.symbol_code, Bar.timeframe).order_by(Bar.symbol_code, Bar.timeframe)
        ).all()
        total = sum(n for _, _, n in rows)
        for code, tf, n in rows:
            print(f"{code} {tf}: {n}")
        print(f"合計 {total} 本（source=rss・出来高 0）")
        if not args.apply:
            print("削除するには --apply を付けて実行してください。")
            return
        s.exec(delete(Bar).where(*junk))
        s.commit()
        print(f"{total} 本を削除しました。")


if __name__ == "__main__":
    main()
