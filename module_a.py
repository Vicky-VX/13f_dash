"""
module_a.py  ─  行业聚焦分析（多基金版）
读取 holdings.db，跨基金做行业聚合分析。

A1  行业热力图：各季各行业的持有基金数 + 池子总配置
A2  资金轮动：QoQ 基金数净变化 + 资金净流向
A3  共识信号：多家基金同时同向操作的行业（最高价值信号）

运行: python module_a.py
"""

import sys
import re
import json
import time
import sqlite3
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH      = "holdings.db"
SECTOR_CACHE = Path("sector_cache.json")

# ── 已知 CUSIP → Ticker ───────────────────────────────────────────────────────
# 随数据增长持续补充；不在此表的走 yfinance 名称查询兜底
CUSIP_TO_TICKER: dict[str, str] = {
    # TCI
    "02079K107": "GOOGL", "02079K305": "GOOGL",
    "369604301": "GE",    "92826C839": "V",
    "615369105": "MCO",   "78409V104": "SPGI",
    "13646K108": "CP",    "136375102": "CNI",
    "594918104": "MSFT",  "N3168P101": "FER.MC",
    # 常见大盘股
    "037833100": "AAPL",  "67066G104": "NVDA",
    "02079K107": "GOOGL", "30303M102": "META",
    "88160R101": "TSLA",  "023135106": "AMZN",
    "17275R102": "CSCO",  "458140100": "INTC",
    "191216100": "KO",    "718172109": "PFE",
    "742718109": "PG",    "931142103": "WMT",
    "097023105": "BA",    "38141G104": "GS",
    "46625H100": "JPM",   "BAC001001": "BAC",
    "172967424": "C",     "92343V104": "VZ",
    "00724F101": "ADBE",  "09857L108": "BLK",
    "20441N106": "CPRT",  "44106M102": "HUM",
    "585521107": "MCD",   "717081103": "PEP",
    "832696405": "SHW",   "855244109": "STZ",
    "025816109": "AXP",   "084670702": "BRK.B",
    "69343P105": "PPG",   "760759100": "ROP",
    "40171V100": "GWW",   "91282CAC5": "",  # Treasury, skip
    "549463108": "LLY",   "879585109": "TDG",
    "72352L106": "PINS",  "29786A106": "ETSY",
    "G0750C108": "ARGX",  "05765K105": "AZPN",
    "U09432101": "ABNB",  "30231G102": "EXPE",
    "N07059210": "ASML",  "G06940107": "AVGO",  # Broadcom
    "G54050102": "IHG",   "G3922B107": "FLUT",  # Flutter
    "H01301128": "ALCON", "H01301128": "ALC",
    "00206R102": "T",     "032511107": "AMGN",
    "002824100": "ABT",   "00287Y109": "ABBV",
    "053015103": "AIG",   "820081105": "SFM",
    "42809H107": "HCA",   "254687106": "DIS",
    "46090E103": "ICE",   "50076Q106": "KVYO",
    "191791108": "KKR",   "14448C104": "CARR",
    "G20567101": "CRH",   "81211K100": "SHOP",
    "G8T5AN108": "SPOT",  "88339J105": "TMUS",
}

MANUAL_SECTOR: dict[str, str] = {
    "FER.MC":  "Industrials",
    "FLUT":    "Consumer Discretionary",
    "ALC":     "Health Care",
    "ARGX":    "Health Care",
    "AZPN":    "Technology",
    "CRH":     "Materials",
    "SHOP":    "Technology",
    "SPOT":    "Communication Services",
}

# 先把 yfinance 返回的各种别名统一到标准行业名
SECTOR_NORMALIZE = {
    "Financial Services":     "Financials",
    "Consumer Cyclical":      "Consumer Discretionary",
    "Consumer Defensive":     "Consumer Staples",
    "Basic Materials":        "Materials",
    "Health Care":            "Healthcare",
    "Healthcare":             "Healthcare",
}

SECTOR_SHORT = {
    "Technology":             "Technology",
    "Communication Services": "Comm Svcs",
    "Financials":             "Financials",
    "Industrials":            "Industrials",
    "Healthcare":             "Healthcare",
    "Consumer Discretionary": "Cons Disc",
    "Consumer Staples":       "Cons Stpl",
    "Energy":                 "Energy",
    "Materials":              "Materials",
    "Real Estate":            "Real Est",
    "Utilities":              "Utilities",
    "Other":                  "Other",
}

# 从 issuer 名清洗出可能的 ticker（启发式）
_STRIP_SUFFIXES = re.compile(
    r'\s+(INC\.?|CORP\.?|CO\.?|LTD\.?|LLC\.?|PLC\.?|SE$|NV$|SA$|AG$'
    r'|GROUP|HLDGS?|HOLDINGS?|INTL|INTERNATIONAL|TECHNOLOGIES?'
    r'|PHARMA|THERAPEUTICS|BIOSCIENCES?|CAPITAL|PARTNERS?|FUND'
    r'|CLASS\s+[ABC]|CL\s+[ABC]|CAP\s+STK.*|COM(?:MON)?(\s+STK)?'
    r'|N\s?Y\s+REGISTRY\s+SHS|ORD\s+SHS?|ADR|ADS)\s*$',
    re.IGNORECASE
)


def clean_name_to_ticker(issuer: str) -> str:
    name = issuer.upper().strip()
    name = _STRIP_SUFFIXES.sub("", name).strip()
    name = re.sub(r"[^A-Z0-9\s]", "", name).strip()
    # 多词取首词（很多股票就是公司简称）
    parts = name.split()
    return parts[0] if parts else ""


# ── 行业查询（带缓存）────────────────────────────────────────────────────────

def load_cache() -> dict:
    return json.loads(SECTOR_CACHE.read_text(encoding="utf-8")) if SECTOR_CACHE.exists() else {}


def save_cache(cache: dict):
    SECTOR_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_sector(cusip: str, issuer: str, cache: dict) -> str:
    if cusip in cache:
        raw = SECTOR_NORMALIZE.get(cache[cusip], cache[cusip])
        return SECTOR_SHORT.get(raw, raw)

    ticker = CUSIP_TO_TICKER.get(cusip, "")

    if not ticker:
        ticker = clean_name_to_ticker(issuer)

    if ticker in MANUAL_SECTOR:
        sector = MANUAL_SECTOR[ticker]
        cache[cusip] = sector
        return SECTOR_SHORT.get(sector, sector)

    if ticker:
        try:
            import yfinance as yf
            info   = yf.Ticker(ticker).info
            sector = info.get("sector") or "Other"
            cache[cusip] = sector
            time.sleep(0.25)
            return SECTOR_SHORT.get(sector, sector)
        except Exception:
            pass

    cache[cusip] = "Other"
    return "Other"


# ── 数据加载 & 行业聚合 ────────────────────────────────────────────────────────

def load_holdings(conn) -> tuple[list[str], dict]:
    """
    返回 (sorted_quarters, by_quarter)
    by_quarter[q] = list of {cik, fund_name, cusip, issuer, value_k, pct, sector}
    """
    cache = load_cache()

    rows = conn.execute("""
        SELECT f.cik, fd.name, f.quarter, h.cusip, h.issuer,
               h.value, h.pct, h.put_call
        FROM holdings h
        JOIN filings  f  ON h.filing_id = f.id
        JOIN funds    fd ON f.cik = fd.cik
        WHERE (h.put_call IS NULL OR h.put_call = '')
        ORDER BY f.quarter, f.cik, h.value DESC
    """).fetchall()

    by_quarter: dict[str, list] = defaultdict(list)
    for cik, fname, quarter, cusip, issuer, value, pct, _ in rows:
        sector = get_sector(cusip, issuer, cache)
        by_quarter[quarter].append({
            "cik": cik, "fund": fname, "quarter": quarter,
            "cusip": cusip, "issuer": issuer,
            "value_k": value, "pct": pct, "sector": sector,
        })

    save_cache(cache)
    return sorted(by_quarter.keys()), dict(by_quarter)


def sector_stats_per_fund(holdings: list[dict]) -> dict[str, dict[str, dict]]:
    """
    返回 {cik: {sector: {value_k, pct}}}
    每只基金在每个行业的敞口。
    """
    # 先按 (cik, sector) 聚合
    result: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"value_k": 0, "pct": 0.0}))
    fund_total: dict[str, int] = defaultdict(int)

    for h in holdings:
        result[h["cik"]][h["sector"]]["value_k"] += h["value_k"]
        fund_total[h["cik"]] += h["value_k"]

    # 计算各行业在该基金的占比
    for cik, sectors in result.items():
        total = fund_total[cik]
        for sec, data in sectors.items():
            data["pct"] = data["value_k"] / total * 100 if total else 0.0

    return dict(result)


def pool_sector_summary(by_quarter: dict, quarters: list[str]) -> dict[str, dict[str, dict]]:
    """
    返回 {quarter: {sector: {fund_count, total_value_k, pool_pct, funds:[cik...]}}}
    """
    result = {}
    for q in quarters:
        holdings = by_quarter[q]
        per_fund = sector_stats_per_fund(holdings)
        pool_total = sum(h["value_k"] for h in holdings)

        sec_data: dict[str, dict] = defaultdict(
            lambda: {"fund_count": 0, "total_value_k": 0, "pool_pct": 0.0, "funds": []}
        )
        for cik, sectors in per_fund.items():
            for sec, data in sectors.items():
                if data["value_k"] > 0:
                    sec_data[sec]["fund_count"]   += 1
                    sec_data[sec]["total_value_k"] += data["value_k"]
                    sec_data[sec]["funds"].append(cik)

        # 排除 Other 计算占比，使百分比更有意义
        pool_total_k = sum(d["total_value_k"] for s, d in sec_data.items() if s != "Other")
        for sec in sec_data:
            sec_data[sec]["pool_pct"] = (
                sec_data[sec]["total_value_k"] / pool_total_k * 100
                if pool_total_k else 0.0
            )

        result[q] = dict(sec_data)
    return result


# ── A1  行业热力图 ─────────────────────────────────────────────────────────────

def bar(pct: float, width: int = 5) -> str:
    filled = int(pct / (100 / width))
    return "█" * filled + "░" * (width - filled)


def trend_arrow(vals: list[float]) -> str:
    if len(vals) < 2: return " "
    d = vals[-1] - vals[-2]
    if d > 2:  return "↑↑"
    if d > .5: return "↑ "
    if d < -2: return "↓↓"
    if d < -.5:return "↓ "
    return "─ "


def print_heatmap(quarters: list[str], pool_summary: dict):
    # 找最新季所有行业，按基金数排序
    latest = pool_summary[quarters[-1]]
    all_sectors = sorted(latest.keys(),
                         key=lambda s: -latest[s]["fund_count"])

    n_funds_total = max(
        len(set(h["cik"] for h in []))
        for h in [[]]
    ) if False else None

    print(f"\n{'━'*78}")
    print(f"  【A1】行业热力图  （基金数 / 占池子%）")
    print(f"{'━'*78}")

    # 表头
    hdr = f"  {'行业':<14}"
    for q in quarters:
        hdr += f"  {q[-4:]:^13}"
    hdr += "  趋势"
    print(hdr)
    print(f"  {'─'*74}")

    for sec in all_sectors:
        if sec == "Other":
            continue
        vals_fc  = [pool_summary[q].get(sec, {}).get("fund_count", 0)    for q in quarters]
        vals_pct = [pool_summary[q].get(sec, {}).get("pool_pct",   0.0)  for q in quarters]
        if max(vals_fc) == 0:
            continue

        row = f"  {SECTOR_SHORT.get(sec, sec):<14}"
        for fc, pct in zip(vals_fc, vals_pct):
            row += f"  {bar(pct)} {fc:>2}家 {pct:>4.1f}%"
        row += f"  {trend_arrow(vals_fc)}"
        print(row)

    # Other 行
    vals_other = [pool_summary[q].get("Other", {}).get("fund_count", 0) for q in quarters]
    if max(vals_other) > 0:
        row = f"  {'Other':<14}"
        for q in quarters:
            fc  = pool_summary[q].get("Other", {}).get("fund_count", 0)
            pct = pool_summary[q].get("Other", {}).get("pool_pct",  0.0)
            row += f"  {bar(pct)} {fc:>2}家 {pct:>4.1f}%"
        print(row)


# ── A2  资金轮动 ───────────────────────────────────────────────────────────────

def print_rotation(quarters: list[str], pool_summary: dict, by_quarter: dict):
    print(f"\n{'━'*78}")
    print(f"  【A2】资金轮动  QoQ 基金数净变化 + 资金净流向")
    print(f"{'━'*78}")

    for i in range(1, len(quarters)):
        pq, cq = quarters[i-1], quarters[i]
        prev = pool_summary[pq]
        curr = pool_summary[cq]
        all_secs = set(prev) | set(curr)

        changes = []
        for sec in all_secs:
            if sec == "Other":
                continue
            fc_diff  = curr.get(sec, {}).get("fund_count",    0) - prev.get(sec, {}).get("fund_count",    0)
            pct_diff = curr.get(sec, {}).get("pool_pct",    0.0) - prev.get(sec, {}).get("pool_pct",    0.0)
            val_curr = curr.get(sec, {}).get("total_value_k", 0)
            val_prev = prev.get(sec, {}).get("total_value_k", 0)
            val_diff_b = (val_curr - val_prev) / 1e6
            if abs(fc_diff) > 0 or abs(pct_diff) > 0.5:
                changes.append((sec, fc_diff, pct_diff, val_diff_b,
                                curr.get(sec, {}).get("fund_count", 0),
                                curr.get(sec, {}).get("pool_pct",   0.0)))

        changes.sort(key=lambda x: -abs(x[1]))
        if not changes:
            continue

        print(f"\n  {pq} → {cq}")
        print(f"  {'行业':<14} {'基金数变化':>8} {'池子%变化':>9} {'资金净流':>10}  当前")
        print(f"  {'─'*62}")

        inflows  = [c for c in changes if c[1] > 0 or c[2] > 0.5]
        outflows = [c for c in changes if c[1] < 0 or c[2] < -0.5]

        for sec, fc_d, pct_d, val_d, fc_curr, pct_curr in sorted(inflows,  key=lambda x: -x[1]):
            arrow = "▶▶" if fc_d >= 3 else "▶ " if fc_d > 0 else "  "
            print(f"  {SECTOR_SHORT.get(sec,sec):<14} {fc_d:>+4}家  {pct_d:>+7.1f}pp  "
                  f"{val_d:>+8.1f}B  {arrow} {fc_curr}家 {pct_curr:.1f}%")
        for sec, fc_d, pct_d, val_d, fc_curr, pct_curr in sorted(outflows, key=lambda x: x[1]):
            arrow = "◀◀" if fc_d <= -3 else "◀ " if fc_d < 0 else "  "
            print(f"  {SECTOR_SHORT.get(sec,sec):<14} {fc_d:>+4}家  {pct_d:>+7.1f}pp  "
                  f"{val_d:>+8.1f}B  {arrow} {fc_curr}家 {pct_curr:.1f}%")


# ── A3  共识信号 ───────────────────────────────────────────────────────────────

def compute_fund_sector_changes(quarters: list[str], by_quarter: dict) -> dict:
    """
    返回 {sector: {quarter: {cik: action}}}
    action: 'NEW' | 'ADD' | 'TRIM' | 'EXIT' | 'HOLD'
    """
    # 先算每个基金每季在每个行业的仓位
    fund_sec_pct: dict[str, dict[str, dict[str, float]]] = {}
    # {quarter: {cik: {sector: pct}}}

    for q in quarters:
        fund_sec_pct[q] = {}
        per_fund = sector_stats_per_fund(by_quarter[q])
        for cik, secs in per_fund.items():
            fund_sec_pct[q][cik] = {s: d["pct"] for s, d in secs.items()}

    # 计算变化
    result: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))

    all_ciks = set()
    for q in quarters:
        all_ciks.update(fund_sec_pct[q].keys())

    all_secs = set()
    for q in quarters:
        for cik_data in fund_sec_pct[q].values():
            all_secs.update(cik_data.keys())

    for i in range(1, len(quarters)):
        pq, cq = quarters[i-1], quarters[i]
        for sec in all_secs:
            for cik in all_ciks:
                prev_pct = fund_sec_pct[pq].get(cik, {}).get(sec, 0.0)
                curr_pct = fund_sec_pct[cq].get(cik, {}).get(sec, 0.0)
                if curr_pct == 0 and prev_pct == 0:
                    continue
                diff = curr_pct - prev_pct
                if prev_pct == 0 and curr_pct > 0:
                    action = "NEW"
                elif curr_pct == 0 and prev_pct > 0:
                    action = "EXIT"
                elif diff > 1.5:
                    action = "ADD"
                elif diff < -1.5:
                    action = "TRIM"
                else:
                    action = "HOLD"
                result[sec][cq][cik] = action

    return result


def print_consensus(quarters: list[str], by_quarter: dict, conn):
    print(f"\n{'━'*78}")
    print(f"  【A3】共识信号  多家基金同时同向操作（≥3家）")
    print(f"{'━'*78}")

    changes = compute_fund_sector_changes(quarters, by_quarter)

    # 获取基金名称映射
    fund_names = {r[0]: r[1] for r in conn.execute("SELECT cik, name FROM funds").fetchall()}

    found_signal = False
    for qi in range(1, len(quarters)):
        cq = quarters[qi]
        signals = []

        for sec, quarter_data in changes.items():
            if sec in ("Other", ) or cq not in quarter_data:
                continue
            q_actions = quarter_data[cq]

            adders  = [cik for cik, a in q_actions.items() if a in ("NEW", "ADD")]
            exitors = [cik for cik, a in q_actions.items() if a in ("EXIT", "TRIM")]

            if len(adders) >= 3:
                signals.append(("加仓", sec, adders,  len(adders)))
            if len(exitors) >= 3:
                signals.append(("减仓", sec, exitors, len(exitors)))

        if not signals:
            continue

        signals.sort(key=lambda x: -x[3])
        print(f"\n  ── {quarters[qi-1]} → {cq} ──────────────────────────────────────────")
        found_signal = True

        for direction, sec, ciks, count in signals:
            icon = "🔺" if direction == "加仓" else "🔻"
            sec_label = SECTOR_SHORT.get(sec, sec)
            names = [fund_names.get(c, c)[:20] for c in ciks]
            print(f"\n  {icon} {sec_label}  {direction} {count} 家")
            for n in names:
                print(f"      • {n}")

    if not found_signal:
        print(f"\n  暂无 ≥3 家同向操作的行业信号\n")

    # 跨季持续趋势总结
    print(f"\n  ── 最新季({quarters[-1]})行业持仓排行（按持有基金数）─────────────────")
    latest = pool_summary_latest = {}
    # 重用最后一季数据
    q_last = quarters[-1]
    per_fund_last = sector_stats_per_fund(by_quarter[q_last])
    sec_count: dict[str, int] = defaultdict(int)
    sec_value: dict[str, int] = defaultdict(int)
    for cik, secs in per_fund_last.items():
        for sec, data in secs.items():
            if data["value_k"] > 0:
                sec_count[sec] += 1
                sec_value[sec] += data["value_k"]

    total_val = sum(sec_value.values())
    ranked = sorted(sec_count.items(), key=lambda x: -x[1])
    print(f"  {'行业':<16} {'持有基金':>6}  {'总配置$B':>9}  {'占池子':>7}")
    print(f"  {'─'*50}")
    for sec, cnt in ranked:
        if sec == "Other":
            continue
        val_b   = sec_value[sec] / 1e6
        pool_pct = sec_value[sec] / total_val * 100 if total_val else 0
        print(f"  {SECTOR_SHORT.get(sec,sec):<16} {cnt:>5}家  {val_b:>9.1f}B  {pool_pct:>6.1f}%")
    print()


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)

    n_funds = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    print(f"\n{'═'*78}")
    print(f"  Module A  行业聚焦分析（多基金版）")
    print(f"  基金池: {n_funds} 家  |  数据库: {DB_PATH}")
    print(f"  正在加载数据和行业分类（首次运行较慢）...")
    print(f"{'═'*78}")

    quarters, by_quarter = load_holdings(conn)
    print(f"  覆盖季度: {' / '.join(quarters)}")

    pool_summary = pool_sector_summary(by_quarter, quarters)

    print_heatmap(quarters, pool_summary)
    print_rotation(quarters, pool_summary, by_quarter)
    print_consensus(quarters, by_quarter, conn)

    conn.close()
    print(f"{'═'*78}\n")


if __name__ == "__main__":
    main()
