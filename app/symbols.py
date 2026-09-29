"""銘柄マスタ（銘柄コード → 銘柄名）。JPX（日本取引所グループ）の上場銘柄一覧を取り込んで引く。

- 取り込み: /data 画面の「銘柄一覧を更新」または `refresh_master()`。JPX が月次で公開している
  「東証上場銘柄一覧」（data_j.xlsx、約4,400銘柄。ETF・REIT・英字入りコード 130A 等も含む）を
  ダウンロードして SymbolMaster テーブルを作り直す
- 参照: `lookup()`。銘柄セットに銘柄を追加するときの名前の自動補完、名前が空の Symbol の補完に使う
- 銘柄名の全角英数字（例 "ＱＤレーザ"）は NFKC で半角にそろえる

一覧ファイルの URL は JPX の「その他統計資料」ページ（JPX_PAGE_URL）からたどる。直リンクが
変わったとき（過去に .xls → .xlsx に変わった）にもページから拾い直せるようにしてある。
"""
from __future__ import annotations

import io
import re
import unicodedata

import httpx
import pandas as pd
from sqlmodel import Session, delete, select

from app.models import Strategy, Symbol, SymbolMaster, utcnow

JPX_BASE = "https://www.jpx.co.jp"
JPX_PAGE_URL = f"{JPX_BASE}/markets/statistics-equities/misc/01.html"
JPX_LIST_URL = f"{JPX_BASE}/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(name or ""))).strip()


def normalize_code(code: str) -> str:
    return unicodedata.normalize("NFKC", str(code or "")).strip().upper()


def _find_list_url(client: httpx.Client) -> str:
    """JPX のページから上場銘柄一覧（data_j.xls[x]）のリンクを探す。見つからなければ既定の URL。"""
    try:
        html = client.get(JPX_PAGE_URL).text
        m = re.search(r'href="([^"]*data_j\.xlsx?)"', html)
        if m:
            href = m.group(1)
            return href if href.startswith("http") else JPX_BASE + href
    except httpx.HTTPError:
        pass
    return JPX_LIST_URL


def download_list() -> pd.DataFrame:
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        url = _find_list_url(client)
        r = client.get(url)
        r.raise_for_status()
    return pd.read_excel(io.BytesIO(r.content), dtype=str)


def parse_list(df: pd.DataFrame) -> list[SymbolMaster]:
    """JPX の一覧（列: 日付, コード, 銘柄名, 市場・商品区分, 33業種区分, ...）→ SymbolMaster。"""
    out = []
    now = utcnow()
    for _, r in df.iterrows():
        code = normalize_code(r.get("コード"))
        name = normalize_name(r.get("銘柄名"))
        if not code or not name or code.lower() == "nan":
            continue
        sector = str(r.get("33業種区分") or "").strip()
        out.append(
            SymbolMaster(
                code=code,
                name=name,
                market=str(r.get("市場・商品区分") or "").strip(),
                sector=sector if sector not in ("-", "nan") else "",
                as_of=str(r.get("日付") or "").strip(),
                updated_at=now,
            )
        )
    return out


def refresh_master(session: Session, df: pd.DataFrame | None = None) -> dict:
    """銘柄マスタを JPX の一覧で作り直し、名前が空の Symbol も補完する。"""
    if df is None:
        df = download_list()
    rows = parse_list(df)
    if not rows:
        raise ValueError("JPX の一覧から銘柄を読み取れませんでした（列名が変わった可能性）")
    session.exec(delete(SymbolMaster))
    session.add_all(rows)
    session.commit()
    filled = fill_missing_names(session)
    return {"count": len(rows), "as_of": rows[0].as_of, "filled": filled}


def lookup(session: Session, code: str) -> SymbolMaster | None:
    code = normalize_code(code)
    return session.get(SymbolMaster, code) if code else None


def lookup_name(session: Session, code: str) -> str:
    m = lookup(session, code)
    return m.name if m else ""


def fill_missing_names(session: Session) -> int:
    """名前が空の Symbol に銘柄マスタの名前を入れる。補完した件数を返す。"""
    n = 0
    for sym in session.exec(select(Symbol).where(Symbol.name == "")).all():
        name = lookup_name(session, sym.code)
        if name:
            sym.name = name
            session.add(sym)
            n += 1
    session.commit()
    return n


def master_status(session: Session) -> dict:
    first = session.exec(select(SymbolMaster).limit(1)).first()
    if not first:
        return {"count": 0, "as_of": "", "updated_at": None}
    count = len(session.exec(select(SymbolMaster.code)).all())
    return {"count": count, "as_of": first.as_of, "updated_at": first.updated_at}


# ---- 戦略の対象銘柄・監視銘柄（銘柄セットの置き換え） ----------------------------


def parse_codes(text: str) -> list[str]:
    """"9984, 5016 7203" / 全角・読点区切りなども受け付けて、正規化・重複除去したコードの列。"""
    raw = re.split(r"[,\s、，]+", unicodedata.normalize("NFKC", str(text or "")))
    out: list[str] = []
    for c in raw:
        c = normalize_code(c)
        if c and c not in out:
            out.append(c)
    return out


def ensure_symbols(session: Session, codes: list[str]) -> None:
    """Symbol 行が無ければ作る（名前は銘柄マスタから）。名前が空なら補完する。"""
    for code in codes:
        sym = session.get(Symbol, code)
        if sym is None:
            session.add(Symbol(code=code, name=lookup_name(session, code)))
        elif not sym.name:
            sym.name = lookup_name(session, code)
            session.add(sym)
    session.commit()


def watch_codes(session: Session) -> list[str]:
    return sorted(session.exec(select(Symbol.code).where(Symbol.watch == True)).all())  # noqa: E712


def quote_codes(session: Session) -> list[str]:
    """RSS で株価を取り込む銘柄 = 有効な戦略の対象銘柄 ∪ 監視銘柄。

    bridge がこの一覧を定期的に取りに来て rss_bridge.xlsx の quotes シートを合わせる
    （build_workbook.py --auto も同じ一覧でブックを作る）。
    """
    codes: list[str] = []
    for st in session.exec(select(Strategy).where(Strategy.enabled == True)).all():  # noqa: E712
        codes += parse_codes(st.symbols)
    codes += watch_codes(session)
    out: list[str] = []
    for c in codes:
        if c not in out:
            out.append(c)
    return sorted(out)
