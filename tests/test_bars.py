import pandas as pd

from app.bars import ticks_to_bars


def test_ticks_to_bars_5m():
    idx = pd.to_datetime(
        [
            "2026-01-05 09:00:10",
            "2026-01-05 09:01:00",
            "2026-01-05 09:04:59",
            "2026-01-05 09:05:30",
        ]
    )
    ticks = pd.DataFrame({"price": [100, 102, 101, 105], "volume": [10, 5, 5, 20]}, index=idx)
    bars = ticks_to_bars(ticks, "5m")
    assert len(bars) == 2
    first = bars.iloc[0]
    assert first["open"] == 100
    assert first["high"] == 102
    assert first["low"] == 100
    assert first["close"] == 101
    assert first["volume"] == 20
