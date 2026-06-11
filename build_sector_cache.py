"""
build_sector_cache.py
步骤：
  1. 从 holdings.db 取所有唯一 CUSIP
  2. 用 OpenFIGI API 批量转 Ticker（10条/请求，免费无需key）
  3. 用 yfinance 查 sector
  4. 写入 sector_cache.json

运行: python build_sector_cache.py
"""

import sys, json, time, sqlite3, urllib.request, urllib.error
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH      = "holdings.db"
CACHE_FILE   = Path("sector_cache.json")
CUSIP_TICKER = Path("cusip_ticker_cache.json")   # CUSIP→ticker 中间缓存

FIGI_URL     = "https://api.openfigi.com/v3/mapping"
FIGI_BATCH   = 10       # 每次最多10条
FIGI_SLEEP   = 2.5      # 无 API key 限速 25req/min → 每请求间隔2.5s

YFINANCE_SLEEP = 0.3


# ── 工具 ──────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Step 1：从 DB 取所有唯一 CUSIP ─────────────────────────────────────────────

def get_all_cusips() -> list[tuple[str, str]]:
    """返回 [(cusip, issuer), ...] 按总持仓市值降序（优先处理重要持仓）。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT cusip, issuer, SUM(value) as total
        FROM holdings
        WHERE put_call IS NULL OR put_call = ''
        GROUP BY cusip
        ORDER BY total DESC
    """).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


# ── Step 2：OpenFIGI 批量查 Ticker ────────────────────────────────────────────

def figi_batch(cusips: list[str]) -> dict[str, str]:
    """
    查询一批 CUSIP→ticker。
    返回 {cusip: ticker}，查不到的不在结果里。
    """
    body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in cusips]).encode()
    req  = urllib.request.Request(
        FIGI_URL, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            results = json.loads(r.read())
    except Exception as e:
        print(f"    OpenFIGI error: {e}")
        return {}

    out = {}
    for cusip, item in zip(cusips, results):
        data = item.get("data") or []
        # 优先选美股普通股
        for d in data:
            exch   = d.get("exchCode", "")
            stype  = d.get("securityType", "")
            ticker = d.get("ticker", "")
            if ticker and exch in ("US", "UW", "UN", "UA", "UT") \
               and "Common" in stype:
                out[cusip] = ticker
                break
        # 兜底：任意有 ticker 的
        if cusip not in out:
            for d in data:
                if d.get("ticker"):
                    out[cusip] = d["ticker"]
                    break
    return out


def build_cusip_ticker(all_cusips: list[tuple[str, str]]) -> dict[str, str]:
    """查 OpenFIGI，返回完整 {cusip: ticker} 映射。"""
    ct_cache = load_json(CUSIP_TICKER)
    cusips   = [c for c, _ in all_cusips]
    todo     = [c for c in cusips if c not in ct_cache]

    print(f"\n  Step 2: OpenFIGI 查询")
    print(f"  总 CUSIP: {len(cusips)}  已缓存: {len(ct_cache)}  待查: {len(todo)}")

    for i in range(0, len(todo), FIGI_BATCH):
        batch = todo[i:i+FIGI_BATCH]
        result = figi_batch(batch)
        for cusip in batch:
            ct_cache[cusip] = result.get(cusip, "")   # 空串代表查不到
        save_json(CUSIP_TICKER, ct_cache)

        found = sum(1 for c in batch if ct_cache.get(c))
        print(f"    [{i+len(batch):>4}/{len(todo)}]  本批找到 {found}/{len(batch)}", end="\r")
        time.sleep(FIGI_SLEEP)

    found_total = sum(1 for v in ct_cache.values() if v)
    print(f"\n  OpenFIGI 完成：{found_total}/{len(cusips)} 个 CUSIP 有 ticker")
    return ct_cache


# ── Step 3：yfinance 查 sector ────────────────────────────────────────────────

SECTOR_NORMALIZE = {
    "Financial Services": "Financials",
    "Consumer Cyclical":  "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Basic Materials":    "Materials",
}

def get_sector_yf(ticker: str) -> str:
    if not ticker:
        return "Other"
    try:
        import yfinance as yf
        info   = yf.Ticker(ticker).info
        sector = info.get("sector") or "Other"
        return SECTOR_NORMALIZE.get(sector, sector)
    except Exception:
        return "Other"


def build_sector_cache(ct_cache: dict[str, str]) -> dict[str, str]:
    """对每个有 ticker 的 CUSIP 查 yfinance sector，更新 sector_cache.json。"""
    sec_cache = load_json(CACHE_FILE)

    # 需要查的：有 ticker 且 sector 还是 Other 或未缓存
    todo = {
        cusip: ticker
        for cusip, ticker in ct_cache.items()
        if ticker and (cusip not in sec_cache or sec_cache.get(cusip) == "Other")
    }

    print(f"\n  Step 3: yfinance 查 sector")
    print(f"  待查: {len(todo)} 个 ticker")

    tickers_done: dict[str, str] = {}   # ticker→sector 避免重复查

    for i, (cusip, ticker) in enumerate(todo.items(), 1):
        if ticker in tickers_done:
            sec_cache[cusip] = tickers_done[ticker]
        else:
            sector = get_sector_yf(ticker)
            sec_cache[cusip] = sector
            tickers_done[ticker] = sector
            time.sleep(YFINANCE_SLEEP)

        if i % 20 == 0 or i == len(todo):
            save_json(CACHE_FILE, sec_cache)
            other_cnt = sum(1 for v in sec_cache.values() if v == "Other")
            print(f"    [{i:>4}/{len(todo)}]  Other 剩余: {other_cnt}", end="\r")

    save_json(CACHE_FILE, sec_cache)
    return sec_cache


# ── 汇总统计 ──────────────────────────────────────────────────────────────────

def print_stats(sec_cache: dict, all_cusips: list[tuple[str, str]]):
    from collections import Counter
    conn = sqlite3.connect(DB_PATH)
    # 加权统计：按总市值
    rows = conn.execute("""
        SELECT cusip, SUM(value) FROM holdings
        WHERE put_call IS NULL OR put_call = ''
        GROUP BY cusip
    """).fetchall()
    conn.close()

    total_val = sum(v for _, v in rows)
    sector_val: Counter = Counter()
    other_val = 0
    for cusip, val in rows:
        sec = sec_cache.get(cusip, "Other")
        if sec == "Other":
            other_val += val
        sector_val[sec] += val

    print(f"\n  行业覆盖率（按市值）:")
    print(f"  {'行业':<25} {'持仓$B':>10}  {'占池子':>7}")
    print(f"  {'─'*48}")
    for sec, val in sector_val.most_common():
        if sec == "Other": continue
        print(f"  {sec:<25} {val/1e6:>10.1f}B  {val/total_val*100:>6.1f}%")
    print(f"  {'Other (未识别)':<25} {other_val/1e6:>10.1f}B  {other_val/total_val*100:>6.1f}%")


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'═'*62}")
    print(f"  build_sector_cache  CUSIP→Ticker→Sector 批量构建")
    print(f"{'═'*62}")

    # Step 1
    print(f"\n  Step 1: 读取 holdings.db")
    all_cusips = get_all_cusips()
    print(f"  唯一 CUSIP: {len(all_cusips)}")

    # Step 2: OpenFIGI
    ct_cache = build_cusip_ticker(all_cusips)

    # Step 3: yfinance sector
    sec_cache = build_sector_cache(ct_cache)

    # 汇总
    print_stats(sec_cache, all_cusips)

    print(f"\n{'═'*62}")
    print(f"  完成！sector_cache.json 已更新")
    print(f"  重新运行 module_a.py 查看完整行业分析")
    print(f"{'═'*62}\n")


if __name__ == "__main__":
    main()
