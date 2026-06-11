"""
module_c.py  ─  Thesis 检查器（多基金版）
输入股票代码，查看基金池内所有基金对该股的持仓历史、共识趋势、价格背离。

用法:
  python module_c.py MSFT
  python module_c.py          ← 交互式
"""

import sys
import sqlite3
import time
import re
from datetime import datetime, timedelta, date

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = "holdings.db"

QUARTER_MAP = {"03": "Q1", "06": "Q2", "09": "Q3", "12": "Q4"}

# ── CUSIP ↔ Ticker 映射 ────────────────────────────────────────────────────────
CUSIP_TO_TICKER: dict[str, str] = {
    "02079K107": "GOOGL", "02079K305": "GOOGL",
    "369604301": "GE",    "92826C839": "V",
    "615369105": "MCO",   "78409V104": "SPGI",
    "13646K108": "CP",    "136375102": "CNI",
    "594918104": "MSFT",  "N3168P101": "FER",
    "037833100": "AAPL",  "67066G104": "NVDA",
    "30303M102": "META",  "88160R101": "TSLA",
    "023135106": "AMZN",  "46625H100": "JPM",
    "38141G104": "GS",    "172967424": "C",
    "549463108": "LLY",   "084670702": "BRK.B",
    "G0750C108": "ARGX",  "N07059210": "ASML",
    "191216100": "KO",    "585521107": "MCD",
    "00724F101": "ADBE",  "09857L108": "BLK",
    "254687106": "DIS",   "46090E103": "ICE",
    "191791108": "KKR",   "81211K100": "SHOP",
    "G8T5AN108": "SPOT",  "88339J105": "TMUS",
    "50076Q106": "KVYO",  "G54050102": "IHG",
    "G3922B107": "FLUT",  "G20567101": "CRH",
}

TICKER_TO_CUSIPS: dict[str, list[str]] = {}
for _cusip, _tk in CUSIP_TO_TICKER.items():
    TICKER_TO_CUSIPS.setdefault(_tk.upper(), []).append(_cusip)

TICKER_ALIAS: dict[str, str] = {
    "ALPHABET": "GOOGL", "GOOGLE": "GOOGL",
    "MICROSOFT": "MSFT", "APPLE": "AAPL",
    "AMAZON": "AMZN",    "NVIDIA": "NVDA",
    "FACEBOOK": "META",  "VISA": "V",
    "MOODY": "MCO",      "MOODYS": "MCO",
}


# ── 工具 ──────────────────────────────────────────────────────────────────────

def qlabel(quarter: str) -> str:
    # quarter 格式 "2026Q1" 或 period "2026-03-31"
    if "Q" in quarter:
        return quarter[-4:]   # e.g. "26Q1"
    yr, mo, _ = quarter.split("-")
    return f"{yr[2:]}{QUARTER_MAP.get(mo,'??')}"


def action_label(curr_sh: int, prev_sh: int | None) -> str:
    if prev_sh is None:         return "🆕 新仓"
    if curr_sh == 0:            return "❌ 清仓"
    diff = curr_sh - prev_sh
    pct  = diff / prev_sh * 100 if prev_sh else 0
    if pct >= 20:  return "▲▲ 大幅加仓"
    if diff > 0:   return "▲  加仓"
    if pct <= -50: return "▼▼ 大幅减仓"
    if diff < 0:   return "▼  减仓"
    return "─  持平"


def conviction(pct: float) -> str:
    if pct >= 15: return "★★★ 核心"
    if pct >= 8:  return "★★  重要"
    if pct >= 3:  return "★   配置"
    return               "    观察"


def trend_summary(actions: list[str]) -> str:
    adds  = sum(1 for a in actions if "▲" in a or "新仓" in a)
    cuts  = sum(1 for a in actions if "▼" in a or "清仓" in a)
    if adds > cuts and adds >= 2: return "📈 持续加仓"
    if cuts > adds and cuts >= 2: return "📉 持续减仓"
    if "🆕" in actions[-1]:       return "⚡ 最新新建仓"
    if "❌" in actions[-1]:       return "🚪 最新清仓"
    if adds > cuts:               return "↗  总体加仓"
    if cuts > adds:               return "↘  总体减仓"
    return                               "↔  基本持平"


# ── 价格查询 ──────────────────────────────────────────────────────────────────

def get_price_at(ticker: str, target_date: str) -> float | None:
    if not ticker or ticker in ("FER",): return None
    try:
        import yfinance as yf
        d     = datetime.strptime(target_date, "%Y-%m-%d").date()
        start = (d - timedelta(days=7)).isoformat()
        end   = (d + timedelta(days=3)).isoformat()
        hist  = yf.Ticker(ticker).history(start=start, end=end)
        return float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        return None


def get_current_price(ticker: str) -> float | None:
    if not ticker or ticker in ("FER",): return None
    try:
        import yfinance as yf
        return float(yf.Ticker(ticker).fast_info.last_price)
    except Exception:
        return None


# ── 数据查询 ──────────────────────────────────────────────────────────────────

def resolve_ticker(ticker: str, conn) -> tuple[str, list[str]]:
    """返回 (canonical_ticker, [cusip...])"""
    tk = TICKER_ALIAS.get(ticker.upper(), ticker.upper())
    cusips = TICKER_TO_CUSIPS.get(tk, [])
    if cusips:
        return tk, cusips

    # 用 issuer 名模糊搜索
    rows = conn.execute(
        "SELECT DISTINCT cusip, issuer FROM holdings WHERE issuer LIKE ?",
        (f"%{ticker}%",)
    ).fetchall()
    cusips = [r[0] for r in rows]
    return tk, cusips


def load_history(cusips: list[str], conn) -> list[dict]:
    if not cusips:
        return []
    ph = ",".join("?" * len(cusips))
    rows = conn.execute(f"""
        SELECT fd.name, f.cik, f.quarter, f.period, f.total_value,
               h.issuer, h.cusip, h.value, h.shares, h.pct
        FROM holdings h
        JOIN filings f  ON h.filing_id = f.id
        JOIN funds   fd ON f.cik = fd.cik
        WHERE h.cusip IN ({ph})
        ORDER BY f.cik, f.quarter
    """, cusips).fetchall()

    result = []
    for fname, cik, quarter, period, total_val, issuer, cusip, value, shares, pct in rows:
        result.append({
            "fund": fname, "cik": cik, "quarter": quarter, "period": period,
            "fund_total_k": total_val,
            "issuer": issuer, "cusip": cusip,
            "value_k": value, "shares": shares, "pct": pct,
        })
    return result


def all_quarters(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT quarter FROM filings ORDER BY quarter"
    ).fetchall()]


# ── 展示 ──────────────────────────────────────────────────────────────────────

def print_fund_block(fname: str, records: list[dict],
                     quarters: list[str], ticker: str,
                     curr_price: float | None):
    """打印单只基金的持仓历史块。"""
    by_q = {r["quarter"]: r for r in records}

    print(f"\n  ┌─ {fname[:48]}")
    print(f"  │  {'季度':<6} {'操作':<12} {'市值$M':>8} {'股数':>12} {'占比':>6}  {'成本估算':>9}")
    print(f"  │  {'─'*60}")

    prev_sh = None
    actions = []
    for q in quarters:
        if q not in by_q:
            print(f"  │  {qlabel(q):<6} {'─ 未持有':<12}")
            prev_sh = None
            continue
        r   = by_q[q]
        act = action_label(r["shares"], prev_sh)
        actions.append(act)
        cost = r["value_k"] * 1000 / r["shares"] if r["shares"] else 0
        print(f"  │  {qlabel(q):<6} {act:<12} "
              f"{r['value_k']/1000:>8,.1f} {r['shares']:>12,} "
              f"{r['pct']:>5.1f}%  ${cost:>8.2f}")
        prev_sh = r["shares"]

    latest = records[-1]
    trend  = trend_summary(actions) if actions else "─"
    conv   = conviction(latest["pct"])

    print(f"  │  {'─'*60}")
    print(f"  │  最新: {latest['pct']:.1f}%  {conv}   趋势: {trend}")

    # 价格背离
    if ticker and ticker not in ("FER",) and latest["shares"]:
        cost_latest = latest["value_k"] * 1000 / latest["shares"]
        if curr_price and cost_latest:
            chg = (curr_price - cost_latest) / cost_latest * 100
            if   chg >  30: sig = "⚠  大幅上涨，评估是否已 priced in"
            elif chg >  15: sig = "↑  上涨，关注估值"
            elif chg < -20: sig = "↓  大幅回落，验证 thesis"
            elif chg < -10: sig = "↓  下跌，留意"
            else:           sig = "≈  接近季末价"
            print(f"  │  季末估算 ${cost_latest:.1f} → 今日 ${curr_price:.1f}"
                  f" ({chg:+.1f}%)  {sig}")

    print(f"  └{'─'*62}")


def print_consensus_summary(by_fund: dict[str, list[dict]],
                            quarters: list[str],
                            ticker: str, curr_price: float | None):
    """跨基金共识汇总。"""
    all_q = quarters
    total_funds = len(by_fund)
    latest_q = all_q[-1]

    # 最新季持仓情况
    holders_latest = {cik: recs for cik, recs in by_fund.items()
                      if recs[-1]["quarter"] == latest_q}

    print(f"\n{'━'*68}")
    print(f"  综合共识  ─  {ticker}")
    print(f"{'━'*68}")
    print(f"  基金池共 {total_funds} 家持有  最新季({qlabel(latest_q)}) 仍持有: {len(holders_latest)} 家")

    if holders_latest:
        avg_pct = sum(r[-1]["pct"] for r in holders_latest.values()) / len(holders_latest)
        total_val_b = sum(r[-1]["value_k"] for r in holders_latest.values()) / 1e6
        print(f"  池内平均仓位: {avg_pct:.1f}%   合计持仓规模: ${total_val_b:.1f}B")

    # 各季持有基金数变化
    q_holder_count = []
    for q in all_q:
        cnt = sum(1 for recs in by_fund.values() if q in {r["quarter"] for r in recs})
        q_holder_count.append((q, cnt))

    print(f"\n  持有基金数变化:")
    for q, cnt in q_holder_count:
        bar = "█" * cnt + "░" * (total_funds - cnt)
        print(f"    {qlabel(q)}  {bar}  {cnt}/{total_funds}")

    # 最新季操作方向统计
    adders  = []
    holders = []
    exitors = []
    for cik, recs in by_fund.items():
        qs = {r["quarter"]: r for r in recs}
        if latest_q not in qs:
            continue
        prev_q = all_q[-2] if len(all_q) >= 2 else None
        prev_sh = qs[prev_q]["shares"] if prev_q and prev_q in qs else None
        act = action_label(qs[latest_q]["shares"], prev_sh)
        fname = recs[0]["fund"][:28]
        if "▲" in act or "新仓" in act:
            adders.append(fname)
        elif "▼" in act or "清仓" in act:
            exitors.append(fname)
        else:
            holders.append(fname)

    if adders:
        print(f"\n  🔺 加仓/新仓 ({len(adders)}家): {', '.join(adders)}")
    if holders:
        print(f"  ─  持平     ({len(holders)}家): {', '.join(holders)}")
    if exitors:
        print(f"  🔻 减仓/清仓 ({len(exitors)}家): {', '.join(exitors)}")

    if curr_price:
        print(f"\n  今日现价: ${curr_price:.2f}")
    print()


# ── 主程序 ────────────────────────────────────────────────────────────────────

def run(ticker_input: str):
    conn    = sqlite3.connect(DB_PATH)
    qtrs    = all_quarters(conn)
    ticker, cusips = resolve_ticker(ticker_input, conn)
    records = load_history(cusips, conn)

    print(f"\n{'═'*68}")
    print(f"  【Module C】Thesis 检查器  ─  {ticker}")
    print(f"  季度覆盖: {' / '.join(qlabel(q) for q in qtrs)}")
    print(f"{'═'*68}")

    if not records:
        print(f"\n  ✗ 基金池中未找到 {ticker} 的持仓记录")
        print(f"  提示: 可尝试公司名关键词，如 python module_c.py alphabet\n")
        conn.close()
        return

    # 获取现价
    curr_price = None
    if ticker not in ("FER",):
        print(f"\n  获取 {ticker} 当前价格...", end=" ", flush=True)
        curr_price = get_current_price(ticker)
        print(f"${curr_price:.2f}" if curr_price else "失败")

    # 按基金分组
    by_fund: dict[str, list[dict]] = {}
    for r in records:
        by_fund.setdefault(r["cik"], []).append(r)

    # 各基金明细块
    for cik, fund_recs in sorted(by_fund.items(),
                                  key=lambda x: -x[1][-1]["pct"]):
        print_fund_block(fund_recs[0]["fund"], fund_recs,
                         qtrs, ticker, curr_price)

    # 共识汇总
    print_consensus_summary(by_fund, qtrs, ticker, curr_price)
    conn.close()


def main():
    if len(sys.argv) > 1:
        run(" ".join(sys.argv[1:]))
    else:
        print("\n  Module C  Thesis 检查器  (多基金版)")
        print(f"  数据库: {DB_PATH}")
        while True:
            try:
                q = input("\n  输入 Ticker 或公司名（回车退出）: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            run(q)


if __name__ == "__main__":
    main()
