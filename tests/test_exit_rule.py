"""全戦略共通の決済条件（exit_rule）: エントリー条件の反転シグナル（既定）/ SMAクロス。"""
import numpy as np
import pandas as pd

from app.engine.backtest import run_backtest
from app.strategy.base import Context, Position, Signal, Strategy, exit_rule_of
from app.strategy.registry import BUILTIN, UNIVERSAL_DEFAULTS, builtin_param_meta


def _bars(closes):
    idx = pd.date_range("2026-01-05 00:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


class _ExitEveryBar(Strategy):
    """建玉があれば毎足 EXIT、無ければ BUY を出す（決済条件の効き方を見るためのダミー）。"""

    default_params = {"qty": 100}

    def on_bar(self, ctx):
        if ctx.position.is_flat:
            return Signal("BUY", 100, reason="買い")
        return Signal("EXIT", reason="反転")


# 上昇 → 下落（短期SMAが長期SMAを下抜ける）
UP_DOWN = list(np.arange(100, 160, 1.0)) + list(np.arange(159, 120, -1.0))


def _ctx(closes, pos):
    b = _bars(closes)
    return Context(symbol="X", now=b.index[-1].to_pydatetime(), bars=b, position=pos)


def test_default_is_signal():
    assert exit_rule_of({}) == "signal"
    assert exit_rule_of({"exit_rule": "sma_cross"}) == "sma_cross"
    # MACD＋上位足トレンドの独自パラメータ exit_on_trend_flip は決済条件とは無関係
    assert exit_rule_of({"exit_on_trend_flip": True}) == "signal"
    assert UNIVERSAL_DEFAULTS["exit_rule"] == "signal"


def test_signal_rule_uses_strategy_exit():
    sig = _ExitEveryBar({}).decide(_ctx(UP_DOWN[:65], Position(qty=100, avg_price=150)))
    assert sig.side == "EXIT" and sig.reason == "反転"


def test_sma_cross_ignores_strategy_exit_and_exits_on_dead_cross():
    strat = _ExitEveryBar({"exit_rule": "sma_cross", "exit_sma_fast": 5, "exit_sma_slow": 20})
    long_pos = Position(qty=100, avg_price=150)
    # まだ短期>長期 → 戦略の EXIT は使わないので何も出ない
    assert strat.decide(_ctx(UP_DOWN[:62], long_pos)) is None
    # デッドクロスした足を探す
    fired = [n for n in range(62, len(UP_DOWN) + 1) if strat.decide(_ctx(UP_DOWN[:n], long_pos)) is not None]
    assert fired, "デッドクロスで手仕舞いが出ない"
    sig = strat.decide(_ctx(UP_DOWN[:fired[0]], long_pos))
    assert sig.side == "EXIT" and "SMA5/20 デッドクロス" in sig.reason
    assert len(fired) == 1  # クロスした足だけ（下にいる間ずっと出すわけではない）
    # 建玉が無ければ新規建ては戦略どおり
    assert strat.decide(_ctx(UP_DOWN[:70], Position())).side == "BUY"


def test_sma_cross_covers_short_on_golden_cross():
    down_up = list(np.arange(160, 100, -1.0)) + list(np.arange(101, 140, 1.0))
    params = {"exit_rule": "sma_cross", "exit_sma_fast": 5, "exit_sma_slow": 20, "direction": "both"}
    strat = _ExitEveryBar(params)
    short_pos = Position(qty=-100, avg_price=130)
    sigs = [strat.decide(_ctx(down_up[:n], short_pos)) for n in range(40, len(down_up) + 1)]
    hits = [s for s in sigs if s is not None]
    assert len(hits) == 1 and hits[0].side == "EXIT" and "ゴールデンクロス" in hits[0].reason


def test_backtest_with_sma_cross_exit():
    strat = _ExitEveryBar({"exit_rule": "sma_cross", "exit_sma_fast": 5, "exit_sma_slow": 20})
    res = run_backtest(strat, _bars(UP_DOWN), "X")
    assert len(res.trades) == 1
    assert "デッドクロス" in res.trades[0].reason_out


def test_every_strategy_gets_exit_rule_select():
    metas = builtin_param_meta()
    for cp in BUILTIN.values():
        m = metas[cp]["exit_rule"]
        assert m["label"] == "決済条件" and m["choices"] == ["signal", "sma_cross"]
        assert m["choice_labels"] == {"signal": "エントリー条件の反転シグナル", "sma_cross": "SMAクロス"}


def test_exit_sma_fields_only_shown_for_sma_cross():
    metas = builtin_param_meta()
    for cp in BUILTIN.values():
        assert metas[cp]["exit_rule"].get("wide") is True
        for k in ("exit_sma_fast", "exit_sma_slow"):
            assert metas[cp][k]["show_if"] == {"exit_rule": "sma_cross"}


def test_param_fields_keep_defined_order():
    """入力欄は アルファベット順ではなく 戦略固有 → 株数 → 共通パラメータ の順。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.strategy.registry import builtin_params

    cp = BUILTIN["トレンド×RSI出戻り"]
    keys = list(builtin_params()[cp])
    assert keys[:2] == ["ma_type", "fast_period"]
    assert keys[keys.index("qty") + 1:] == list(UNIVERSAL_DEFAULTS)
    with TestClient(app) as c:
        html = c.get("/strategies/new").text
    line = next(ln for ln in html.splitlines() if "const STRAT_PARAMS" in ln)
    seg = line[line.index(f'"{cp}"'):]
    pos = [seg.index(f'"{k}"') for k in ("ma_type", "qty", "direction", "exit_rule", "stop_loss_pct")]
    assert pos == sorted(pos)
