"""
module_b.py  ─  个股信号（多基金版）
B1  新仓信号：本季多家基金同时新开的仓位
B2  加仓共识：净买方最多的股票（加仓家数 − 减仓家数）
B3  减仓共识：净卖方最多的股票
B4  Position Sizing 热榜：池内最高信念持仓
B5  价格背离：主要信号股票的季末价 vs 今日现价

运行: python module_b.py [--quarter 2026Q1]
"""

import sys, json, sqlite3, time
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH      = "holdings.db"
CT_CACHE     = Path("cusip_ticker_cache.json")   # CUSIP→ticker（OpenFIGI构建）
PRICE_CACHE  = Path("price_cache.json")           # ticker→{date: price}

# 加仓阈值：股数变化超过此比例才算显著
ADD_THRESHOLD  = 0.05   # +5%
TRIM_THRESHOLD = 0.05   # -5%


# ── 缓存工具 ──────────────────────────────────────────────────────────────────

def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

def save_json(p: Path, d: dict):
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ── ticker 查询 ───────────────────────────────────────────────────────────────

def get_ticker(cusip: str, ct_cache: dict) -> str:
    return ct_cache.get(cusip, "")


# ── 价格查询（带缓存）─────────────────────────────────────────────────────────

def price_at(ticker: str, date_str: str, cache: dict) -> float | None:
    """季末收盘价，带本地缓存。"""
    if not ticker or "/" in ticker or len(ticker) > 10:
        return None
    key = f"{ticker}@{date_str}"
    if key in cache:
        return cache[key] if cache[key] else None
    try:
        import yfinance as yf
        d     = datetime.strptime(date_str, "%Y-%m-%d").date()
        start = (d - timedelta(days=7)).isoformat()
        end   = (d + timedelta(days=3)).isoformat()
        hist  = yf.Ticker(ticker).history(start=start, end=end)
        if hist.empty:
            cache[key] = None
            return None
        p = float(hist["Close"].iloc[-1])
        cache[key] = p
        time.sleep(0.2)
        return p
    except Exception:
        cache[key] = None
        return None


def current_price(ticker: str, cache: dict) -> float | None:
    if not ticker or "/" in ticker or len(ticker) > 10:
        return None
    key = f"{ticker}@NOW"
    if key in cache:
        return cache[key] if cache[key] else None
    try:
        import yfinance as yf
        p = float(yf.Ticker(ticker).fast_info.last_price)
        cache[key] = p
        time.sleep(0.2)
        return p
    except Exception:
        cache[key] = None
        return None


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_quarters(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT quarter FROM filings ORDER BY quarter"
    ).fetchall()]


def load_quarter_holdings(conn, quarter: str) -> dict[str, dict]:
    """返回 {cik: {cusip: {issuer, value_k, shares, pct, period}}}"""
    rows = conn.execute("""
        SELECT f.cik, fd.name, f.period, h.cusip, h.issuer,
               h.value, h.shares, h.pct
        FROM holdings h
        JOIN filings f  ON h.filing_id = f.id
        JOIN funds   fd ON f.cik = fd.cik
        WHERE f.quarter = ?
          AND (h.put_call IS NULL OR h.put_call = '')
        ORDER BY f.cik, h.value DESC
    """, (quarter,)).fetchall()

    result: dict[str, dict] = {}
    for cik, fname, period, cusip, issuer, value, shares, pct in rows:
        result.setdefault(cik, {"name": fname, "period": period, "holdings": {}})
        result[cik]["holdings"][cusip] = {
            "issuer": issuer, "value_k": value,
            "shares": shares, "pct": pct,
        }
    return result


# ── 变化检测 ──────────────────────────────────────────────────────────────────

def classify_action(curr_sh: int, prev_sh: int | None) -> str:
    if prev_sh is None:
        return "NEW"
    if curr_sh == 0 and prev_sh > 0:
        return "EXIT"
    if prev_sh == 0:
        return "NEW"
    chg = (curr_sh - prev_sh) / prev_sh
    if chg >= ADD_THRESHOLD:
        return "ADD"
    if chg <= -TRIM_THRESHOLD:
        return "TRIM"
    return "HOLD"


def compute_changes(prev_q: dict, curr_q: dict) -> dict[str, list[dict]]:
    """
    返回 {cusip: [{cik, fname, action, curr_pct, prev_pct, curr_sh, prev_sh,
                   issuer, period, chg_pct}]}
    """
    result: dict[str, list] = defaultdict(list)
    all_ciks = set(prev_q) | set(curr_q)

    for cik in all_ciks:
        fname  = curr_q.get(cik, prev_q.get(cik, {})).get("name", cik)
        period = curr_q.get(cik, {}).get("period", "")
        curr_h = curr_q.get(cik, {}).get("holdings", {})
        prev_h = prev_q.get(cik, {}).get("holdings", {})
        all_cusips = set(curr_h) | set(prev_h)

        for cusip in all_cusips:
            c = curr_h.get(cusip)
            p = prev_h.get(cusip)
            if c is None and p is None:
                continue

            curr_sh  = c["shares"]  if c else 0
            prev_sh  = p["shares"]  if p else None
            curr_pct = c["pct"]     if c else 0.0
            prev_pct = p["pct"]     if p else 0.0
            issuer   = (c or p)["issuer"]
            action   = classify_action(curr_sh, prev_sh)

            if action == "HOLD":
                continue

            result[cusip].append({
                "cik": cik, "fname": fname[:22],
                "action": action,
                "curr_pct": curr_pct, "prev_pct": prev_pct,
                "curr_sh":  curr_sh,  "prev_sh":  prev_sh or 0,
                "issuer": issuer, "period": period,
                "pct_chg": curr_pct - prev_pct,
            })

    return dict(result)


def net_signal(actions: list[dict]) -> tuple[int, int, int]:
    """返回 (new_count, net_add, exit_count)"""
    new_  = sum(1 for a in actions if a["action"] == "NEW")
    add_  = sum(1 for a in actions if a["action"] == "ADD")
    trim_ = sum(1 for a in actions if a["action"] == "TRIM")
    exit_ = sum(1 for a in actions if a["action"] == "EXIT")
    return new_, (add_ + new_) - (trim_ + exit_), exit_


# ── 格式化 ────────────────────────────────────────────────────────────────────

def fmt_pct_chg(chg: float) -> str:
    s = f"{chg:+.1f}pp"
    return s

def action_icon(a: str) -> str:
    return {"NEW": "🆕", "ADD": "▲", "TRIM": "▼", "EXIT": "❌"}.get(a, "─")


# ── B1  新仓信号 ──────────────────────────────────────────────────────────────

def print_new_positions(changes: dict, ct_cache: dict, min_funds: int = 2):
    """多家同季新开的仓位——最强共识信号。"""
    new_stocks = {
        cusip: [a for a in acts if a["action"] == "NEW"]
        for cusip, acts in changes.items()
    }
    new_stocks = {c: a for c, a in new_stocks.items() if len(a) >= min_funds}

    print(f"\n{'━'*72}")
    print(f"  【B1】新仓信号  （≥{min_funds}家同季新开，最强共识）")
    print(f"{'━'*72}")

    if not new_stocks:
        print(f"\n  本季无 ≥{min_funds} 家同时新开的仓位\n")
        return

    ranked = sorted(new_stocks.items(), key=lambda x: -len(x[1]))
    for cusip, acts in ranked[:15]:
        issuer = acts[0]["issuer"]
        ticker = get_ticker(cusip, ct_cache)
        tk_str = f"({ticker})" if ticker else ""
        print(f"\n  🆕 {issuer[:36]:<36} {tk_str}")
        print(f"     {len(acts)} 家同时建仓:")
        for a in sorted(acts, key=lambda x: -x["curr_pct"]):
            print(f"       • {a['fname']:<22}  {a['curr_pct']:.1f}% 仓位")


# ── B2/B3  加仓/减仓共识 ──────────────────────────────────────────────────────

def print_consensus_moves(changes: dict, ct_cache: dict, direction: str):
    """direction = 'buy' or 'sell'"""
    title = "【B2】加仓共识" if direction == "buy" else "【B3】减仓共识"
    print(f"\n{'━'*72}")
    print(f"  {title}  （净买方/卖方最多的个股）")
    print(f"{'━'*72}")

    scored = []
    for cusip, acts in changes.items():
        new_, net, exit_ = net_signal(acts)
        if direction == "buy"  and net <= 0: continue
        if direction == "sell" and net >= 0: continue
        total_funds = len(acts)
        buyers  = [a for a in acts if a["action"] in ("NEW", "ADD")]
        sellers = [a for a in acts if a["action"] in ("TRIM", "EXIT")]
        issuer  = acts[0]["issuer"]
        ticker  = get_ticker(cusip, ct_cache)
        scored.append((cusip, issuer, ticker, net, buyers, sellers, new_, exit_))

    scored.sort(key=lambda x: (-abs(x[3]), -len(x[4])) if direction == "buy"
                else (x[3], -len(x[5])))

    for cusip, issuer, ticker, net, buyers, sellers, new_, exit_ in scored[:12]:
        tk_str  = f"({ticker})" if ticker else ""
        net_str = f"+{net}" if net > 0 else str(net)
        print(f"\n  {'▲' if direction=='buy' else '▼'} "
              f"{issuer[:36]:<36} {tk_str:<10} 净{net_str}家")

        if direction == "buy":
            for a in sorted(buyers, key=lambda x: -x["curr_pct"])[:6]:
                tag = "🆕" if a["action"] == "NEW" else "▲"
                print(f"       {tag} {a['fname']:<22}  {a['curr_pct']:.1f}%"
                      f"  ({fmt_pct_chg(a['pct_chg'])})")
        else:
            for a in sorted(sellers, key=lambda x: x["curr_pct"])[:6]:
                tag = "❌" if a["action"] == "EXIT" else "▼"
                print(f"       {tag} {a['fname']:<22}  {a['curr_pct']:.1f}%"
                      f"  ({fmt_pct_chg(a['pct_chg'])})")


# ── B4  Position Sizing 热榜 ──────────────────────────────────────────────────

def print_sizing_heatmap(curr_q: dict, ct_cache: dict, top_n: int = 20):
    """全池最高信念持仓——按（持有基金数 × 平均仓位）综合排名。"""
    print(f"\n{'━'*72}")
    print(f"  【B4】Position Sizing 热榜  （高信念 = 多家 × 高仓位）")
    print(f"{'━'*72}")

    # 聚合每只股票在各基金的仓位
    stock_data: dict[str, dict] = {}
    for cik, fund in curr_q.items():
        for cusip, h in fund["holdings"].items():
            if cusip not in stock_data:
                stock_data[cusip] = {
                    "issuer": h["issuer"],
                    "ticker": get_ticker(cusip, ct_cache),
                    "funds": [],
                }
            stock_data[cusip]["funds"].append({
                "fname": fund["name"][:20],
                "pct":   h["pct"],
                "value_k": h["value_k"],
            })

    # 综合得分：持有家数 × 最大单仓占比（体现集中度）
    ranked = []
    for cusip, d in stock_data.items():
        funds     = d["funds"]
        n         = len(funds)
        max_pct   = max(f["pct"] for f in funds)
        avg_pct   = sum(f["pct"] for f in funds) / n
        total_val = sum(f["value_k"] for f in funds)
        score     = n * max_pct   # 家数 × 最高单仓
        ranked.append((score, cusip, d["issuer"], d["ticker"],
                       n, avg_pct, max_pct, total_val, funds))

    ranked.sort(reverse=True)

    print(f"\n  {'持仓':<34} {'Ticker':<8} {'家数':>4} {'池均%':>6} "
          f"{'最高%':>6} {'总$B':>8}  代表基金")
    print(f"  {'─'*70}")

    for score, cusip, issuer, ticker, n, avg_pct, max_pct, total_val, funds in ranked[:top_n]:
        top_fund = sorted(funds, key=lambda x: -x["pct"])[0]
        stars    = "★★★" if max_pct >= 15 else "★★ " if max_pct >= 8 else "★  "
        print(f"  {issuer[:33]:<34} {ticker:<8} {n:>4} "
              f"{avg_pct:>5.1f}% {max_pct:>5.1f}%  "
              f"{total_val/1e6:>7.1f}B  "
              f"{stars} {top_fund['fname']} {top_fund['pct']:.0f}%")


# ── B5  价格背离 ──────────────────────────────────────────────────────────────

def print_price_divergence(changes: dict, curr_q: dict,
                           ct_cache: dict, price_cache: dict):
    """对 B2/B3 信号中的主要股票做季末价 vs 今日价对比。"""
    print(f"\n{'━'*72}")
    print(f"  【B5】价格背离  主要信号股票  季末买入价 vs 今日价")
    print(f"{'━'*72}")

    # 取净信号最强的前15只
    scored = []
    for cusip, acts in changes.items():
        _, net, _ = net_signal(acts)
        if abs(net) < 2:
            continue
        issuer = acts[0]["issuer"]
        ticker = get_ticker(cusip, ct_cache)
        # 找最近一期的 period（用于季末价）
        period = next((a["period"] for a in acts if a["period"]), None)
        scored.append((abs(net), net, cusip, issuer, ticker, period))

    scored.sort(reverse=True)

    print(f"\n  {'持仓':<32} {'Ticker':<8} {'季末价':>8} {'今日价':>8} "
          f"{'涨跌幅':>8}  信号")
    print(f"  {'─'*72}")

    fetched = 0
    for _, net, cusip, issuer, ticker, period in scored[:15]:
        if not ticker or not period or "/" in ticker or len(ticker) > 10:
            direction = "▲" if net > 0 else "▼"
            print(f"  {issuer[:31]:<32} {'':<8} {'—':>8} {'—':>8} {'—':>8}"
                  f"  {direction}{abs(net)}家  (无ticker)")
            continue

        p_end  = price_at(ticker, period, price_cache)
        p_now  = current_price(ticker, price_cache)
        fetched += 1
        if fetched % 10 == 0:
            save_json(PRICE_CACHE, price_cache)

        direction = "▲" if net > 0 else "▼"
        net_str   = f"{direction}{abs(net)}家"

        if p_end and p_now:
            chg = (p_now - p_end) / p_end * 100
            if   chg >  30: sig = "⚠  大涨，评估是否priced in"
            elif chg >  15: sig = "↑  上涨，关注估值"
            elif chg < -20: sig = "↓  大跌，验证thesis"
            elif chg < -10: sig = "↓  下跌，留意"
            else:           sig = "≈  接近季末价"
            print(f"  {issuer[:31]:<32} {ticker:<8} ${p_end:>7.1f} ${p_now:>7.1f} "
                  f"{chg:>+7.1f}%  {net_str}  {sig}")
        else:
            print(f"  {issuer[:31]:<32} {ticker:<8} {'—':>8} {'—':>8} {'—':>8}"
                  f"  {net_str}  (价格获取失败)")

    save_json(PRICE_CACHE, price_cache)


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    # 支持 --quarter 参数指定季度，默认最新
    target_q = None
    if "--quarter" in sys.argv:
        idx = sys.argv.index("--quarter")
        if idx + 1 < len(sys.argv):
            target_q = sys.argv[idx + 1]

    conn    = sqlite3.connect(DB_PATH)
    quarters = load_quarters(conn)

    if not quarters:
        print("  ✗ holdings.db 无数据")
        conn.close()
        return

    curr_q_label = target_q or quarters[-1]
    prev_q_label = quarters[quarters.index(curr_q_label) - 1] \
        if curr_q_label in quarters and quarters.index(curr_q_label) > 0 else None

    ct_cache    = load_json(CT_CACHE)
    price_cache = load_json(PRICE_CACHE)

    print(f"\n{'═'*72}")
    print(f"  Module B  个股信号")
    print(f"  对比: {prev_q_label} → {curr_q_label}")
    print(f"{'═'*72}")

    curr_q = load_quarter_holdings(conn, curr_q_label)
    prev_q = load_quarter_holdings(conn, prev_q_label) if prev_q_label else {}
    conn.close()

    n_funds = len(curr_q)
    n_pos   = sum(len(f["holdings"]) for f in curr_q.values())
    print(f"  本季: {n_funds} 家基金  {n_pos} 条持仓记录")

    changes = compute_changes(prev_q, curr_q)

    print_new_positions(changes, ct_cache, min_funds=2)
    print_consensus_moves(changes, ct_cache, direction="buy")
    print_consensus_moves(changes, ct_cache, direction="sell")
    print_sizing_heatmap(curr_q, ct_cache)
    print_price_divergence(changes, curr_q, ct_cache, price_cache)

    print(f"\n{'═'*72}\n")


if __name__ == "__main__":
    main()
