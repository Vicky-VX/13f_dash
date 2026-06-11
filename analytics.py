"""
analytics.py  ─  13F 持仓深度分析
从 backtest.db 读取已有数据，输出五类分析报告。

用法:
  python analytics.py --remind          季报季提醒
  python analytics.py --overlap         持仓重叠分析（群体共识 Top30）
  python analytics.py --new-positions   新仓预警（最新季度新建仓）
  python analytics.py --concentration   持仓集中度追踪（前5大仓位占比变化）
  python analytics.py --divergence      跨基金分歧指标（最有争议的股票）
  python analytics.py --all             以上全部
"""

import sys, json, sqlite3, argparse
from datetime import date, datetime
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BT_DB         = "backtest.db"
FUND_POOL_FILE = "fund_pool.json"


# ── 工具 ──────────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    if not Path(BT_DB).exists():
        print(f"[错误] 找不到 {BT_DB}，请先运行 python backtest.py --all")
        sys.exit(1)
    return sqlite3.connect(BT_DB)


def load_pool() -> list[dict]:
    with open(FUND_POOL_FILE, encoding="utf-8") as f:
        return json.load(f)["funds"]


def fmt_pct(v, decimals=1, na="N/A"):
    if v is None: return na
    return f"{v:.{decimals}f}%"


def fmt_pp(v, decimals=1, na="N/A"):
    if v is None: return na
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.{decimals}f}pp"


def latest_quarter_per_fund(conn) -> dict[str, str]:
    """返回每家基金（cik）在 bt_holdings 里最新的季度。"""
    rows = conn.execute("""
        SELECT cik, MAX(quarter) FROM bt_holdings GROUP BY cik
    """).fetchall()
    return {cik: q for cik, q in rows}


def prev_quarter(q: str) -> str:
    """'2024Q2' → '2024Q1'; '2024Q1' → '2023Q4'"""
    yr, qn = int(q[:4]), int(q[5])
    if qn == 1:
        return f"{yr-1}Q4"
    return f"{yr}Q{qn-1}"


# ── 功能一：季报季提醒 ────────────────────────────────────────────────────────

FILING_MONTHS = {2: "Q4(上年)", 5: "Q1", 8: "Q2", 11: "Q3"}

def feature_remind(conn):
    today = date.today()
    mo    = today.month
    yr    = today.year

    print(f"\n{'═'*72}")
    print(f"  【季报季提醒】  今日: {today}  披露季: {FILING_MONTHS.get(mo, '─')}")
    print(f"{'═'*72}")

    if mo not in FILING_MONTHS:
        # 计算距下次披露季还有多少天
        next_months = sorted(m for m in FILING_MONTHS if m > mo)
        if not next_months:
            next_months = sorted(FILING_MONTHS)
        nm = next_months[0]
        nyr = yr if nm > mo else yr + 1
        next_date = date(nyr, nm, 1)
        days_left = (next_date - today).days
        print(f"\n  当前不在披露季（2/5/8/11月）。")
        print(f"  下次披露季: {nyr}-{nm:02d}  还有 {days_left} 天\n")
        return

    # 当前季度标签（本期应披露的季度）
    filing_label = FILING_MONTHS[mo]
    if mo == 2:
        target_q = f"{yr-1}Q4"
    else:
        q_num = {5:1, 8:2, 11:3}[mo]
        target_q = f"{yr}Q{q_num}"

    pool = load_pool()
    # 查哪些基金已经披露了 target_q
    filed_ciks = {r[0] for r in conn.execute(
        "SELECT DISTINCT cik FROM bt_filings WHERE quarter=?", (target_q,)
    ).fetchall()}

    filed    = [f for f in pool if f["cik"] in filed_ciks]
    not_filed = [f for f in pool if f["cik"] not in filed_ciks]

    print(f"\n  目标季度: {target_q}  ({filing_label})  |  "
          f"已披露 {len(filed)}/{len(pool)} 家\n")

    print(f"  ✅ 已披露 ({len(filed)} 家):")
    for f in filed:
        row = conn.execute(
            "SELECT filing_date FROM bt_filings WHERE cik=? AND quarter=? LIMIT 1",
            (f["cik"], target_q)).fetchone()
        fd = row[0] if row else "─"
        print(f"     • {f['name'][:40]:<40}  申报日: {fd}")

    print(f"\n  ⏳ 尚未披露 ({len(not_filed)} 家):")
    for f in not_filed:
        # 显示最近一季披露日期
        last = conn.execute(
            "SELECT quarter, filing_date FROM bt_filings WHERE cik=? ORDER BY quarter DESC LIMIT 1",
            (f["cik"],)).fetchone()
        last_str = f"上季: {last[0]} ({last[1]})" if last else "暂无数据"
        print(f"     • {f['name'][:40]:<40}  {last_str}")
    print()


# ── 功能二：持仓重叠分析 ──────────────────────────────────────────────────────

def feature_overlap(conn, top_n: int = 30):
    """统计最近一季各基金共同持有的股票。"""
    latest = latest_quarter_per_fund(conn)
    if not latest:
        print("  无持仓数据"); return

    # 取每家基金最新季度的持仓
    fund_tickers: dict[str, set] = defaultdict(set)   # cik → {ticker}
    ticker_funds:  dict[str, list] = defaultdict(list) # ticker → [(fund_name, pct, action)]
    ticker_value:  dict[str, float] = defaultdict(float)

    for cik, q in latest.items():
        rows = conn.execute("""
            SELECT h.ticker, h.issuer, h.pct, h.value_k, h.action, h.fund_name
            FROM bt_holdings h
            WHERE h.cik=? AND h.quarter=? AND h.ticker != ''
        """, (cik, q)).fetchall()
        for ticker, issuer, pct, value_k, action, fname in rows:
            fund_tickers[cik].add(ticker)
            ticker_funds[ticker].append((fname, pct, action))
            ticker_value[ticker] += value_k or 0

    # 排序：按持有基金数降序，再按总持仓价值降序
    ranked = sorted(
        [(t, fs) for t, fs in ticker_funds.items() if len(fs) >= 2],
        key=lambda x: (-len(x[1]), -ticker_value[x[0]])
    )[:top_n]

    total_funds = len(latest)
    print(f"\n{'═'*80}")
    print(f"  【持仓重叠分析】群体共识 Top{top_n}  ─  基于 {total_funds} 家基金最新季度")
    print(f"{'═'*80}")
    print(f"  {'股票代码':<8} {'持有家数':>6} {'覆盖率':>7} {'总持仓(百万)':>12}  主要基金")
    print(f"  {'─'*80}")

    for ticker, fund_list in ranked:
        n_funds   = len(fund_list)
        coverage  = n_funds / total_funds * 100
        total_val = ticker_value[ticker] / 1000   # k → M
        # 取仓位最重的3家
        top3 = sorted(fund_list, key=lambda x: x[1], reverse=True)[:3]
        top3_str = "  ".join(f"{n[:14]}({p:.1f}%)" for n, p, a in top3)
        print(f"  {ticker:<8} {n_funds:>6} {coverage:>6.0f}%  ${total_val:>10,.0f}M  {top3_str}")

    # 高共识板块摘要
    print(f"\n  共识分层:")
    bins = [(5, "★★★★ 超高共识"), (4, "★★★  高共识"), (3, "★★   中共识"), (2, "★    低共识")]
    for threshold, label in bins:
        items = [t for t, fs in ticker_funds.items() if len(fs) >= threshold]
        print(f"    {label}: {len(items)} 只")
    print()


# ── 功能三：新仓预警 ─────────────────────────────────────────────────────────

def feature_new_positions(conn):
    latest = latest_quarter_per_fund(conn)
    if not latest:
        print("  无持仓数据"); return

    # 找全局最新季度（多数基金一致的那个季度）
    from collections import Counter
    most_common_q = Counter(latest.values()).most_common(1)[0][0]

    rows = conn.execute("""
        SELECT h.fund_name, h.ticker, h.issuer, h.pct, h.value_k, h.quarter
        FROM bt_holdings h
        WHERE h.action = 'NEW'
          AND h.quarter = ?
          AND h.ticker != ''
        ORDER BY h.value_k DESC
    """, (most_common_q,)).fetchall()

    print(f"\n{'═'*80}")
    print(f"  【新仓预警】{most_common_q} 新建仓  ─  共 {len(rows)} 个信号")
    print(f"{'═'*80}")

    if not rows:
        print("  本季无新建仓信号\n")
        return

    # 按 ticker 聚合（多家同时新建仓的更重要）
    by_ticker: dict[str, list] = defaultdict(list)
    for fname, ticker, issuer, pct, value_k, q in rows:
        by_ticker[ticker].append((fname, pct, value_k))

    # 被多家同时新建的放最前
    ranked = sorted(by_ticker.items(),
                    key=lambda x: (-len(x[1]), -sum(v for _, _, v in x[1])))

    print(f"  {'股票代码':<8} {'新建仓家数':>8} {'总仓位(百万)':>12}  基金列表")
    print(f"  {'─'*78}")
    multi = [(t, fl) for t, fl in ranked if len(fl) > 1]
    single = [(t, fl) for t, fl in ranked if len(fl) == 1]

    if multi:
        print(f"\n  🔥 多家同时新建仓 ({len(multi)} 只):")
        for ticker, fund_list in multi:
            total_val = sum(v for _, _, v in fund_list) / 1000
            funds_str = "  ".join(f"{n[:16]}({p:.1f}%)" for n, p, v in
                                   sorted(fund_list, key=lambda x: x[1], reverse=True))
            print(f"  {ticker:<8} {len(fund_list):>8} ${total_val:>10,.0f}M  {funds_str}")

    print(f"\n  📋 单家新建仓 ({len(single)} 只):")
    print(f"  {'基金':<30} {'股票':>6} {'仓位':>6} {'持仓(百万)':>10}  公司名称")
    print(f"  {'─'*78}")
    for ticker, fund_list in single[:40]:  # 最多显示 40 条
        fname, pct, value_k = fund_list[0]
        issuer_row = conn.execute(
            "SELECT issuer FROM bt_holdings WHERE ticker=? LIMIT 1", (ticker,)).fetchone()
        issuer = issuer_row[0][:28] if issuer_row else ""
        print(f"  {fname[:28]:<30} {ticker:>6} {pct:>5.1f}%  ${value_k/1000:>8,.0f}M  {issuer}")
    if len(single) > 40:
        print(f"  ... 还有 {len(single)-40} 条")
    print()


# ── 功能四：持仓集中度追踪 ───────────────────────────────────────────────────

def feature_concentration(conn, top_n: int = 5):
    """追踪每家基金前 N 大持仓占比的历史变化。"""
    rows = conn.execute("""
        SELECT cik, fund_name, quarter, pct
        FROM bt_holdings
        WHERE ticker != ''
        ORDER BY cik, quarter, pct DESC
    """).fetchall()

    # 按 (cik, quarter) 分组，计算 top-N 集中度
    from itertools import groupby
    by_fund_q: dict[tuple, list] = defaultdict(list)
    for cik, fname, q, pct in rows:
        by_fund_q[(cik, fname, q)].append(pct)

    # 按基金聚合：计算每季 top-N 集中度
    fund_conc: dict[str, dict[str, float]] = defaultdict(dict)
    fund_names: dict[str, str] = {}
    for (cik, fname, q), pcts in by_fund_q.items():
        top_sum = sum(sorted(pcts, reverse=True)[:top_n])
        fund_conc[cik][q] = top_sum
        fund_names[cik] = fname

    print(f"\n{'═'*90}")
    print(f"  【持仓集中度追踪】前 {top_n} 大仓位占比  ─  ↑集中度上升=信念增强  ↓下降=分散化")
    print(f"{'═'*90}")

    # 对每家基金计算趋势（最新季 vs 4季前）
    summary = []
    for cik, q_map in fund_conc.items():
        qs = sorted(q_map.keys())
        if len(qs) < 2:
            continue
        latest_q  = qs[-1]
        oldest_q  = qs[0]
        conc_now  = q_map[latest_q]
        conc_base = q_map[qs[-min(5, len(qs))]]  # 最近4季前
        delta = conc_now - conc_base
        summary.append((cik, fund_names[cik], qs, q_map, delta, conc_now))

    summary.sort(key=lambda x: -abs(x[4]))  # 变化最大的排前面

    for cik, fname, qs, q_map, delta, conc_now in summary:
        trend = "↑" if delta > 0 else ("↓" if delta < 0 else "─")
        arrow_color = "🔺" if delta > 3 else ("🔻" if delta < -3 else "")
        recent_qs = qs[-8:]  # 最近8季
        conc_str  = "  ".join(f"{q}:{q_map[q]:.0f}%" for q in recent_qs)
        print(f"\n  {fname[:36]}")
        print(f"    {conc_str}")
        print(f"    当前: {conc_now:.1f}%  趋势: {trend}{delta:+.1f}pp  {arrow_color}")
    print()


# ── 功能五：跨基金分歧指标 ───────────────────────────────────────────────────

def feature_divergence(conn, min_funds: int = 3, top_n: int = 25):
    """
    对于同时被多家基金持有的股票，
    计算加仓家数 vs 减仓家数，分歧越大越值得关注。
    """
    latest = latest_quarter_per_fund(conn)
    if not latest:
        print("  无持仓数据"); return

    # 对每家基金，比较最新季 vs 上季的持仓变化
    ticker_actions: dict[str, dict] = defaultdict(lambda: {"add":[], "cut":[], "hold":[], "new":[], "exit":[]})

    for cik, curr_q in latest.items():
        prev_q = prev_quarter(curr_q)

        curr_rows = conn.execute("""
            SELECT ticker, shares, pct, fund_name FROM bt_holdings
            WHERE cik=? AND quarter=? AND ticker != ''
        """, (cik, curr_q)).fetchall()

        prev_map = {r[0]: (r[1], r[2]) for r in conn.execute("""
            SELECT ticker, shares, pct FROM bt_holdings
            WHERE cik=? AND quarter=? AND ticker != ''
        """, (cik, prev_q)).fetchall()}

        prev_tickers = set(prev_map.keys())
        curr_tickers = {r[0] for r in curr_rows}

        for ticker, shares, pct, fname in curr_rows:
            if ticker not in prev_map:
                ticker_actions[ticker]["new"].append(fname)
            else:
                prev_sh, prev_pct = prev_map[ticker]
                pct_diff = pct - prev_pct
                sh_ratio = (shares - prev_sh) / prev_sh if prev_sh else 0
                if pct_diff >= 2.0 or sh_ratio >= 0.08:
                    ticker_actions[ticker]["add"].append(fname)
                elif pct_diff <= -2.0 or sh_ratio <= -0.08:
                    ticker_actions[ticker]["cut"].append(fname)
                else:
                    ticker_actions[ticker]["hold"].append(fname)

        # 本季退出的
        for ticker in prev_tickers - curr_tickers:
            ticker_actions[ticker]["exit"].append(
                conn.execute("SELECT fund_name FROM bt_holdings WHERE cik=? AND quarter=? AND ticker=? LIMIT 1",
                             (cik, prev_q, ticker)).fetchone()[0] if True else "")

    # 计算分歧分 = (加仓家+新建仓) 与 (减仓家+退出家) 中，较小那方的绝对值
    divergence = []
    for ticker, acts in ticker_actions.items():
        bulls = len(acts["add"]) + len(acts["new"])
        bears = len(acts["cut"]) + len(acts["exit"])
        total = bulls + bears + len(acts["hold"])
        if total < min_funds:
            continue
        div_score = min(bulls, bears)      # 双方都不为0才叫分歧
        if div_score == 0:
            continue
        divergence.append((ticker, bulls, bears, total, acts))

    divergence.sort(key=lambda x: (-x[3], -(x[1]+x[2])))  # 覆盖面广且分歧明显
    top = divergence[:top_n]

    print(f"\n{'═'*90}")
    print(f"  【跨基金分歧指标】最有争议的股票  ─  双方都有人加仓/减仓的才算分歧")
    print(f"{'═'*90}")
    print(f"  {'股票':>6} {'看多':>5} {'看空':>5} {'持有':>5}  看多基金(加仓/新建仓)  vs  看空基金(减仓/退出)")
    print(f"  {'─'*88}")

    for ticker, bulls, bears, total, acts in top:
        bull_names = (acts["add"] + acts["new"])[:3]
        bear_names = (acts["cut"] + acts["exit"])[:3]
        bull_str = ", ".join(n[:12] for n in bull_names)
        bear_str = ", ".join(n[:12] for n in bear_names)
        bar_b = "▲" * bulls
        bar_s = "▽" * bears
        print(f"  {ticker:>6} {bulls:>5} {bears:>5} {total:>5}  "
              f"{bar_b:<6} {bull_str[:30]:<30}  {bar_s:<6} {bear_str[:30]}")

    # 特别提示：被最多家减仓的股票
    most_cut = sorted(ticker_actions.items(),
                      key=lambda x: -(len(x[1]["cut"]) + len(x[1]["exit"])))[:5]
    print(f"\n  🚨 被最多家减仓/退出（潜在卖压）:")
    for ticker, acts in most_cut:
        n = len(acts["cut"]) + len(acts["exit"])
        names = (acts["cut"] + acts["exit"])[:4]
        print(f"     {ticker:<8} {n} 家  {', '.join(n[:14] for n in names)}")

    most_add = sorted(ticker_actions.items(),
                      key=lambda x: -(len(x[1]["add"]) + len(x[1]["new"])))[:5]
    print(f"\n  🚀 被最多家加仓/新建仓（潜在买力）:")
    for ticker, acts in most_add:
        n = len(acts["add"]) + len(acts["new"])
        names = (acts["add"] + acts["new"])[:4]
        print(f"     {ticker:<8} {n} 家  {', '.join(n[:14] for n in names)}")
    print()


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="13F 持仓深度分析")
    parser.add_argument("--remind",        action="store_true", help="季报季提醒")
    parser.add_argument("--overlap",       action="store_true", help="持仓重叠分析")
    parser.add_argument("--new-positions", action="store_true", help="新仓预警")
    parser.add_argument("--concentration", action="store_true", help="持仓集中度追踪")
    parser.add_argument("--divergence",    action="store_true", help="跨基金分歧指标")
    parser.add_argument("--all",           action="store_true", help="全部分析")
    parser.add_argument("--top",           type=int, default=30, help="排行显示条数（默认30）")
    args = parser.parse_args()

    if not any([args.remind, args.overlap, args.new_positions,
                args.concentration, args.divergence, args.all]):
        parser.print_help()
        return

    conn = get_conn()

    run_all   = args.all
    run_remind = args.remind or run_all
    run_overlap = args.overlap or run_all
    run_new    = args.new_positions or run_all
    run_conc   = args.concentration or run_all
    run_div    = args.divergence or run_all

    if run_remind:
        feature_remind(conn)
    if run_overlap:
        feature_overlap(conn, top_n=args.top)
    if run_new:
        feature_new_positions(conn)
    if run_conc:
        feature_concentration(conn)
    if run_div:
        feature_divergence(conn, top_n=args.top)

    conn.close()


if __name__ == "__main__":
    main()
