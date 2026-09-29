from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app import symbols
from app.bars import load_bars
from app.config import get_config
from app.db import get_session
from app.engine import orders as orders_engine
from app.engine import paper
from app.engine.backtest import run_backtest
from app.engine.optimize import OptimizeRow, optimize
from app.engine.risk import get_risk_engine
from app.history import fetch_and_store
from app.models import (
    BacktestRun,
    BacktestTrade,
    Bar,
    LiveCursor,
    OptimizationRun,
    Order,
    PaperTrade,
    Signal,
    Strategy,
    Symbol,
    Tick,
)
from app.strategy.registry import BUILTIN, builtin_param_meta, builtin_params, load_strategy_class
from app.web import status as status_mod
from app.web.glossary import GLOSSARY, gloss

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC_DIR = Path(__file__).parent / "static"


def _static(name: str) -> str:
    """/static のファイルの URL に更新時刻を付ける（?v=…）。ファイルを変えたらブラウザが
    キャッシュの古い CSS/JS を使い続けないように（2026-09-29 に param_form.js で発生）。"""
    try:
        v = int((_STATIC_DIR / name).stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{name}?v={v}"


templates.env.globals["static"] = _static

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
templates.env.filters["gloss"] = gloss
templates.env.filters["since"] = status_mod.since_text
MODE_LABELS = {"notify": "通知のみ", "paper": "ペーパー", "live": "実発注"}
templates.env.filters["mode_label"] = lambda m: MODE_LABELS.get(m, m)


def _pnl(v, unit: str = "") -> str:
    """損益の表示: 色だけに頼らず ▲▼ と符号でも分かるように（▲ +1,250 / ▼ −500 / ±0）。"""
    if v is None:
        return "—"
    v = round(float(v))
    if v > 0:
        return f"▲ +{v:,}{unit}"
    if v < 0:
        return f"▼ −{abs(v):,}{unit}"
    return f"±0{unit}"


def _pnl_cls(v) -> str:
    if v is None:
        return "muted"
    return "pos" if v > 0 else "neg" if v < 0 else "muted"


templates.env.filters["pnl"] = _pnl
templates.env.filters["pnl_cls"] = _pnl_cls


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _ctx(request: Request, **kw):
    return {
        "request": request,
        "builtin": BUILTIN,
        "builtin_params": builtin_params(),
        "builtin_param_meta": builtin_param_meta(),
        **kw,
    }


@router.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request, s: Session = Depends(get_session)):
    """全ページ上部のステータスバー（base.html が数秒ごとに取りに来る）。"""
    return templates.TemplateResponse(
        request, "_status_bar.html", _ctx(request, sys=status_mod.system_status(s))
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_session)):
    """システム全体の今の状態。表示部分は数秒ごとに自動で差し替わる（base.html の autorefresh）。"""
    sys = status_mod.system_status(s)
    day0 = status_mod.jst_day_start_utc(sys["now"])
    today_signals = s.exec(
        select(Signal).where(Signal.ts >= day0).order_by(Signal.ts.desc(), Signal.id.desc()).limit(15)
    ).all()
    today_orders = sorted(sys["today_orders"], key=lambda o: o.id, reverse=True)[:15]
    recent_bt = s.exec(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(5)).all()

    paper_closed = s.exec(select(PaperTrade).where(PaperTrade.status == "closed")).all()
    paper = {
        "realized": round(sum(t.pnl or 0 for t in paper_closed), 0),
        "open_n": s.exec(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "open")
        ).one(),
        "strategies": s.exec(
            select(func.count()).select_from(Strategy).where(Strategy.mode == "paper")
        ).one(),
    }
    overview = status_mod.strategy_overview(s)
    today_trips = [t for t in orders_engine.round_trips(s) if t["exit_ts"] >= day0]
    unrealized = [r["unrealized"] for o in overview for r in o["rows"] if r["unrealized"] is not None]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            sys=sys,
            overview=overview,
            today_summary=orders_engine.summarize_trips(today_trips),
            unrealized_total=sum(unrealized) if unrealized else None,
            quotes=_latest_ticks(s),
            quote_codes=symbols.quote_codes(s),
            today_signals=today_signals,
            today_orders=today_orders,
            recent_bt=recent_bt,
            paper=paper,
        ),
    )


# ---- 銘柄（戦略の対象銘柄・監視銘柄。旧「銘柄セット」は廃止） ------------------------


@router.get("/api/quote-codes")
def api_quote_codes(s: Session = Depends(get_session)):
    """RSS で株価を取り込む銘柄（有効な戦略の対象銘柄 ∪ 監視銘柄）。bridge が定期的に取りに来る。"""
    return {"codes": symbols.quote_codes(s)}


@router.post("/symbols/watch")
def add_watch(codes: str = Form(...), s: Session = Depends(get_session)):
    """監視銘柄を追加（戦略で使っていなくても株価を取り込む）。カンマ/スペース区切りで複数可。"""
    parsed = symbols.parse_codes(codes)
    symbols.ensure_symbols(s, parsed)
    for code in parsed:
        sym = s.get(Symbol, code)
        sym.watch = True
        s.add(sym)
    s.commit()
    return RedirectResponse("/symbols", status_code=303)


@router.post("/symbols/watch/{code}/delete")
def delete_watch(code: str, s: Session = Depends(get_session)):
    sym = s.get(Symbol, symbols.normalize_code(code))
    if sym:
        sym.watch = False
        s.add(sym)
        s.commit()
    return RedirectResponse("/symbols", status_code=303)


@router.get("/api/symbol-name")
def api_symbol_name(code: str = "", s: Session = Depends(get_session)):
    """銘柄コード → 銘柄名（銘柄マスタから）。見つからなければ name は空。"""
    m = symbols.lookup(s, code)
    return {"code": symbols.normalize_code(code), "name": m.name if m else "",
            "market": m.market if m else ""}


@router.post("/data/symbols/refresh", response_class=HTMLResponse)
def data_symbols_refresh(request: Request, s: Session = Depends(get_session)):
    try:
        res = symbols.refresh_master(s)
        msg = (f"銘柄一覧を更新しました: {res['count']:,} 銘柄（JPX {res['as_of']} 時点）。"
               f"名前が空だった銘柄 {res['filled']} 件を補完。")
        ok = True
    except Exception as e:  # noqa: BLE001 - ネットワーク/形式エラーを画面に出す
        msg, ok = f"銘柄一覧の更新に失敗しました: {e}", False
    return templates.TemplateResponse(
        request, "data.html",
        _ctx(request, fetch_results=None, master_msg=msg, master_ok=ok, **_data_coverage_ctx(s)),
    )


def _data_coverage_ctx(s: Session) -> dict:
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
    # 「過去データを取得」のコード欄に一発で入れる候補（取り込み中の全銘柄 / 戦略ごと）
    presets: dict[str, str] = {}
    all_codes = symbols.quote_codes(s)
    if all_codes:
        presets["取り込み中の全銘柄"] = ",".join(all_codes)
    for st in s.exec(select(Strategy).where(Strategy.deleted == False).order_by(Strategy.name)).all():  # noqa: E712
        codes = symbols.parse_codes(st.symbols)
        if codes:
            presets[f"戦略: {st.name}"] = ",".join(codes)
    return {
        "master": symbols.master_status(s),
        "rows": rows,
        "names": names,
        "presets": presets,
    }


@router.get("/data", response_class=HTMLResponse)
def data_coverage(request: Request, s: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "data.html", _ctx(request, fetch_results=None, **_data_coverage_ctx(s))
    )


@router.post("/data/fetch", response_class=HTMLResponse)
def data_fetch(
    request: Request,
    codes: str = Form(...),
    interval: str = Form("5m"),
    period: str = Form("60d"),
    s: Session = Depends(get_session),
):
    code_list = [c for c in re.split(r"[,\s]+", codes.strip()) if c]
    results = [fetch_and_store(s, code, interval, period) for code in code_list]
    return templates.TemplateResponse(
        request,
        "data.html",
        _ctx(
            request,
            fetch_results=results,
            fetch_interval=interval,
            fetch_period=period,
            **_data_coverage_ctx(s),
        ),
    )


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


def _collected_rows(s: Session) -> list[dict]:
    """データ収集中の銘柄（有効な戦略の対象銘柄 ∪ 監視銘柄）ごとの最新気配と、収集している理由。"""
    names = {sym.code: sym for sym in s.exec(select(Symbol)).all()}
    used_by: dict[str, list[str]] = {}
    for st in s.exec(
        select(Strategy).where(Strategy.enabled == True, Strategy.deleted == False)  # noqa: E712
    ).all():
        for c in symbols.parse_codes(st.symbols):
            used_by.setdefault(c, []).append(st.name)
    ticks = {r["code"]: r for r in _latest_ticks(s)}
    rows = []
    for code in symbols.quote_codes(s):
        sym = names.get(code)
        rows.append({
            "code": code,
            "name": sym.name if sym else "",
            "watch": bool(sym and sym.watch),
            "used_by": used_by.get(code, []),
            "tick": ticks.get(code),
        })
    return rows


@router.get("/symbols", response_class=HTMLResponse)
def symbols_page(request: Request, s: Session = Depends(get_session)):
    """データ収集中の銘柄（旧「ライブ」）。一覧・最新気配・監視銘柄の追加と削除。"""
    return templates.TemplateResponse(
        request, "symbols.html", _ctx(request, rows=_collected_rows(s))
    )


@router.get("/live")
def live_redirect():
    return RedirectResponse("/symbols", status_code=301)


# ---- バックテスト -------------------------------------------------------------


@router.get("/backtest", response_class=HTMLResponse)
def backtest_form(request: Request, s: Session = Depends(get_session)):
    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()
    return templates.TemplateResponse(
        request, "backtest.html", _ctx(request, symbols=symbols, result=None, class_path=None)
    )


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
                class_path=class_path,
                selected=symbol_code,
                selected_timeframe=timeframe,
                params_json=params_json,
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
                side=t.side,
            )
        )
    s.commit()

    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()
    return templates.TemplateResponse(
        request,
        "backtest.html",
        _ctx(
            request,
            symbols=symbols,
            result=result,
            run_id=run.id,
            class_path=class_path,
            selected=symbol_code,
            selected_timeframe=timeframe,
            params_json=json.dumps(strat.params, ensure_ascii=False),
        ),
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


# ---- 最適化（グリッドサーチ + ウォークフォワード検証） -----------------------


@router.get("/optimize", response_class=HTMLResponse)
def optimize_form(request: Request, s: Session = Depends(get_session)):
    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()
    return templates.TemplateResponse(
        request, "optimize.html", _ctx(request, symbols=symbols, result=None, class_path=None)
    )


@router.post("/optimize", response_class=HTMLResponse)
def optimize_run(
    request: Request,
    class_path: str = Form(...),
    symbol_code: str = Form(...),
    timeframe: str = Form("5m"),
    grid_json: str = Form(...),
    train_ratio: float = Form(0.7),
    min_test_trades: int = Form(3),
    rank_by: str = Form("total_pnl"),
    commission_per_trade: float = Form(0.0),
    s: Session = Depends(get_session),
):
    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()

    def _error(msg: str):
        return templates.TemplateResponse(
            request,
            "optimize.html",
            _ctx(
                request, symbols=symbols, result=None, error=msg,
                class_path=class_path, selected_symbol=symbol_code, rank_by=rank_by,
            ),
        )

    try:
        grid = json.loads(grid_json or "{}")
    except json.JSONDecodeError as e:
        return _error(f"パラメータ範囲 JSON エラー: {e}")

    cls = load_strategy_class(class_path)
    bars = load_bars(s, symbol_code, timeframe)
    try:
        rows = optimize(
            cls, bars, symbol_code, grid,
            timeframe=timeframe, train_ratio=train_ratio, min_test_trades=min_test_trades,
            rank_by=rank_by, commission_per_trade=commission_per_trade,
        )
    except ValueError as e:
        return _error(str(e))

    top = rows[:50]
    run = OptimizationRun(
        strategy_name=cls.__name__,
        class_path=class_path,
        symbol_code=symbol_code,
        timeframe=timeframe,
        grid_json=json.dumps(grid, ensure_ascii=False),
        train_ratio=train_ratio,
        rank_by=rank_by,
        min_test_trades=min_test_trades,
        combos=len(rows),
        results_json=json.dumps(
            [{"params": r.params, "train": r.train, "test": r.test, "warning": r.warning} for r in top],
            ensure_ascii=False,
            default=str,
        ),
    )
    s.add(run)
    s.commit()
    s.refresh(run)
    return templates.TemplateResponse(
        request,
        "optimize.html",
        _ctx(
            request, symbols=symbols, result=top, run_id=run.id, combos=len(rows),
            class_path=class_path, selected_symbol=symbol_code, rank_by=rank_by,
            selected_timeframe=timeframe,
        ),
    )


@router.get("/optimizations", response_class=HTMLResponse)
def optimizations(request: Request, s: Session = Depends(get_session)):
    runs = s.exec(select(OptimizationRun).order_by(OptimizationRun.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse(request, "optimizations.html", _ctx(request, runs=runs))


@router.get("/optimizations/{run_id}", response_class=HTMLResponse)
def optimization_detail(run_id: int, request: Request, s: Session = Depends(get_session)):
    run = s.get(OptimizationRun, run_id)
    if not run:
        return RedirectResponse("/optimizations", status_code=303)
    parsed = json.loads(run.results_json or "[]")
    result = [
        OptimizeRow(params=r["params"], train=r["train"], test=r["test"], warning=r.get("warning", ""))
        for r in parsed
    ]
    symbols = s.exec(select(Symbol).order_by(Symbol.code)).all()
    return templates.TemplateResponse(
        request,
        "optimize.html",
        _ctx(
            request, symbols=symbols, result=result, run_id=run.id, combos=run.combos,
            class_path=run.class_path, selected_symbol=run.symbol_code, rank_by=run.rank_by,
            readonly=True,
        ),
    )


# ---- シグナル履歴 -----------------------------------------------------------


@router.get("/signals", response_class=HTMLResponse)
def signals(request: Request, strategy_id: int | None = None, s: Session = Depends(get_session)):
    """自動売買 › シグナル。?strategy_id= で戦略ごとに絞り込む。"""
    stmt = select(Signal)
    if strategy_id:
        stmt = stmt.where(Signal.strategy_id == strategy_id)
    rows = s.exec(stmt.order_by(Signal.ts.desc(), Signal.id.desc()).limit(300)).all()
    return templates.TemplateResponse(
        request, "signals.html",
        _ctx(request, rows=rows, strategies=_active_strategies(s), strategy_id=strategy_id),
    )


# ---- 戦略（live エンジンで回す） -------------------------------------------


STRATEGY_ERRORS = {
    "dup_name": "同じ名前の戦略が既にあります。別の名前を付けてください。",
    "params": "パラメータ（JSON）の形式が正しくありません。",
    "no_symbols": "対象銘柄（証券コード）を1つ以上入れてください。",
    "open_position": "この戦略は実発注の建玉を持っています。建玉がある間は、ロジック・足・モードの変更、"
                     "建玉のある銘柄を外すこと、戦略の削除はできません"
                     "（bot がその建玉を管理しなくなり、損切り・手仕舞いが出なくなるため）。"
                     "先に手仕舞うか、手動で決済して 自動売買 › 概要 で「手動決済を記録」してください。",
}


def _active_strategies(s: Session) -> list[Strategy]:
    return s.exec(
        select(Strategy).where(Strategy.deleted == False).order_by(Strategy.created_at.desc())  # noqa: E712
    ).all()


@router.get("/strategies", response_class=HTMLResponse)
def strategies(request: Request, s: Session = Depends(get_session)):
    """自動売買 › 戦略: 一覧（設定は要約文で表示）。"""
    if request.query_params.get("class_path"):
        # /optimizations の「この設定で戦略登録」→ 追加画面へ（パラメータを引き継ぐ）
        return RedirectResponse(f"/strategies/new?{request.url.query}", status_code=303)
    names = {sym.code: sym.name for sym in s.exec(select(Symbol)).all()}
    received = set(s.exec(select(Tick.symbol_code).distinct()).all())
    no_ticks = [c for c in symbols.quote_codes(s) if c not in received]
    rows = [status_mod.strategy_state(s, st, names) for st in _active_strategies(s)]
    return templates.TemplateResponse(
        request,
        "strategies.html",
        _ctx(request, rows=rows, names=names, no_ticks=no_ticks,
             error_msg=STRATEGY_ERRORS.get(request.query_params.get("error", ""), "")),
    )


def _strategy_form(request: Request, *, st: Strategy | None, values: dict, error_code: str = "",
                   locked: list[str] | None = None):
    return templates.TemplateResponse(
        request,
        "strategy_form.html",
        _ctx(request, st=st, v=values, error_msg=STRATEGY_ERRORS.get(error_code, error_code),
             locked=locked or []),
    )


@router.get("/strategies/new", response_class=HTMLResponse)
def strategy_new(request: Request):
    q = request.query_params
    values = {"name": "", "class_path": q.get("class_path", ""), "symbols": q.get("symbols", ""),
              "timeframe": q.get("timeframe", "5m"), "mode": "notify",
              "params_json": q.get("params_json", "")}
    return _strategy_form(request, st=None, values=values)


def _validate_strategy_form(s: Session, values: dict, exclude_id: int | None = None) -> str:
    if not values["name"]:
        return "名前を入れてください。"
    try:
        if not isinstance(json.loads(values["params_json"] or "{}"), dict):
            return "params"
    except json.JSONDecodeError:
        return "params"
    dup = s.exec(select(Strategy).where(Strategy.name == values["name"])).first()
    if dup and dup.id != exclude_id:
        return "dup_name"
    if not symbols.parse_codes(values["symbols"]):
        return "no_symbols"
    return ""


@router.post("/strategies")
def create_strategy(
    request: Request,
    name: str = Form(...),
    class_path: str = Form(...),
    symbols_text: str = Form("", alias="symbols"),
    timeframe: str = Form("5m"),
    params_json: str = Form("{}"),
    mode: str = Form("notify"),
    s: Session = Depends(get_session),
):
    values = {"name": name.strip(), "class_path": class_path.strip(), "symbols": symbols_text,
              "timeframe": timeframe, "mode": mode, "params_json": params_json.strip() or "{}"}
    err = _validate_strategy_form(s, values)
    if err:
        return _strategy_form(request, st=None, values=values, error_code=err)
    codes = symbols.parse_codes(symbols_text)
    symbols.ensure_symbols(s, codes)
    st = Strategy(
        name=values["name"],
        class_path=values["class_path"],
        params_json=values["params_json"],
        symbols=",".join(codes),
        timeframe=timeframe,
        mode=mode,
        enabled=False,
    )
    s.add(st)
    s.commit()
    s.refresh(st)
    return RedirectResponse(f"/strategies/{st.id}", status_code=303)


def _get_strategy(s: Session, strategy_id: int) -> Strategy | None:
    st = s.get(Strategy, strategy_id)
    return st if st and not st.deleted else None


@router.get("/strategies/{strategy_id}", response_class=HTMLResponse)
def strategy_detail(strategy_id: int, request: Request, s: Session = Depends(get_session)):
    """戦略の詳細: 設定（日本語の項目名）・今の状況・成績・シグナル・発注。"""
    st = _get_strategy(s, strategy_id)
    if not st:
        return RedirectResponse("/strategies", status_code=303)
    state = status_mod.strategy_state(s, st)
    trips = list(reversed(orders_engine.round_trips(s, st.id)))
    paper_trades = s.exec(
        select(PaperTrade).where(PaperTrade.strategy_id == st.id)
        .order_by(PaperTrade.entry_ts.desc()).limit(50)
    ).all()
    paper_closed = [t for t in paper_trades if t.status == "closed"]
    return templates.TemplateResponse(
        request,
        "strategy_detail.html",
        _ctx(
            request,
            state=state,
            d=state["desc"],
            trips=trips[:50],
            live_summary=orders_engine.summarize_trips(trips),
            paper_trades=paper_trades,
            paper_summary=paper.summarize(paper_closed),
            signals=s.exec(
                select(Signal).where(Signal.strategy_id == st.id)
                .order_by(Signal.ts.desc(), Signal.id.desc()).limit(30)
            ).all(),
            orders=s.exec(
                select(Order).where(Order.strategy_id == st.id)
                .order_by(Order.ts.desc(), Order.id.desc()).limit(30)
            ).all(),
            open_codes=_open_live_codes(s, st.id),
            error_msg=STRATEGY_ERRORS.get(request.query_params.get("error", ""), ""),
        ),
    )


def _locked_fields(s: Session, st: Strategy) -> list[str]:
    """建玉がある間は変えられない項目（変えると bot がその建玉を管理しなくなる）。"""
    return ["class_path", "timeframe", "mode"] if _open_live_codes(s, st.id) else []


@router.get("/strategies/{strategy_id}/edit", response_class=HTMLResponse)
def strategy_edit(strategy_id: int, request: Request, s: Session = Depends(get_session)):
    st = _get_strategy(s, strategy_id)
    if not st:
        return RedirectResponse("/strategies", status_code=303)
    values = {"name": st.name, "class_path": st.class_path, "symbols": st.symbols,
              "timeframe": st.timeframe, "mode": st.mode, "params_json": st.params_json}
    return _strategy_form(request, st=st, values=values, locked=_locked_fields(s, st))


@router.post("/strategies/{strategy_id}/edit")
def strategy_update(
    strategy_id: int,
    request: Request,
    name: str = Form(...),
    class_path: str = Form(""),
    symbols_text: str = Form("", alias="symbols"),
    timeframe: str = Form(""),
    params_json: str = Form("{}"),
    mode: str = Form(""),
    s: Session = Depends(get_session),
):
    st = _get_strategy(s, strategy_id)
    if not st:
        return RedirectResponse("/strategies", status_code=303)
    locked = _locked_fields(s, st)
    values = {"name": name.strip(), "class_path": class_path.strip() or st.class_path,
              "symbols": symbols_text, "timeframe": timeframe or st.timeframe,
              "mode": mode or st.mode, "params_json": params_json.strip() or "{}"}
    err = _validate_strategy_form(s, values, exclude_id=st.id)
    codes = symbols.parse_codes(symbols_text)
    if not err and locked:
        changed = [f for f in locked if values[f] != getattr(st, f)]
        removed = [c for c in _open_live_codes(s, st.id) if c not in codes]
        if changed or removed:
            err = "open_position"
    if err:
        return _strategy_form(request, st=st, values=values, error_code=err, locked=locked)

    old_codes = set(symbols.parse_codes(st.symbols))
    reset_all = values["timeframe"] != st.timeframe or values["class_path"] != st.class_path
    symbols.ensure_symbols(s, codes)
    st.name = values["name"]
    st.class_path = values["class_path"]
    st.params_json = values["params_json"]
    st.symbols = ",".join(codes)
    st.timeframe = values["timeframe"]
    st.mode = values["mode"]
    s.add(st)
    # 足・ロジックを変えたら全銘柄、銘柄を足したらその銘柄を「今より後の足だけ」から評価し直す
    for c in s.exec(select(LiveCursor).where(LiveCursor.strategy_id == st.id)).all():
        if reset_all or c.symbol_code not in old_codes:
            s.delete(c)
    s.commit()
    return RedirectResponse(f"/strategies/{st.id}", status_code=303)


def _back(next_url: str, default: str = "/strategies") -> RedirectResponse:
    """フォームの next（戻り先）へ。外部 URL には飛ばさない。"""
    ok = next_url.startswith("/") and not next_url.startswith("//")
    return RedirectResponse(next_url if ok else default, status_code=303)


@router.post("/strategies/{strategy_id}/toggle")
def toggle_strategy(strategy_id: int, next_url: str = Form("/strategies", alias="next"),
                    s: Session = Depends(get_session)):
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
    return _back(next_url)


def _open_live_codes(s: Session, strategy_id: int) -> list[str]:
    """この戦略が実発注（live）で建玉を持っている銘柄。"""
    return [p["symbol_code"] for p in orders_engine.open_positions(s) if p["strategy_id"] == strategy_id]


@router.post("/strategies/{strategy_id}/symbols")
def update_strategy_symbols(
    strategy_id: int, symbols_text: str = Form("", alias="symbols"), s: Session = Depends(get_session)
):
    st = s.get(Strategy, strategy_id)
    if not st:
        return RedirectResponse("/strategies", status_code=303)
    codes = symbols.parse_codes(symbols_text)
    if not codes:
        return RedirectResponse("/strategies?error=no_symbols", status_code=303)
    # 建玉がある銘柄を外すと、その建玉を bot が管理しなくなる（損切り・手仕舞いが出ない）
    if [c for c in _open_live_codes(s, strategy_id) if c not in codes]:
        return RedirectResponse("/strategies?error=open_position", status_code=303)
    symbols.ensure_symbols(s, codes)
    old = set(symbols.parse_codes(st.symbols))
    st.symbols = ",".join(codes)
    s.add(st)
    # 新しく足した銘柄は「今より後の足だけ」から評価する（カーソル初期化は live が行う）
    for c in s.exec(select(LiveCursor).where(LiveCursor.strategy_id == strategy_id)).all():
        if c.symbol_code not in old:
            s.delete(c)
    s.commit()
    return RedirectResponse("/strategies", status_code=303)


@router.post("/strategies/{strategy_id}/delete")
def delete_strategy(strategy_id: int, s: Session = Depends(get_session)):
    if _open_live_codes(s, strategy_id):
        return RedirectResponse("/strategies?error=open_position", status_code=303)
    for c in s.exec(select(LiveCursor).where(LiveCursor.strategy_id == strategy_id)).all():
        s.delete(c)
    st = s.get(Strategy, strategy_id)
    if st and not st.deleted:
        # 論理削除: id を使い回させないため行は残す。名前は空けて同じ名前で作り直せるようにする
        st.deleted = True
        st.enabled = False
        st.name = f"{st.name} (削除済み #{st.id})"
        s.add(st)
    s.commit()
    return RedirectResponse("/strategies", status_code=303)


# ---- リスク管理 / 実発注（P4） -----------------------------------------------


@router.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, s: Session = Depends(get_session)):
    eng = get_risk_engine()
    positions = orders_engine.open_positions(s)
    return templates.TemplateResponse(
        request,
        "risk.html",
        _ctx(
            request,
            trading_cfg=eng.cfg,
            state=eng.state,
            sys=status_mod.system_status(s),
            positions=positions,
            prices={code: paper.latest_price(s, code) for code in {p["symbol_code"] for p in positions}},
            names={sym.code: sym.name for sym in s.exec(select(Symbol)).all()},
        ),
    )


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, s: Session = Depends(get_session)):
    """自動売買 › 発注履歴。"""
    rows = s.exec(select(Order).order_by(Order.ts.desc(), Order.id.desc()).limit(300)).all()
    return templates.TemplateResponse(request, "orders.html", _ctx(request, rows=rows))


@router.post("/risk/arm")
def risk_arm():
    get_risk_engine().arm()
    return RedirectResponse("/risk", status_code=303)


@router.post("/risk/manual-close")
def risk_manual_close(
    strategy_id: int = Form(...), symbol_code: str = Form(...), s: Session = Depends(get_session)
):
    """MarketSpeed II で手動決済した建玉を bot 側で「決済済み」にする（発注はしない）。"""
    orders_engine.record_manual_close(s, strategy_id, symbol_code)
    return RedirectResponse("/risk", status_code=303)


@router.post("/risk/disarm")
def risk_disarm(reason: str = Form("manual")):
    get_risk_engine().disarm(reason or "manual")
    return RedirectResponse("/risk", status_code=303)


# ---- 発注リレー（P4）: bridge が拾って RssStockOrder で発注、結果を報告する -----


@router.get("/api/orders/pending")
def orders_pending(s: Session = Depends(get_session)):
    """bridge がポーリングして拾う。呼ぶたびに status を new -> sending に進めて配布済みにする。"""
    claimed = orders_engine.claim_pending(s)
    return {"orders": [orders_engine.order_to_dict(o) for o in claimed]}


@router.post("/api/orders/{order_id}/report")
async def orders_report(order_id: int, request: Request, s: Session = Depends(get_session)):
    """bridge からの結果報告。body 例:
    {"status": "filled", "broker_order_id": "...", "filled_qty": 100, "avg_price": 2810.0}
    {"status": "rejected", "error": "発注ロック中（発注を行うには発注機能を有効にしてください）"}
    """
    body = await request.json()
    order = orders_engine.apply_report(
        s,
        order_id,
        status=body.get("status", "error"),
        broker_order_id=body.get("broker_order_id", ""),
        filled_qty=int(body.get("filled_qty") or 0),
        avg_price=float(body.get("avg_price") or 0.0),
        error=body.get("error", ""),
    )
    if order is None:
        return JSONResponse({"ok": False, "error": f"order {order_id} not found"}, status_code=404)
    return {"ok": True}


# ---- 成績（ペーパートレード） --------------------------------------------


@router.get("/performance", response_class=HTMLResponse)
def performance(request: Request, s: Session = Depends(get_session)):
    strategies = s.exec(
        select(Strategy)
        .where(Strategy.mode == "paper", Strategy.deleted == False)  # noqa: E712
        .order_by(Strategy.created_at.desc())
    ).all()
    names = {sym.code: sym.name for sym in s.exec(select(Symbol)).all()}

    cards = []
    for st in strategies:
        trades = s.exec(
            select(PaperTrade).where(PaperTrade.strategy_id == st.id).order_by(PaperTrade.entry_ts.desc())
        ).all()
        closed = [t for t in trades if t.status == "closed"]
        opens = []
        unrealized = 0.0
        for t in (t for t in trades if t.status == "open"):
            last = paper.latest_price(s, t.symbol_code)
            direction = -1 if t.side == "SHORT" else 1
            u = (last - t.entry_price) * t.qty * direction if last else 0.0
            unrealized += u
            opens.append({"t": t, "last": last, "unrealized": u})
        m = paper.summarize(closed)
        realized = m.get("realized_pnl", 0) or 0
        cards.append(
            {
                "st": st,
                "metrics": m,
                "opens": opens,
                "realized": realized,
                "unrealized": round(unrealized, 0),
                "total": round(realized + unrealized, 0),
                "trades": trades[:50],
            }
        )
    # 実発注: 発注履歴から往復（建て→手仕舞い）を組み立てた概算の成績。戦略ごと（削除済みも含む）
    day0 = status_mod.jst_day_start_utc(_now_utc())
    by_strategy: dict[int, list[dict]] = {}
    for t in orders_engine.round_trips(s):
        by_strategy.setdefault(t["strategy_id"], []).append(t)
    live_cards = []
    for sid, trips in by_strategy.items():
        st = s.get(Strategy, sid)
        known_today = [t["pnl"] for t in trips if t["pnl"] is not None and t["exit_ts"] >= day0]
        live_cards.append({
            "strategy_id": sid,
            "name": trips[-1]["strategy_name"],
            "alive": bool(st and not st.deleted),
            "metrics": orders_engine.summarize_trips(trips),
            "today": round(sum(known_today), 0),
            "trips": list(reversed(trips))[:30],
        })
    live_cards.sort(key=lambda c: (not c["alive"], c["name"]))
    live_total = orders_engine.summarize_trips(orders_engine.round_trips(s))
    return templates.TemplateResponse(
        request, "performance.html",
        _ctx(request, cards=cards, names=names, live_cards=live_cards, live_total=live_total),
    )


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


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", _ctx(request, glossary=GLOSSARY))


@router.get("/healthz")
def healthz():
    return {"ok": True}
