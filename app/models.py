"""SQLModel テーブル定義。P0 で使うのは Symbol / SymbolSet / Bar / Strategy / Signal /
BacktestRun / BacktestTrade。Order/Position/Fill は P3-P4 で使うが、スキーマを早めに
固定しておくため今のうちに定義しておく。"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """DB は一貫して naive UTC で扱う（SQLite の比較・resample を単純にするため）。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Symbol(SQLModel, table=True):
    code: str = Field(primary_key=True, description="4桁の証券コード 例 7203")
    name: str = ""
    market: str = "東証"
    tick_size: float = 1.0


class SymbolSet(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class SymbolSetItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    set_id: int = Field(foreign_key="symbolset.id", index=True)
    symbol_code: str = Field(foreign_key="symbol.code", index=True)
    sort_order: int = 0


class Bar(SQLModel, table=True):
    """確定済みローソク足。ts は UTC・足の開始時刻。"""

    id: int | None = Field(default=None, primary_key=True)
    symbol_code: str = Field(foreign_key="symbol.code", index=True)
    timeframe: str = Field(index=True, description="1m / 5m / 1d")
    ts: datetime = Field(index=True)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    source: str = "yfinance"

    __table_args__ = ({"sqlite_autoincrement": True},)


class Tick(SQLModel, table=True):
    """bridge から届く生の気配。P1 で足に集約する元データ。"""

    id: int | None = Field(default=None, primary_key=True)
    symbol_code: str = Field(index=True)
    ts: datetime = Field(index=True)
    price: float = 0.0
    volume: float = 0.0
    bid: float | None = None
    ask: float | None = None
    received_at: datetime = Field(default_factory=utcnow)


class Strategy(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    class_path: str = Field(description="例 app.strategy.examples.sma_cross:SmaCross")
    params_json: str = "{}"
    symbol_set_id: int | None = Field(default=None, foreign_key="symbolset.id")
    timeframe: str = "5m"
    mode: str = Field(default="notify", description="notify / paper / live")
    enabled: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Signal(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    strategy_id: int | None = Field(default=None, foreign_key="strategy.id", index=True)
    strategy_name: str = ""
    symbol_code: str = Field(index=True)
    ts: datetime = Field(index=True, description="トリガーとなった足の時刻")
    side: str = Field(description="BUY / SELL / EXIT")
    reason: str = ""
    price: float = 0.0
    origin: str = Field(default="backtest", description="backtest / live")
    idempotency_key: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=utcnow)


class PaperTrade(SQLModel, table=True):
    """ペーパートレードの1往復。live エンジンが mode=paper の戦略のシグナルから記録する
    （BUY で建て、EXIT/SELL で仕切る）。BacktestTrade のライブ版。現物ロング only。"""

    id: int | None = Field(default=None, primary_key=True)
    strategy_id: int = Field(foreign_key="strategy.id", index=True)
    strategy_name: str = ""
    symbol_code: str = Field(index=True)
    qty: int = 0
    entry_ts: datetime
    entry_price: float
    entry_reason: str = ""
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl: float | None = None
    return_pct: float | None = None
    status: str = Field(default="open", index=True, description="open / closed")
    created_at: datetime = Field(default_factory=utcnow)


class LiveCursor(SQLModel, table=True):
    """live エンジンが (戦略, 銘柄) ごとに「どの足まで評価したか」を記録する。

    初回は最新の確定足の時刻で初期化するだけで発火させない（有効化した瞬間に
    過去足ぶんのシグナルがまとめて飛ぶのを防ぐ）。以降は last_bar_ts より新しい
    足だけを評価する。
    """

    id: int | None = Field(default=None, primary_key=True)
    strategy_id: int = Field(foreign_key="strategy.id", index=True)
    symbol_code: str = Field(index=True)
    last_bar_ts: datetime
    updated_at: datetime = Field(default_factory=utcnow)


class BacktestRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    strategy_name: str = ""
    class_path: str = ""
    params_json: str = "{}"
    symbol_code: str = ""
    timeframe: str = ""
    start: datetime | None = None
    end: datetime | None = None
    metrics_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)


class BacktestTrade(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="backtestrun.id", index=True)
    symbol_code: str = ""
    side: str = "LONG"
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    qty: int
    pnl: float
    return_pct: float


# --- 以下 P3-P4 用（今は未使用・スキーマ固定目的）------------------------------


class Order(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    strategy_id: int | None = Field(default=None, foreign_key="strategy.id", index=True)
    symbol_code: str = Field(index=True)
    ts: datetime = Field(default_factory=utcnow)
    side: str = ""
    qty: int = 0
    order_type: str = "MKT"
    limit_price: float | None = None
    status: str = "new"
    broker_order_id: str = ""
    error: str = ""
    idempotency_key: str = Field(default="", index=True)


class Fill(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    ts: datetime = Field(default_factory=utcnow)
    qty: int = 0
    price: float = 0.0


class Position(SQLModel, table=True):
    symbol_code: str = Field(primary_key=True)
    qty: int = 0
    avg_price: float = 0.0
    updated_at: datetime = Field(default_factory=utcnow)
