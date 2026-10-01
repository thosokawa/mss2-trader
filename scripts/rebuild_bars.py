"""残っている tick から足（1分足・5分足）を作り直す。

2026-10-01 まで、足の集計（app/aggregator.py）が3時間の窓の先頭の足を途中からの tick で
毎回上書きしていたため、RSS 由来の足は始値・高値・安値・出来高が「足の最後の数秒」だけの
値になっていた（終値は正しい）。tick は残っているので、そこから正しく作り直せる。

  python scripts/rebuild_bars.py                 # 対象の期間・銘柄と、作り直す前後の例を表示するだけ
  python scripts/rebuild_bars.py --apply         # 実際に作り直す（tick が残っている全期間）
  python scripts/rebuild_bars.py --start 2026-09-28 --apply

backend を新しいコードで再起動してから実行すること（古い集計が動いていると直近3時間の足をまた壊す）。
tick は既定で30日より古いものが消える（app/retention.py）ので、それより前の足は作り直せない。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

from app.aggregator import _complete_bars, rebuild_bars  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Bar, Tick  # noqa: E402

JST = timedelta(hours=9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="JST の日付（例 2026-09-28）。省略で tick が残っている最初の日")
    ap.add_argument("--apply", action="store_true", help="実際に作り直す（無指定なら確認の表示のみ）")
    args = ap.parse_args()
    init_db()
    with Session(engine) as s:
        first_tick, last_tick = s.exec(select(func.min(Tick.ts), func.max(Tick.ts))).one()
        if first_tick is None:
            print("tick がありません。")
            return
        start = (datetime.fromisoformat(args.start) - JST) if args.start else first_tick
        end = last_tick
        codes = sorted(s.exec(select(Tick.symbol_code).where(Tick.ts >= start).distinct()).all())
        n_rss = s.exec(select(func.count()).select_from(Bar)
                       .where(Bar.source == "rss", Bar.ts >= start)).one()
        print(f"期間: {start + JST:%Y-%m-%d %H:%M} 〜 {end + JST:%Y-%m-%d %H:%M}（JST）")
        print(f"銘柄: {len(codes)} 件 {', '.join(codes)}")
        print(f"今ある RSS 由来の足: {n_rss} 本")

        # 例: 最初の銘柄の、10:00 の足がある直近の日の 10:00〜10:15 の5分足を、今の値と作り直した値で並べる
        code = codes[0]
        ten = s.exec(select(func.max(Bar.ts)).where(
            Bar.symbol_code == code, Bar.timeframe == "5m", Bar.ts >= start,
            func.strftime("%H:%M", Bar.ts) == "01:00")).one()
        last_day = (pd.Timestamp(ten or end) + JST).normalize()
        a = (last_day + pd.Timedelta(hours=10) - JST).to_pydatetime()
        b = a + timedelta(minutes=15)
        new = _complete_bars(s, code, a, b, ("5m",)).get("5m")
        old = s.exec(select(Bar).where(Bar.symbol_code == code, Bar.timeframe == "5m",
                                       Bar.ts >= a, Bar.ts < b).order_by(Bar.ts)).all()
        print(f"\n例 {code} 5分足（今 → 作り直し後）:")
        for o in old:
            n = new.loc[pd.Timestamp(o.ts)] if new is not None and pd.Timestamp(o.ts) in new.index else None
            after = (f"始{n.open:g} 高{n.high:g} 安{n.low:g} 終{n.close:g} 出来高{n.volume:,.0f}"
                     if n is not None else "（作り直しの対象外）")
            print(f"  {o.ts + JST:%m/%d %H:%M} 始{o.open:g} 高{o.high:g} 安{o.low:g} 終{o.close:g} "
                  f"出来高{o.volume:,.0f}  →  {after}")
        if not args.apply:
            print("\n作り直すには --apply を付けて実行してください。")
            return
        n = rebuild_bars(s, start, end, codes=codes)
        print(f"\n{n} 本の足を作り直しました。")


if __name__ == "__main__":
    main()
