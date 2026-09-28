"""発注専用ブック（rss_orders.xlsx）のシート構成。build_workbook.py と bridge.py の両方が使う。

  orders           : RssStockOrder（現物）        引数20個 A〜T、ステータス U
  margin_open      : RssMarginOpenOrder（信用新規） 引数22個 A〜V、ステータス W
  margin_close     : RssMarginCloseOrder（信用返済）引数20個 A〜T、ステータス U
  margin_positions : RssMarginPositionList（信用建玉一覧。返済する建玉の建日・建値・建市場を引く）

引数の並びは楽天証券 MarketSpeed II RSS オンラインヘルプ（注文）の関数形式どおり。
依存ライブラリを持たない（bridge.py から素の import で読めるように）。
"""
from __future__ import annotations

N_ROWS = 300


def col_letter(n: int) -> str:
    """1 始まりの列番号 → Excel の列記号（1=A, 27=AA）。"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


STOCK_ORDER_ARGS = [
    "発注ID", "発注トリガー", "銘柄コード", "売買区分", "注文区分", "SOR区分",
    "注文数量", "価格区分", "注文価格", "執行条件", "注文期限", "口座区分",
    "逆指値条件価格", "逆指値条件区分", "逆指値価格区分", "逆指値価格",
    "セット注文区分", "セット注文価格", "セット注文執行条件", "セット注文期限",
]
MARGIN_OPEN_ARGS = [
    "発注ID", "発注トリガー", "銘柄コード", "売買区分", "注文区分", "SOR区分", "信用区分",
    "注文数量", "価格区分", "注文価格", "執行条件", "注文期限", "口座区分",
    "逆指値条件価格", "逆指値条件区分", "逆指値価格区分", "逆指値価格",
    "セット注文区分", "セット注文価格区分", "セット注文価格", "セット注文執行条件", "セット注文期限",
]
MARGIN_CLOSE_ARGS = [
    "発注ID", "発注トリガー", "銘柄コード", "売買区分", "注文区分", "SOR区分", "信用区分",
    "注文数量", "価格区分", "注文価格", "執行条件", "注文期限", "口座区分",
    "建日", "建単価", "建市場",
    "逆指値条件価格", "逆指値条件区分", "逆指値価格区分", "逆指値価格",
]

# sheet 名 -> (RSS 関数名, 引数の見出し)
ORDER_SHEETS = {
    "orders": ("RssStockOrder", STOCK_ORDER_ARGS),
    "margin_open": ("RssMarginOpenOrder", MARGIN_OPEN_ARGS),
    "margin_close": ("RssMarginCloseOrder", MARGIN_CLOSE_ARGS),
}

POSITIONS_SHEET = "margin_positions"
POSITION_ITEMS = [
    "銘柄コード", "銘柄名称", "口座区分", "建市場", "信用区分", "弁済期限", "売買", "建玉数量",
    "発注数量", "建値", "建日",
]


def status_col(sheet: str) -> str:
    return col_letter(len(ORDER_SHEETS[sheet][1]) + 1)


def order_formula(sheet: str, row: int) -> str:
    func, args = ORDER_SHEETS[sheet]
    refs = ",".join(f"{col_letter(c)}{row}" for c in range(1, len(args) + 1))
    return f"={func}({refs})"


def positions_formula() -> str:
    return f"=RssMarginPositionList($A$1:${col_letter(len(POSITION_ITEMS))}$1)"
