"""
analyze_tci.py
基于 tci_test.db，对 TCI Fund Management 做三层分析：
  B1  QoQ 持仓变化（新仓 / 加仓 / 减仓 / 清仓）
  B2  Position Sizing 排行（当季）
  B3  价格背离（季末买入价 vs 今日现价）

运行: python analyze_tci.py
"""

import sys
import sqlite3
from datetime import datetime, date

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = "tci_test.db"

# ── CUSIP → Ticker 手动映射（TCI持仓）─────────────────────────────────────────
# yfinance 用 ticker，EDGAR 只有 CUSIP，先维护一张小表
CUSIP_TO_TICKER = {
    "02079K107": "GOOGL",   # Alphabet CL C
    "02079K305": "GOOGL",   # Alphabet CL A
    "369604301": "GE",      # GE Aerospace（GEV 是 GE Vernova，不同公司）
    "92826C839": "V",       # Visa
    "615369105": "MCO",     # Moody's
    "78409V104": "SPGI",    # S&P Global
    "13646K108": "CP",      # Canadian Pacific Kansas City（原 CP，CPKC yfinance 不识别）
    "136375102": "CNI",     # Canadian National Railway
    "594918104": "MSFT",    # Microsoft
    "N3168P101": "FER.MC",  # Ferrovial SE（西班牙，yfinance 不准）
}

# ── 数据库读取 ────────────────────────────────────────────────────────────────

def load_all_quarters(conn) -> dict[str, list[dict]]:
    """返回 {period: [holdings...]} 按期间分组，期间按升序排列。"""
    rows = conn.execute("""
        SELECT f.period, h.issuer, h.cusip, h.value, h.shares, h.pct
        FROM holdings h
        JOIN filings f ON h.filing_id = f.id
        ORDER BY f.period ASC, h.value DESC
    """).fetchall()

    result: dict[str, list[dict]] = {}
    for period, issuer, cusip, value, shares, pct in rows:
        result.setdefault(period, []).append({
            "issuer": issuer, "cusip": cusip,
            "value": value, "shares": shares, "pct": pct,
            "ticker": CUSIP_TO_TICKER.get(cusip, ""),
        })
    return result


# ── B1  QoQ 变化检测 ──────────────────────────────────────────────────────────

def detect_changes(prev: list[dict], curr: list[dict]) -> list[dict]:
    """对比相邻两季，标注每只股的变化类型和幅度。"""
    prev_map = {h["cusip"]: h for h in prev}
    curr_map = {h["cusip"]: h for h in curr}

    results = []

    for cusip, ch in curr_map.items():
        ph = prev_map.get(cusip)
        if ph is None:
            action = "🆕 新仓"
            val_chg = ch["value"]
            pct_chg = ch["pct"]
            sh_chg  = ch["shares"]
        else:
            sh_diff = ch["shares"] - ph["shares"]
            sh_pct  = sh_diff / ph["shares"] * 100 if ph["shares"] else 0
            if sh_diff > 0:
                action = "▲ 加仓"
            elif sh_diff < 0:
                action = "▼ 减仓"
            else:
                action = "─ 持平"
            val_chg = ch["value"] - ph["value"]
            pct_chg = ch["pct"] - ph["pct"]
            sh_chg  = sh_diff

        results.append({**ch, "action": action,
                        "val_chg": val_chg, "pct_chg": pct_chg, "sh_chg": sh_chg})

    # 已清仓的
    for cusip, ph in prev_map.items():
        if cusip not in curr_map:
            results.append({**ph, "action": "❌ 清仓",
                            "val_chg": -ph["value"], "pct_chg": -ph["pct"],
                            "sh_chg": -ph["shares"]})

    results.sort(key=lambda x: x["value"], reverse=True)
    return results


def print_qoq(periods: list[str], by_period: dict[str, list[dict]]):
    print(f"\n{'━'*66}")
    print(f"  【B1】QoQ 持仓变化")
    print(f"{'━'*66}")

    for i in range(1, len(periods)):
        prev_p, curr_p = periods[i-1], periods[i]
        prev = by_period[prev_p]
        curr = by_period[curr_p]
        changes = detect_changes(prev, curr)

        print(f"\n  {prev_p}  →  {curr_p}")
        print(f"  {'持仓':<34} {'操作':<8} {'市值$M':>8} {'仓位%':>6} {'仓位变化':>8}")
        print(f"  {'─'*64}")
        for c in changes:
            sign   = "+" if c["pct_chg"] >= 0 else ""
            marker = "  " if c["action"] == "─ 持平" else "► "
            print(f"  {marker}{c['issuer'][:32]:<32} {c['action']:<8} "
                  f"{c['value']/1000:>8,.1f} {c['pct']:>5.1f}% "
                  f"{sign}{c['pct_chg']:>+.1f}pp")


# ── B2  Position Sizing 排行 ──────────────────────────────────────────────────

def print_sizing(periods: list[str], by_period: dict[str, list[dict]]):
    latest_p = periods[-1]
    holdings = by_period[latest_p]
    total_k  = sum(h["value"] for h in holdings)

    print(f"\n{'━'*66}")
    print(f"  【B2】Position Sizing  ({latest_p}，总规模 ${total_k/1e6:.1f}B)")
    print(f"{'━'*66}")
    print(f"  {'#':<3} {'持仓':<34} {'市值$M':>8} {'占比':>6}  信念")
    print(f"  {'─'*60}")

    for i, h in enumerate(holdings, 1):
        bar = "█" * int(h["pct"] / 2) + "░" * (15 - int(h["pct"] / 2))
        conviction = (
            "★★★ 核心" if h["pct"] >= 15 else
            "★★  重要" if h["pct"] >= 8  else
            "★   配置" if h["pct"] >= 3  else
            "    边缘"
        )
        print(f"  {i:<3} {h['issuer'][:33]:<34} {h['value']/1000:>8,.1f} "
              f"{h['pct']:>5.1f}%  {conviction}")


# ── B3  价格背离 ──────────────────────────────────────────────────────────────

def get_price_on(ticker: str, target_date: str) -> float | None:
    """取 target_date 当天或最近一个交易日的收盘价。"""
    if not ticker or ticker.endswith(".MC"):
        return None
    try:
        import yfinance as yf
        # 取 target_date 前后各 5 天的数据，取最后一个有效收盘
        from datetime import timedelta
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        start = (d - timedelta(days=7)).isoformat()
        end   = (d + timedelta(days=3)).isoformat()
        hist  = yf.Ticker(ticker).history(start=start, end=end)
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def get_current_price(ticker: str) -> float | None:
    if not ticker or ticker.endswith(".MC"):
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).fast_info
        return float(info.last_price)
    except Exception:
        return None


def print_price_divergence(periods: list[str], by_period: dict[str, list[dict]]):
    latest_p = periods[-1]
    holdings = by_period[latest_p]
    today    = date.today().isoformat()

    print(f"\n{'━'*72}")
    print(f"  【B3】价格背离  季末({latest_p}) → 今日({today})")
    print(f"{'━'*72}")
    print(f"  {'持仓':<28} {'Ticker':<8} {'季末价':>7} {'今日价':>7} {'涨跌幅':>8}  {'信号'}")
    print(f"  {'─'*70}")

    for h in holdings:
        ticker  = h["ticker"]
        if not ticker or ticker.endswith(".MC"):
            print(f"  {h['issuer'][:27]:<28} {'':8} {'—':>7} {'—':>7} {'—':>8}  (无ticker)")
            continue

        # 季末每股成本 ≈ 季末总市值 / 股数（近似，13F value 为季末市值）
        cost_per_sh = (h["value"] * 1000 / h["shares"]) if h["shares"] else None
        curr_price  = get_current_price(ticker)

        if cost_per_sh and curr_price:
            chg = (curr_price - cost_per_sh) / cost_per_sh * 100
            if chg > 30:
                signal = "⚠  大幅上涨，thesis可能已priced in"
            elif chg > 15:
                signal = "↑  上涨，关注估值"
            elif chg < -20:
                signal = "↓  大幅下跌，验证thesis"
            elif chg < -10:
                signal = "↓  下跌，留意"
            else:
                signal = "≈  接近季末价"
            print(f"  {h['issuer'][:27]:<28} {ticker:<8} "
                  f"${cost_per_sh:>6.1f} ${curr_price:>6.1f} "
                  f"{chg:>+7.1f}%  {signal}")
        else:
            print(f"  {h['issuer'][:27]:<28} {ticker:<8} {'—':>7} {'—':>7} {'—':>8}  (价格获取失败)")


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    conn     = sqlite3.connect(DB_PATH)
    by_period = load_all_quarters(conn)
    periods  = sorted(by_period.keys())
    conn.close()

    print(f"\n{'═'*66}")
    print(f"  TCI Fund Management  13F 分析报告")
    print(f"  覆盖季度: {' / '.join(periods)}")
    print(f"{'═'*66}")

    print_qoq(periods, by_period)
    print_sizing(periods, by_period)
    print_price_divergence(periods, by_period)

    print(f"\n{'═'*66}\n")


if __name__ == "__main__":
    main()
