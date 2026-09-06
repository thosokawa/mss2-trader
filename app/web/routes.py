from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.bars import load_bars
from app.config import get_config
from app.db import get_session
from app.engine.backtest import run_backtest
from app.models import (
    BacktestRun,
    BacktestTrade,
    Bar,
    LiveCursor,
    Signal,
    Strategy,
    Symbol,
    SymbolSet,
    SymbolSetItem,
    Tick,
)
from app.strategy.registry import BUILTIN, load_strategy_class

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_TZ = ZoneInfo(get_config().app.timezone)


def _jst(dt: datetime | str | None, fmt: str = "%m/%d %H:%M") -> str:
    """DB の naive UTC を設定タイムゾーン（既定 Asia/Tokyo）に直して表示する。

    SQLite の集計関数（func.min/max）は日時を文字列で返すことがあるので吸収する。
    """
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ).strftime(fmt)


templates.env.filters["jst"] = _jst


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _ctx(request: Request, **kw):
    return {"request": request, "builtin": BUILTIN, **kw}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_session)):
    counts = {
        "symbols": s.exec(select(func.count()).select_from(Symbol)).one(),
        "symbol_sets": s.exec(select(func.count()).select_from(SymbolSet)).one(),
        "bars": s.exec(select(func.count()).select_from(Bar)).one(),
        "backtests": s.exec(select(func.count()).select_from(BacktestRun)).one(),
        "strategies_enabled": s.exec(
            select(func.count()).select_from(Strategy).where(Strategy.enabled == True)  # noqa: E712
        ).one(),
    }
    recent_signals = s.exec(select(Signal).order_by(Signal.ts.desc()).limit(20)).all()
    recent_bt = s.exec(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(10)).all()

    last_tick = s.exec(select(func.max(Tick.received_at))).one()
    bridge = {
        "last_tick": last_tick,
        "age_sec": (_now_utc() - last_tick).total_seconds() if last_tick else None,
        "ticks_1h": s.exec(
            select(func.count()).select_from(Tick).where(Tick.received_at >= _now_utc() - timedelta(hours=1))
        ).one(),
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(request, counts=counts, recent_signals=recent_signals, recent_bt=recent_bt, bridge=bridge),
    )


# ---- 銘柄セット -----------------------------------------------------------------


@router.get("/symbol-sets", response_class=HTMLResponse)
def symbol_sets(request: Request, s: Session = Depends(get_session)):
    sets = s.exec(select(SymbolSet).order_by(SymbolSet.name)).all()

    def _count(set_id: int) -> int:
        return s.exec(
            select(func.count()).select_from(SymbolSetItem).where(SymbolSetItem.set_id == set_id)
        ).one()

    counts = {ss.id: _count(ss.id) for ss in sets}
    return templates.TemplateResponse(request, "symbol_sets.html", _ctx(request, sets=sets, counts=counts))


@router.post("/symbol-sets")
def create_symbol_set(name: str = Form(...), note: str = Form(""), s: Session = Depends(get_session)):
    if name.strip():
        s.add(SymbolSet(name=name.strip(), note=note.strip()))
        s.commit()
    return RedirectResponse("/symbol-sets", status_code=303)


@router.get("/symbol-sets/{set_id}", response_class=HTMLResponse)
def symbol_set_detail(set_id: int, request: Request, s: Session = Depends(get_session)):
    ss = s.get(SymbolSet, set_id)
    if not ss:
        return RedirectResponse("/symbol-sets", status_code=303)
    items = s.exec(
        select(SymbolSetItem, Symbol)
        .join(Symbol, Symbol.code == SymbolSetItem.symbol_code)
        .where(SymbolSetItem.set_id == set_id)
        .order_by(SymbolSetItem.sort_order, SymbolSetItem.id)
    ).all()
    return templates.TemplateResponse(request, "symbol_set_detail.html", _ctx(request, ss=ss, items=items))


@router.post("/symbol-sets/{set_id}/items")
def add_item(
    set_id: int,
    code: str = Form(...),
    name: str = Form(""),
    s: Session = Depends(get_session),
):
    code = code.strip()
    if code:
        if not s.get(Symbol, code):
            s.add(Symbol(code=code, name=name.strip()))
        elif name.strip():
            sym = s.get(Symbol, code)
            sym.name = name.strip()
            s.add(sym)
        exists = s.exec(
            select(SymbolSetItem).where(
                SymbolSetItem.set_id == set_id, SymbolSetItem.symbol_code == code
            )
        ).first()
        if not exists:
            n = s.exec(
                select(func.count()).select_from(SymbolSetItem).where(SymbolSetItem.set_id == set_id)
            ).one()
            s.add(SymbolSetItem(set_id=set_id, symbol_code=code, sort_order=n))
        s.commit()
    return RedirectResponse(f"/symbol-sets/{set_id}", status_code=303)


@router.post("/symbol-sets/{set_id}/items/{item_id}/delete")
def delete_item(set_id: int, item_id: int, s: Session = Depends(get_session)):
    it = s.get(SymbolSetItem, item_id)
    if it:
        s.delete(it)
        s.commit()
    return RedirectResponse(f"/symbol-sets/{set_id}", status_code=303)


@router.post("/symbol-sets/{set_id}/delete")
def delete_set(set_id: int, s: Session = Depends(get_session)):
    for it in s.exec(select(SymbolSetItem).where(SymbolSetItem.set_id == set_id)).all():
        s.delete(it)
    ss = s.get(SymbolSet, set_id)
    if ss:
        s.delete(ss)
    s.commit()
    return RedirectResponse("/symbol-sets", status_code=303)


# ---- データ（足）カバレッジ ---------------------------------------------------


@router.get("/data", response_class=HTMLResponse)
def data_coverage(request: Request, s: Session = Depends(get_session)):
    rows = s.exec(
        select(
            Bar.symbol_code,
            Bar.timeframe,
            func.count().label("n"),
            func.min(Bar.ts).label("start"),
            func.max(Bar.ts).label("end"),
        ).group_by(Bar.symbol_code, Bar.timeframe)
    ).all()
    names = {sym.code: sym.name for sym in s.exec(select(Symbol)).all()}
    return templates.TemplateResponse(request, "data.html", _ctx(request, rows=rows, names=names))


# ---- ライブ気配（bridge 生存監視） -----------------------------------------


def _latest_ticks(s: Session) -> list[dict]:
    codes = s.exec(select(Tick.symbol_code).distinct()).all()
    names = {sym.code: sym.name for sym in s.exec(select(Symbol)).all()}
    now = _now_utc()
    out = []
    for c in sorted(codes):
        t = s.exec(
            select(Tick).where(Tick.symbol_code == c).order_by(Tick.ts.desc()).limit(1)
        ).first()
        if not t:
            continue
        age = (now - t.received_at).total_seconds()
        out.append(
            {
                "code": c,
                "name": names.get(c, ""),
                "price": t.price,
                "bid": t.bid,
                "ask": t.ask,
                "volume": t.volume,
                "ts": t.ts,
                "received_at": t.received_at,
                "age_sec": round(age, 1),
                "stale": age > 30,
            }
        )
    return out


@router.get("/live", response_class=HTMLResponse)
def live(request: Request, s: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "live.html", _ctx(request, rows=_latest_ticks(s)))


@router.get("/live/table", response_class=HTMLResponse)
def live_table(request: Request, s: Session = Depends(get_session)):
    """htmx ポーリングで table 部分だけ差し替える。"""
    return templates.TemplateResponse(request, "_live_table.html", _ctx(request, rows=_latest_ticks(s)))


# ---- バックテスト -------------------------------------------------------------


@router.get("/backtest", response_class=HTMLResponse)
def backtest_form(request: Request, s: Session = Depends(get_session)):
    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()
    return templates.TemplateResponse(request, "backtest.html", _ctx(request, symbols=symbols, result=None))


@router.post("/backtest", response_class=HTMLResponse)
def backtest_run(
    request: Request,
    class_path: str = Form(...),
    symbol_code: str = Form(...),
    timeframe: str = Form("5m"),
    params_json: str = Form("{}"),
    commission_per_trade: float = Form(0.0),
    s: Session = Depends(get_session),
):
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            request,
            "backtest.html",
            _ctx(
                request,
                symbols=s.exec(select(Symbol)).all(),
                result=None,
                error=f"params JSON エラー: {e}",
            ),
        )

    cls = load_strategy_class(class_path)
    strat = cls(params)
    strat.timeframe = timeframe
    bars = load_bars(s, symbol_code, timeframe)
    result = run_backtest(strat, bars, symbol_code, commission_per_trade=commission_per_trade)

    run = BacktestRun(
        strategy_name=cls.__name__,
        class_path=class_path,
        params_json=json.dumps(strat.params, ensure_ascii=False),
        symbol_code=symbol_code,
        timeframe=timeframe,
        start=pd.Timestamp(bars.index[0]).to_pydatetime() if not bars.empty else None,
        end=pd.Timestamp(bars.index[-1]).to_pydatetime() if not bars.empty else None,
        metrics_json=json.dumps(result.metrics, ensure_ascii=False, default=str),
    )
    s.add(run)
    s.commit()
    s.refresh(run)
    for t in result.trades:
        s.add(
            BacktestTrade(
                run_id=run.id,
                symbol_code=symbol_code,
                entry_ts=t.entry_ts,
                entry_price=t.entry_price,
                exit_ts=t.exit_ts,
                exit_price=t.exit_price,
                qty=t.qty,
                pnl=t.pnl,
                return_pct=t.return_pct,
            )
        )
    s.commit()

    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()
    return templates.TemplateResponse(
        request,
        "backtest.html",
        _ctx(request, symbols=symbols, result=result, run_id=run.id, selected=symbol_code),
    )


@router.get("/backtests", response_class=HTMLResponse)
def backtests(request: Request, s: Session = Depends(get_session)):
    runs = s.exec(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(100)).all()
    parsed = [(r, json.loads(r.metrics_json or "{}")) for r in runs]
    return templates.TemplateResponse(request, "backtests.html", _ctx(request, runs=parsed))


@router.get("/backtests/{run_id}", response_class=HTMLResponse)
def backtest_detail(run_id: int, request: Request, s: Session = Depends(get_session)):
    run = s.get(BacktestRun, run_id)
    if not run:
        return RedirectResponse("/backtests", status_code=303)
    trades = s.exec(
        select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts)
    ).all()
    return templates.TemplateResponse(
        request,
        "backtest_detail.html",
        _ctx(request, run=run, metrics=json.loads(run.metrics_json or "{}"), trades=trades),
    )


# ---- シグナル履歴 -----------------------------------------------------------


@router.get("/signals", response_class=HTMLResponse)
def signals(request: Request, s: Session = Depends(get_session)):
    rows = s.exec(select(Signal).order_by(Signal.ts.desc()).limit(300)).all()
    return templates.TemplateResponse(request, "signals.html", _ctx(request, rows=rows))


# ---- 戦略（live エンジンで回す） -------------------------------------------


@router.get("/strategies", response_class=HTMLResponse)
def strategies(request: Request, s: Session = Depends(get_session)):
    rows = s.exec(select(Strategy).order_by(Strategy.created_at.desc())).all()
    sets = s.exec(select(SymbolSet).order_by(SymbolSet.name)).all()
    set_names = {ss.id: ss.name for ss in sets}
    return templates.TemplateResponse(
        request, "strategies.html", _ctx(request, rows=rows, sets=sets, set_names=set_names)
    )


@router.post("/strategies")
def create_strategy(
    name: str = Form(...),
    class_path: str = Form(...),
    symbol_set_id: str = Form(""),
    timeframe: str = Form("5m"),
    params_json: str = Form("{}"),
    mode: str = Form("notify"),
    s: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/strategies", status_code=303)
    try:
        json.loads(params_json or "{}")
    except json.JSONDecodeError:
        return RedirectResponse("/strategies?error=params", status_code=303)
    s.add(
        Strategy(
            name=name,
            class_path=class_path.strip(),
            params_json=params_json.strip() or "{}",
            symbol_set_id=int(symbol_set_id) if symbol_set_id else None,
            timeframe=timeframe,
            mode=mode,
            enabled=False,
        )
    )
    s.commit()
    return RedirectResponse("/strategies", status_code=303)


@router.post("/strategies/{strategy_id}/toggle")
def toggle_strategy(strategy_id: int, s: Session = Depends(get_session)):
    st = s.get(Strategy, strategy_id)
    if st:
        st.enabled = not st.enabled
        s.add(st)
        if st.enabled:
            # 有効化時はカーソルを捨てて「今より後の足だけ」に揃える
            # （無効中に溜まった足でまとめて発火するのを防ぐ）
            for c in s.exec(select(LiveCursor).where(LiveCursor.strategy_id == strategy_id)).all():
                s.delete(c)
        s.commit()
    return RedirectResponse("/strategies", status_code=303)


@router.post("/strategies/{strategy_id}/delete")
def delete_strategy(strategy_id: int, s: Session = Depends(get_session)):
    for c in s.exec(select(LiveCursor).where(LiveCursor.strategy_id == strategy_id)).all():
        s.delete(c)
    st = s.get(Strategy, strategy_id)
    if st:
        s.delete(st)
    s.commit()
    return RedirectResponse("/strategies", status_code=303)


# ---- bridge 受信 (P1) -------------------------------------------------------


@router.post("/api/ingest")
async def ingest(request: Request, s: Session = Depends(get_session)):
    """bridge からの気配を受け取る。body 例:
    {"quotes": [{"code": "7203", "price": 2810.5, "volume": 12300, "ts": "2026-09-06T04:30:00Z"}]}
    """
    body = await request.json()
    quotes = body.get("quotes", [])
    n = 0
    for q in quotes:
        raw_ts = q.get("ts")
        if raw_ts:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            ts = dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
        else:
            ts = _now_utc()
        if not (q.get("price") or q.get("bid") or q.get("ask")):
            continue
        s.add(
            Tick(
                symbol_code=str(q["code"]),
                ts=ts,
                price=float(q.get("price") or 0),
                volume=float(q.get("volume") or 0),
                bid=q.get("bid"),
                ask=q.get("ask"),
            )
        )
        n += 1
    s.commit()
    return JSONResponse({"ok": True, "received": n})


@router.get("/healthz")
def healthz():
    return {"ok": True}
