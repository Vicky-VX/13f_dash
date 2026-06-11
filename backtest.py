"""
backtest.py  ─  13F 持仓回测
以季末价为买入基准，计算 +6M / +1Y 的实际股价表现与 SPY 对比。
反映基金真实选股能力（而非申报延迟影响）。

用法:
  python backtest.py --fund "TCI Fund Management"   # 单基金
  python backtest.py --all                           # 全部基金评分排行
"""

import sys, re, json, time, sqlite3, argparse
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

# ── 配置 ──────────────────────────────────────────────────────────────────────

FUND_POOL_FILE = "fund_pool.json"
BT_DB          = "backtest.db"
CT_CACHE_FILE  = Path("cusip_ticker_cache.json")

HEADERS   = {"User-Agent": "13F Research Tool research@example.com"}
SLEEP     = 0.25
N_YEARS   = 4          # 回测年数
SPY       = "SPY"      # 基准
BENCHMARK = "^GSPC"    # 或用 SPY

# 申报日相对季末的估算延迟（用于在缺失 filing_date 时推算）
FILING_LAG = 45        # 天

QUARTER_MAP = {"03":"Q1","06":"Q2","09":"Q3","12":"Q4"}

# 分时段回测区间
PHASE_1 = ("2022Q1", "2023Q4")   # 熊市+震荡期
PHASE_2 = ("2024Q1", "2025Q3")   # 牛市期


# ── 工具 ──────────────────────────────────────────────────────────────────────

def fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None
    except Exception:
        return None


def period_to_quarter(period: str) -> str:
    try:
        yr, mo, _ = period.split("-")
        return f"{yr}{QUARTER_MAP.get(mo,'??')}"
    except Exception:
        return period


def quarter_to_period_end(q: str) -> str:
    """'2023Q1' → '2023-03-31'"""
    yr = q[:4]
    qn = q[4:]
    ends = {"Q1": f"{yr}-03-31", "Q2": f"{yr}-06-30",
            "Q3": f"{yr}-09-30", "Q4": f"{yr}-12-31"}
    return ends.get(qn, "")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    yr = d.year + m // 12
    mo = m % 12 + 1
    day = min(d.day, [31,28+int((yr%4==0 and yr%100!=0) or yr%400==0),
                       31,30,31,30,31,31,30,31,30,31][mo-1])
    return date(yr, mo, day)


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_json(p: Path, d: dict):
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 价格查询（SQLite 永久缓存）────────────────────────────────────────────────

_price_conn: sqlite3.Connection | None = None   # 由 main() 设置
_cache_hits    = 0
_cache_misses  = 0
_price_missing = 0   # 拉取后仍无数据（退市/无效代码）的次数

# ticker 快速过滤：含这些字符的直接跳过
_INVALID_CHARS = set("/* $")


def get_price(ticker: str, target_date: str) -> float | None:
    """取 target_date 当天或最近交易日收盘价；先查 SQLite 缓存，未命中才拉 yfinance。"""
    global _cache_hits, _cache_misses, _price_missing
    if not ticker or len(ticker) > 12 or any(c in ticker for c in _INVALID_CHARS):
        return None

    # ① 查缓存（NULL 也算命中，避免重复请求已知缺失的数据）
    if _price_conn:
        row = _price_conn.execute(
            "SELECT close FROM price_cache WHERE ticker=? AND date=?",
            (ticker, target_date)
        ).fetchone()
        if row is not None:
            _cache_hits += 1
            return row[0]   # 可能是 None（已知无数据）

    _cache_misses += 1

    # ② 拉 yfinance —— 完全静默：屏蔽所有 stdout/stderr 输出和 warnings
    price = None
    try:
        import yfinance as yf, warnings, io, contextlib
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        if d <= date.today():
            start = (d - timedelta(days=7)).isoformat()
            end   = (d + timedelta(days=4)).isoformat()
            buf   = io.StringIO()
            with warnings.catch_warnings(), contextlib.redirect_stderr(buf):
                warnings.simplefilter("ignore")
                hist = yf.Ticker(ticker).history(start=start, end=end)
            if not hist.empty:
                hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index
                before = hist[hist.index.date <= d]
                if before.empty:
                    before = hist
                price = float(before["Close"].iloc[-1])
        time.sleep(0.15)
    except Exception:
        pass

    if price is None:
        _price_missing += 1

    # ③ 写入缓存（无论是否 None，都记录避免重复请求）
    if _price_conn:
        _price_conn.execute(
            "INSERT OR IGNORE INTO price_cache(ticker, date, close) VALUES(?,?,?)",
            (ticker, target_date, price)
        )
        _price_conn.commit()

    return price


def print_cache_stats():
    total = _cache_hits + _cache_misses
    if total == 0:
        return
    hit_pct = _cache_hits / total * 100
    print(f"\n  价格查询：共 {total} 次，缓存命中 {hit_pct:.0f}%"
          f"，新请求 {_cache_misses} 次"
          + (f"，{_price_missing} 条数据缺失（退市/无效代码）" if _price_missing else ""))


# ── EDGAR 数据拉取（重用 fetch_all.py 逻辑）────────────────────────────────────

def strip_ns(xml_str: str) -> str:
    xml_str = re.sub(r'\s+xmlns(?::[a-zA-Z0-9_-]+)?="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+=(?:"[^"]*"|\'[^\']*\')', '', xml_str)
    xml_str = re.sub(r'<([a-zA-Z0-9_-]+):',  '<',  xml_str)
    xml_str = re.sub(r'</([a-zA-Z0-9_-]+):', '</', xml_str)
    return xml_str


def get_accessions(cik: str, n: int = 20) -> list[dict]:
    """从 EDGAR browse 页面获取最近 n 条 13F-HR accession。"""
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
           f"?action=getcompany&CIK={cik}&type=13F-HR"
           f"&dateb=&owner=include&count={n}&search_text=")
    html = fetch(url)
    time.sleep(SLEEP)
    if not html:
        return []

    accs = re.findall(r'(\d{10}-\d{2}-\d{6})-index', html)
    accs = list(dict.fromkeys(accs))
    result = []
    for acc in accs:
        result.append({"accession": acc})
    return result


def get_filing_meta(cik: str, acc: str) -> tuple[str, str]:
    acc_clean = acc.replace("-", "")
    for suffix in ("-index.htm", "-index.html"):
        html = fetch(f"https://www.sec.gov/Archives/edgar/data"
                     f"/{cik}/{acc_clean}/{acc}{suffix}")
        time.sleep(SLEEP)
        if html:
            period = re.search(r'Period of Report[^\d]*(\d{4}-\d{2}-\d{2})', html)
            filed  = re.search(r'Filing Date[^\d]*(\d{4}-\d{2}-\d{2})', html)
            return (period.group(1) if period else "",
                    filed.group(1)  if filed  else "")
    return "", ""


def get_document_url(cik: str, acc: str) -> tuple[str, str]:
    acc_clean = acc.replace("-", "")
    base      = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}"
    index_html = None
    for suffix in ("-index.htm", "-index.html"):
        index_html = fetch(f"{base}/{acc}{suffix}")
        time.sleep(SLEEP)
        if index_html:
            break
    if not index_html:
        return "", ""

    hrefs = re.findall(r'href="(/Archives/edgar/data/[^"]+)"', index_html)
    hrefs = [h for h in hrefs
             if "xslForm13F" not in h and "primary_doc" not in h.lower()]

    for h in hrefs:
        if h.lower().endswith(".xml"):
            return "https://www.sec.gov" + h, "xml"
    for h in hrefs:
        if h.lower().endswith(".txt"):
            return "https://www.sec.gov" + h, "txt"
    return "", ""


def parse_holdings_from_content(content: str, fmt: str) -> list[dict]:
    if fmt == "xml":
        cleaned = strip_ns(content)
    else:
        cleaned = strip_ns(content)

    m = re.search(r'(<informationTable[^>]*>.*?</informationTable>)',
                  cleaned, re.DOTALL | re.IGNORECASE)
    if not m:
        return []

    try:
        root = ET.fromstring(m.group(1))
    except ET.ParseError:
        return []

    def g(node, tag):
        el = node.find(tag)
        return el.text.strip() if el is not None and el.text else ""

    holdings, total = [], 0
    for info in root.iter("infoTable"):
        val    = int(g(info, "value") or 0)
        sh_el  = info.find("shrsOrPrnAmt")
        shares = int(sh_el.find("sshPrnamt").text) if (
            sh_el is not None and sh_el.find("sshPrnamt") is not None) else 0
        total += val
        holdings.append({
            "issuer":   g(info, "nameOfIssuer"),
            "cusip":    g(info, "cusip"),
            "value":    val,
            "shares":   shares,
            "put_call": g(info, "putCall"),
        })

    if not holdings:
        return []

    max_val = max(h["value"] for h in holdings)
    if max_val >= 500_000_000:
        for h in holdings:
            h["value"] = h["value"] // 1000
        total = total // 1000

    for h in holdings:
        h["pct"] = round(h["value"] / total * 100, 4) if total else 0.0
    holdings.sort(key=lambda x: x["value"], reverse=True)
    return holdings


# ── 回测数据库 ─────────────────────────────────────────────────────────────────

def init_bt_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bt_filings (
        id           INTEGER PRIMARY KEY,
        cik          TEXT,
        fund_name    TEXT,
        quarter      TEXT,
        period_end   TEXT,
        filing_date  TEXT,
        accession    TEXT UNIQUE
    );

    CREATE TABLE IF NOT EXISTS bt_holdings (
        id              INTEGER PRIMARY KEY,
        filing_id       INTEGER REFERENCES bt_filings(id),
        cik             TEXT,
        fund_name       TEXT,
        quarter         TEXT,
        period_end      TEXT,
        cusip           TEXT,
        issuer          TEXT,
        ticker          TEXT,
        shares          INTEGER,
        value_k         INTEGER,
        pct             REAL,
        action          TEXT,
        -- 季末价格（反映选股能力）
        price_end       REAL,
        price_6m        REAL,
        price_1y        REAL,
        spy_end         REAL,
        spy_6m          REAL,
        spy_1y          REAL,
        -- 披露日价格（反映跟随可行性）
        price_filing    REAL,
        price_6m_f      REAL,
        price_1y_f      REAL,
        spy_filing      REAL,
        spy_6m_f        REAL,
        spy_1y_f        REAL,
        UNIQUE(cik, quarter, cusip)
    );

    CREATE INDEX IF NOT EXISTS idx_bt_cik_q ON bt_holdings(cik, quarter);
    CREATE INDEX IF NOT EXISTS idx_bt_cusip  ON bt_holdings(cusip);

    CREATE TABLE IF NOT EXISTS price_cache (
        ticker  TEXT NOT NULL,
        date    TEXT NOT NULL,
        close   REAL,
        PRIMARY KEY (ticker, date)
    );
    """)
    # 迁移旧表：补充披露日列（若已存在则忽略）
    for col in ("price_filing","price_6m_f","price_1y_f",
                "spy_filing","spy_6m_f","spy_1y_f"):
        try:
            conn.execute(f"ALTER TABLE bt_holdings ADD COLUMN {col} REAL")
        except Exception:
            pass
    conn.commit()


def fetch_fund_history(fund: dict, conn, target_quarters: int = N_YEARS * 4):
    """拉取一只基金最近 target_quarters 季的完整 13F 历史，存入 bt_holdings。"""
    name    = fund["name"]
    cik     = fund["cik"].lstrip("0")
    cik_pad = fund["cik"]

    print(f"\n  [{name}]  CIK:{fund['cik']}")
    accs = get_accessions(cik, n=target_quarters + 4)

    stored = 0
    for acc_info in accs[:target_quarters]:
        acc = acc_info["accession"]

        if conn.execute("SELECT 1 FROM bt_filings WHERE accession=?",
                        (acc,)).fetchone():
            q_row = conn.execute(
                "SELECT quarter FROM bt_filings WHERE accession=?", (acc,)).fetchone()
            print(f"    ✓ {q_row[0]} 已缓存", end="\r")
            stored += 1
            continue

        period, filed = get_filing_meta(cik, acc)
        if not period:
            continue
        quarter = period_to_quarter(period)

        # 只取 N_YEARS 年内的数据
        yr = int(quarter[:4])
        if yr < date.today().year - N_YEARS:
            continue

        url, fmt = get_document_url(cik, acc)
        if not url:
            continue

        content = fetch(url)
        time.sleep(SLEEP)
        if not content:
            continue

        holdings = parse_holdings_from_content(content, fmt)
        if not holdings:
            continue

        cur = conn.execute("""
            INSERT OR IGNORE INTO bt_filings
            (cik, fund_name, quarter, period_end, filing_date, accession)
            VALUES (?,?,?,?,?,?)
        """, (cik_pad, name, quarter, period, filed, acc))
        conn.commit()

        fid = cur.lastrowid or conn.execute(
            "SELECT id FROM bt_filings WHERE accession=?", (acc,)).fetchone()[0]

        ct_cache = load_json(CT_CACHE_FILE)
        MANUAL = {"G3643J108":"FLUT","M7S64H106":"MNDY","N07059210":"ASML",
                  "G0750C108":"ARGX","G8T5AN108":"SPOT","G20567101":"CRH"}

        rows = []
        for h in holdings:
            if h.get("put_call"):
                continue
            cusip = h["cusip"]
            tk_raw = MANUAL.get(cusip) or ct_cache.get(cusip, "")
            ticker = tk_raw if (tk_raw and len(tk_raw) <= 10
                                and " " not in tk_raw and "/" not in tk_raw) else ""
            # action 先统一写 HOLD，拉完全部季后由 recompute_actions() 修正
            rows.append((fid, cik_pad, name, quarter, period,
                         cusip, h["issuer"], ticker,
                         h["shares"], h["value"], h["pct"], "HOLD"))

        conn.executemany("""
            INSERT OR IGNORE INTO bt_holdings
            (filing_id, cik, fund_name, quarter, period_end,
             cusip, issuer, ticker, shares, value_k, pct, action)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()
        stored += 1
        print(f"    ✓ {quarter} ({filed})  {len(rows)} 只持仓")

    print(f"    → 共 {stored} 季数据")
    return stored


# ── Action 重计算（拉完所有季后统一跨季比对）────────────────────────────────────

def recompute_actions(conn, cik: str):
    """
    正确逻辑：所有季度拉完后，按时间顺序逐季比对，更新 action 字段。
    只标记 NEW / ADD（主动增仓信号），其余为 HOLD。
    """
    quarters = [r[0] for r in conn.execute("""
        SELECT DISTINCT quarter FROM bt_holdings
        WHERE cik=? ORDER BY quarter
    """, (cik,)).fetchall()]

    if len(quarters) < 2:
        return 0

    updated = 0
    for i, curr_q in enumerate(quarters):
        if i == 0:
            # 第一季无上季可比 → 全部标 HOLD（不纳入回测，避免偏差）
            conn.execute("UPDATE bt_holdings SET action='HOLD' WHERE cik=? AND quarter=?",
                         (cik, curr_q))
            continue

        prev_q = quarters[i - 1]

        curr_rows = conn.execute("""
            SELECT id, cusip, pct, shares
            FROM bt_holdings WHERE cik=? AND quarter=?
        """, (cik, curr_q)).fetchall()

        prev_map = {r[1]: {"pct": r[2], "shares": r[3]}
                    for r in conn.execute("""
            SELECT id, cusip, pct, shares
            FROM bt_holdings WHERE cik=? AND quarter=?
        """, (cik, prev_q)).fetchall()}

        for row_id, cusip, pct, shares in curr_rows:
            prev = prev_map.get(cusip)

            if prev is None:
                action = "NEW"                        # 上季没有 → 新建仓
            else:
                pct_diff = pct - prev["pct"]
                sh_diff  = shares - prev["shares"]
                sh_pct   = sh_diff / prev["shares"] if prev["shares"] else 0
                if pct_diff >= 3.0 or sh_pct >= 0.10:
                    action = "ADD"                    # 仓位 +3pp 或股数 +10%
                else:
                    action = "HOLD"                   # 维持不变，不纳入回测

            conn.execute("UPDATE bt_holdings SET action=? WHERE id=?",
                         (action, row_id))
            updated += 1

    conn.commit()
    return updated


# ── 价格填充 ──────────────────────────────────────────────────────────────────

def fill_prices(conn, cik: str | None = None, verbose: bool = True) -> bool:
    """
    同时填充：
    1. 季末价格（price_end / price_6m / price_1y）    → 反映选股能力
    2. 披露日价格（price_filing / price_6m_f / price_1y_f）→ 反映跟随可行性
    只处理 NEW/ADD 信号，且对应期间已到期的记录。
    返回 True 表示有新数据被填入。
    """
    today     = date.today()
    cutoff_6m = (today - timedelta(days=185)).isoformat()

    where  = "AND h.cik=?" if cik else ""
    params = (cutoff_6m,) + ((cik,) if cik else ())

    rows = conn.execute(f"""
        SELECT h.id, h.ticker, h.period_end, f.filing_date
        FROM bt_holdings h
        JOIN bt_filings f ON h.filing_id = f.id
        WHERE h.action IN ('NEW','ADD')
          AND h.ticker != ''
          AND h.period_end <= ?
          {where}
        ORDER BY h.period_end
    """, params).fetchall()

    # 过滤：price_end 和 price_filing 都已填充的跳过
    todo = []
    for row_id, ticker, period_end, filing_date in rows:
        existing = conn.execute(
            "SELECT price_end, price_filing FROM bt_holdings WHERE id=?",
            (row_id,)).fetchone()
        if existing and existing[0] is not None and existing[1] is not None:
            continue
        todo.append((row_id, ticker, period_end, filing_date))

    if not todo:
        if verbose: print("  价格数据已是最新")
        return False

    print(f"\n  填充价格数据: {len(todo)} 条记录...")
    done = 0

    for row_id, ticker, period_end, filing_date in todo:
        if not ticker or not period_end:
            continue
        try:
            d_end = datetime.strptime(period_end, "%Y-%m-%d").date()
        except ValueError:
            continue

        # 推算披露日（若缺失则用季末+45天估算）
        if filing_date:
            try:
                d_fil = datetime.strptime(filing_date, "%Y-%m-%d").date()
            except ValueError:
                d_fil = d_end + timedelta(days=FILING_LAG)
        else:
            d_fil = d_end + timedelta(days=FILING_LAG)

        d_end_6m = add_months(d_end, 6)
        d_end_1y = add_months(d_end, 12)
        d_fil_6m = add_months(d_fil, 6)
        d_fil_1y = add_months(d_fil, 12)

        def gp(tk, d):
            return get_price(tk, d.isoformat()) if d <= today else None

        p_end    = gp(ticker, d_end)
        p_6m     = gp(ticker, d_end_6m)
        p_1y     = gp(ticker, d_end_1y)
        spy_end  = gp(SPY, d_end)
        spy_6m   = gp(SPY, d_end_6m)
        spy_1y   = gp(SPY, d_end_1y)

        p_fil    = gp(ticker, d_fil)
        p_6m_f   = gp(ticker, d_fil_6m)
        p_1y_f   = gp(ticker, d_fil_1y)
        spy_fil  = gp(SPY, d_fil)
        spy_6m_f = gp(SPY, d_fil_6m)
        spy_1y_f = gp(SPY, d_fil_1y)

        conn.execute("""
            UPDATE bt_holdings
            SET price_end=?,  price_6m=?,   price_1y=?,
                spy_end=?,    spy_6m=?,      spy_1y=?,
                price_filing=?, price_6m_f=?, price_1y_f=?,
                spy_filing=?,   spy_6m_f=?,   spy_1y_f=?
            WHERE id=?
        """, (p_end, p_6m, p_1y, spy_end, spy_6m, spy_1y,
              p_fil, p_6m_f, p_1y_f, spy_fil, spy_6m_f, spy_1y_f,
              row_id))

        done += 1
        if done % 20 == 0:
            conn.commit()
            print(f"    {done}/{len(todo)}", end="\r")

    conn.commit()
    print(f"  价格填充完成: {done} 条")
    return True


# ── 回测指标计算 ──────────────────────────────────────────────────────────────

def safe_ret(p_now, p_base) -> float | None:
    if p_now and p_base and p_base > 0:
        return (p_now / p_base - 1) * 100
    return None


def _alpha(p_now, p_base, spy_now, spy_base) -> float | None:
    r  = safe_ret(p_now, p_base)
    rs = safe_ret(spy_now, spy_base)
    return (r - rs) if r is not None and rs is not None else None


def compute_fund_metrics(conn, cik: str, fund_name: str) -> dict:
    """计算一只基金的全部回测指标。"""
    rows = conn.execute("""
        SELECT h.quarter, h.period_end, h.action, h.pct,
               h.price_end,  h.price_6m,   h.price_1y,
               h.spy_end,    h.spy_6m,      h.spy_1y,
               h.price_filing, h.price_6m_f, h.price_1y_f,
               h.spy_filing,   h.spy_6m_f,   h.spy_1y_f,
               h.issuer, h.ticker
        FROM bt_holdings h
        WHERE h.cik=? AND h.action IN ('NEW','ADD')
          AND h.price_end IS NOT NULL
        ORDER BY h.period_end, h.pct DESC
    """, (cik,)).fetchall()

    if not rows:
        return {}

    by_q: dict[str, list] = defaultdict(list)
    for r in rows:
        by_q[r[0]].append(r)

    def agg_quarter(holdings, use_filing: bool):
        """按季度聚合收益，use_filing=True 用披露日价格。"""
        w6m = w1y = ws6m = ws1y = 0.0
        tw6m = tw1y = 0.0
        h6m = h1y = t6m = t1y = 0

        for r in holdings:
            (q, period_end, action, pct,
             p_end, p_6m, p_1y, spy_end, spy_6m, spy_1y,
             p_fil, p_6m_f, p_1y_f, spy_fil, spy_6m_f, spy_1y_f,
             issuer, ticker) = r

            if use_filing:
                pb, p6, p1, sb, s6, s1 = p_fil, p_6m_f, p_1y_f, spy_fil, spy_6m_f, spy_1y_f
            else:
                pb, p6, p1, sb, s6, s1 = p_end, p_6m, p_1y, spy_end, spy_6m, spy_1y

            a6 = _alpha(p6, pb, s6, sb)
            a1 = _alpha(p1, pb, s1, sb)
            r6 = safe_ret(p6, pb)
            rs6 = safe_ret(s6, sb)

            if a6 is not None:
                w6m += r6 * pct; ws6m += rs6 * pct; tw6m += pct
                t6m += 1; h6m += 1 if a6 > 0 else 0
            if a1 is not None:
                r1 = safe_ret(p1, pb); rs1 = safe_ret(s1, sb)
                w1y += r1 * pct; ws1y += rs1 * pct; tw1y += pct
                t1y += 1; h1y += 1 if a1 > 0 else 0

        a6m = ((w6m/tw6m) - (ws6m/tw6m)) if tw6m else None
        a1y = ((w1y/tw1y) - (ws1y/tw1y)) if tw1y else None
        return {
            "alpha_6m": a6m, "alpha_1y": a1y,
            "hit_6m":   h6m/t6m*100 if t6m else None,
            "hit_1y":   h1y/t1y*100 if t1y else None,
            "wr_6m":    w6m/tw6m if tw6m else None,
            "spy_6m":   ws6m/tw6m if tw6m else None,
        }

    # ── 逐季指标 ─────────────────────────────────────────────────────────────
    q_stats = []
    end_a6, end_a1, end_h6, end_h1 = [], [], [], []
    fil_a6, fil_a1 = [], []

    for q in sorted(by_q.keys()):
        h = by_q[q]
        eq = agg_quarter(h, use_filing=False)
        fq = agg_quarter(h, use_filing=True)
        q_stats.append({
            "quarter":  q,
            "n_signals": len(h),
            "n_new":    sum(1 for r in h if r[2]=="NEW"),
            "n_add":    sum(1 for r in h if r[2]=="ADD"),
            **{f"end_{k}": v for k, v in eq.items()},
            **{f"fil_{k}": v for k, v in fq.items()},
        })
        if eq["alpha_6m"] is not None: end_a6.append(eq["alpha_6m"])
        if eq["alpha_1y"] is not None: end_a1.append(eq["alpha_1y"])
        if eq["hit_6m"]   is not None: end_h6.append(eq["hit_6m"])
        if eq["hit_1y"]   is not None: end_h1.append(eq["hit_1y"])
        if fq["alpha_6m"] is not None: fil_a6.append(fq["alpha_6m"])
        if fq["alpha_1y"] is not None: fil_a1.append(fq["alpha_1y"])

    # ── 分阶段指标 ───────────────────────────────────────────────────────────
    def phase_metrics(q_lo: str, q_hi: str) -> dict:
        """计算指定季度区间的 1Y alpha 和一致性。"""
        p_a1, p_h1 = [], []
        for q in sorted(by_q.keys()):
            if not (q_lo <= q <= q_hi):
                continue
            eq = agg_quarter(by_q[q], use_filing=False)
            if eq["alpha_1y"] is not None:
                p_a1.append(eq["alpha_1y"])
            if eq["hit_1y"] is not None:
                p_h1.append(eq["hit_1y"])
        avg_a1 = (sum(p_a1) / len(p_a1)) if p_a1 else None
        consist = (sum(1 for a in p_a1 if a > 0) / len(p_a1)) if p_a1 else None
        return {"alpha_1y": avg_a1, "consistency": consist, "n_quarters": len(p_a1)}

    ph1 = phase_metrics(*PHASE_1)
    ph2 = phase_metrics(*PHASE_2)

    def style_judgment(ph1: dict, ph2: dict) -> str:
        a1 = ph1.get("alpha_1y")
        a2 = ph2.get("alpha_1y")
        if a1 is None and a2 is None:
            return "需观察"
        pos1 = (a1 is not None and a1 > 0)
        pos2 = (a2 is not None and a2 > 0)
        if pos1 and pos2:
            return "全天候"
        if pos2 and not pos1:
            return "牛市成长型"
        if pos1 and not pos2:
            return "熊市价值型"
        return "需观察"

    style = style_judgment(ph1, ph2)

    # ── NEW vs ADD 专项 alpha（1Y，季末价）────────────────────────────────────
    def group_alpha_1y(action_filter):
        vals = []
        for r in rows:
            if r[2] != action_filter: continue
            a = _alpha(r[6], r[4], r[9], r[7])   # price_1y, price_end, spy_1y, spy_end
            if a is not None: vals.append(a)
        return (sum(vals)/len(vals) if vals else None, len(vals))

    new_a1y, n_new = group_alpha_1y("NEW")
    add_a1y, n_add = group_alpha_1y("ADD")

    avg = lambda lst: sum(lst)/len(lst) if lst else None

    end_avg_a6 = avg(end_a6)
    end_avg_a1 = avg(end_a1)
    fil_avg_a1 = avg(fil_a1)
    lag_cost   = (fil_avg_a1 - end_avg_a1) if (fil_avg_a1 is not None and end_avg_a1 is not None) else None

    # 一致性：季度维度有正 1Y alpha 的比例
    consistency = (sum(1 for a in end_a1 if a > 0) / len(end_a1)) if end_a1 else None

    return {
        "cik":          cik,
        "fund_name":    fund_name,
        "quarters":     q_stats,
        "n_quarters":   len(q_stats),
        "n_signals":    len(rows),
        # 季末价维度
        "end_alpha_6m": end_avg_a6,
        "end_alpha_1y": end_avg_a1,
        "end_hit_6m":   avg(end_h6),
        "end_hit_1y":   avg(end_h1),
        # 披露日维度
        "fil_alpha_6m": avg(fil_a6),
        "fil_alpha_1y": fil_avg_a1,
        # 分组 alpha
        "new_alpha_1y": new_a1y,  "n_new": n_new,
        "add_alpha_1y": add_a1y,  "n_add": n_add,
        "best_signal":  ("NEW" if (new_a1y or 0) > (add_a1y or 0) else "ADD"),
        # 综合
        "consistency":  consistency,
        "lag_cost":     lag_cost,
        # 分阶段
        "ph1_alpha_1y":    ph1["alpha_1y"],
        "ph1_consistency": ph1["consistency"],
        "ph1_n_quarters":  ph1["n_quarters"],
        "ph2_alpha_1y":    ph2["alpha_1y"],
        "ph2_consistency": ph2["consistency"],
        "ph2_n_quarters":  ph2["n_quarters"],
        "style":           style,
    }


# ── 报告输出 ──────────────────────────────────────────────────────────────────

def fmt(v, suffix="", decimals=1, na="  N/A"):
    if v is None:
        return na
    s = f"{v:+.{decimals}f}{suffix}" if "+" in f"{v:+.0f}" or v < 0 else f"{v:.{decimals}f}{suffix}"
    return s


def save_results_to_db(conn, all_metrics: list[dict]):
    """把回测指标存入 bt_fund_results + bt_quarter_results，供 dashboard 读取。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bt_fund_results (
        cik TEXT PRIMARY KEY, fund_name TEXT,
        n_signals INTEGER, n_quarters INTEGER,
        end_alpha_6m REAL, end_alpha_1y REAL,
        end_hit_6m REAL,   end_hit_1y REAL,
        fil_alpha_6m REAL, fil_alpha_1y REAL,
        new_alpha_1y REAL, n_new INTEGER,
        add_alpha_1y REAL, n_add INTEGER,
        best_signal TEXT, consistency REAL,
        lag_cost REAL, recommend TEXT,
        ph1_alpha_1y REAL, ph1_consistency REAL,
        ph2_alpha_1y REAL, ph2_consistency REAL,
        style TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS bt_quarter_results (
        id INTEGER PRIMARY KEY,
        cik TEXT, fund_name TEXT, quarter TEXT,
        n_signals INTEGER, n_new INTEGER, n_add INTEGER,
        end_alpha_6m REAL, end_alpha_1y REAL,
        end_hit_6m REAL,   end_hit_1y REAL,
        fil_alpha_1y REAL,
        UNIQUE(cik, quarter)
    );
    """)
    # 迁移旧表补充分阶段列
    for col, typ in [("ph1_alpha_1y","REAL"),("ph1_consistency","REAL"),
                     ("ph2_alpha_1y","REAL"),("ph2_consistency","REAL"),
                     ("style","TEXT")]:
        try:
            conn.execute(f"ALTER TABLE bt_fund_results ADD COLUMN {col} {typ}")
        except Exception:
            pass

    # 一次性修复：清除因 INSERT 列序错误写入的脏数据
    # 判断标准：style 不是合法风格字符串（说明写进了时间戳或其他错误值）
    conn.execute("""
        UPDATE bt_fund_results
        SET ph1_alpha_1y=NULL, ph1_consistency=NULL,
            ph2_alpha_1y=NULL, ph2_consistency=NULL, style=NULL
        WHERE style IS NOT NULL
          AND style NOT IN ('全天候','牛市成长型','熊市价值型','需观察')
    """)
    conn.commit()

    from datetime import datetime as _dt
    now = _dt.now().isoformat()

    for m in all_metrics:
        # 显式列名，防止 ALTER TABLE 追加列导致位置错乱
        conn.execute("""
            INSERT OR REPLACE INTO bt_fund_results
            (cik, fund_name, n_signals, n_quarters,
             end_alpha_6m, end_alpha_1y, end_hit_6m, end_hit_1y,
             fil_alpha_6m, fil_alpha_1y, new_alpha_1y, n_new,
             add_alpha_1y, n_add, best_signal, consistency,
             lag_cost, recommend,
             ph1_alpha_1y, ph1_consistency,
             ph2_alpha_1y, ph2_consistency,
             style, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (m["cik"], m["fund_name"],
              m["n_signals"], m["n_quarters"],
              m.get("end_alpha_6m"), m.get("end_alpha_1y"),
              m.get("end_hit_6m"),   m.get("end_hit_1y"),
              m.get("fil_alpha_6m"), m.get("fil_alpha_1y"),
              m.get("new_alpha_1y"), m.get("n_new"),
              m.get("add_alpha_1y"), m.get("n_add"),
              m.get("best_signal"),  m.get("consistency"),
              m.get("lag_cost"),     recommend(m),
              m.get("ph1_alpha_1y"), m.get("ph1_consistency"),
              m.get("ph2_alpha_1y"), m.get("ph2_consistency"),
              m.get("style"),        now))

        for q in m.get("quarters", []):
            conn.execute("""
                INSERT OR REPLACE INTO bt_quarter_results
                (cik, fund_name, quarter, n_signals, n_new, n_add,
                 end_alpha_6m, end_alpha_1y, end_hit_6m, end_hit_1y, fil_alpha_1y)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (m["cik"], m["fund_name"], q["quarter"],
                  q["n_signals"], q["n_new"], q["n_add"],
                  q.get("end_alpha_6m"), q.get("end_alpha_1y"),
                  q.get("end_hit_6m"),   q.get("end_hit_1y"),
                  q.get("fil_alpha_1y")))

    conn.commit()
    print(f"  回测结果已存入 backtest.db ({len(all_metrics)} 家基金)")


def recommend(m: dict) -> str:
    a1y = m.get("end_alpha_1y") or 0
    con = (m.get("consistency") or 0)
    lag = m.get("lag_cost") or 0
    if a1y > 10 and con > 0.6 and lag > -5:
        return "★★★★ 强烈推荐"
    if a1y > 10 and con > 0.6 and lag <= -5:
        return "★★★  眼光准需快速执行"
    if a1y > 5 and con > 0.5:
        return "★★   选择性跟随"
    return "★    不建议"


def print_fund_report(m: dict):
    if not m:
        print("  无回测数据")
        return
    qs = [q["quarter"] for q in m["quarters"]]
    yr_range = f"{qs[0]} ~ {qs[-1]}" if qs else "—"
    print(f"\n{'═'*80}")
    print(f"  {m['fund_name']}  ─  回测报告")
    print(f"  覆盖: {m['n_quarters']} 季  {yr_range}  |  总信号: {m['n_signals']} 个")
    print(f"{'═'*80}")
    print(f"\n  {'季度':<8} {'总信号':>5} {'新建仓':>5} {'加仓':>4}  "
          f"{'季末6M超额':>10} {'季末1Y超额':>10} {'6M胜率':>7} {'1Y胜率':>7}  {'披露日1Y超额':>11}")
    print(f"  {'─'*76}")
    for q in m["quarters"]:
        ea6  = fmt(q.get("end_alpha_6m"), "pp", na="       —")
        ea1  = fmt(q.get("end_alpha_1y"), "pp", na="       —")
        h6   = fmt(q.get("end_hit_6m"),  "%", decimals=0, na="    —")
        h1   = fmt(q.get("end_hit_1y"),  "%", decimals=0, na="    —")
        fa1  = fmt(q.get("fil_alpha_1y"), "pp", na="         —")
        icon = "▲" if (q.get("end_alpha_6m") or 0) > 0 else "▼"
        print(f"  {q['quarter']:<8} {q['n_signals']:>5} {q['n_new']:>5} {q['n_add']:>4}  "
              f"{icon}{ea6:>9} {ea1:>10} {h6:>7} {h1:>7}  {fa1:>11}")
    print(f"\n  {'─'*70}")
    rec = recommend(m)
    print(f"  季末1Yα {fmt(m['end_alpha_1y'],'pp'):>8}   披露日1Yα {fmt(m['fil_alpha_1y'],'pp'):>8}"
          f"   滞后成本 {fmt(m['lag_cost'],'pp'):>8}")
    print(f"  6M胜率  {fmt(m['end_hit_6m'],'%',0):>6}   1Y胜率   {fmt(m['end_hit_1y'],'%',0):>6}"
          f"   一致性   {fmt((m.get('consistency') or 0)*100,'%',0):>6}")
    print(f"  NEW1Yα  {fmt(m['new_alpha_1y'],'pp'):>8} (n={m['n_new']})"
          f"   ADD1Yα   {fmt(m['add_alpha_1y'],'pp'):>8} (n={m['n_add']})"
          f"   最优信号 → 跟{m['best_signal']}")
    # 分阶段摘要
    def _f(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None
    ph1c = _f(m.get("ph1_consistency"))
    ph2c = _f(m.get("ph2_consistency"))
    c1 = f"{ph1c*100:.0f}%" if ph1c is not None else "N/A"
    c2 = f"{ph2c*100:.0f}%" if ph2c is not None else "N/A"
    style_icon = {"全天候":"🌐","牛市成长型":"🚀","熊市价值型":"🛡","需观察":"❓"}.get(m.get("style",""), "")
    print(f"\n  ── 分时段回测 ──────────────────────────────────────────────────────")
    print(f"  阶段一({PHASE_1[0]}~{PHASE_1[1]})  1Yα: {fmt(_f(m.get('ph1_alpha_1y')),'pp'):>8}   一致性: {c1}")
    print(f"  阶段二({PHASE_2[0]}~{PHASE_2[1]})  1Yα: {fmt(_f(m.get('ph2_alpha_1y')),'pp'):>8}   一致性: {c2}")
    print(f"  风格判断: {style_icon} {m.get('style','需观察')}")
    print(f"\n  推荐度: {rec}\n")


def print_ranking(all_metrics: list[dict]):
    valid = [m for m in all_metrics if m.get("end_alpha_1y") is not None]
    if not valid:
        print("  无足够数据")
        return
    valid.sort(key=lambda x: x["end_alpha_1y"] or 0, reverse=True)

    W = 112
    # ── 季末价完整表 ─────────────────────────────────────────────────────────
    print(f"\n{'═'*W}")
    print(f"  【季末价完整表】机构选股能力（季末为买入基准）")
    print(f"{'═'*W}")
    print(f"  {'基金':<28} {'信号':>4}  {'6M胜率':>6} {'6Mα':>8}  "
          f"{'1Y胜率':>6} {'1Yα':>8}  {'一致性':>6}  {'NEWα1Y':>8} {'ADDα1Y':>8}  最优")
    print(f"  {'─'*W}")
    for m in valid:
        con = (m.get('consistency') or 0) * 100
        print(f"  {m['fund_name'][:26]:<28} {m['n_signals']:>4}  "
              f"{fmt(m['end_hit_6m'],'%',0):>6} {fmt(m['end_alpha_6m'],'pp'):>8}  "
              f"{fmt(m['end_hit_1y'],'%',0):>6} {fmt(m['end_alpha_1y'],'pp'):>8}  "
              f"{con:>5.0f}%  "
              f"{fmt(m['new_alpha_1y'],'pp'):>8} {fmt(m['add_alpha_1y'],'pp'):>8}  "
              f"跟{m['best_signal']} ✓")

    # ── 披露日完整表 ─────────────────────────────────────────────────────────
    fil_valid = sorted(valid, key=lambda x: x.get("fil_alpha_1y") or 0, reverse=True)
    print(f"\n{'═'*W}")
    print(f"  【披露日完整表】跟随可行性（披露日为买入基准）")
    print(f"{'═'*W}")
    print(f"  {'基金':<28} {'信号':>4}  {'6Mα(披)':>9} {'1Yα(披)':>9}  "
          f"{'1Yα(末)':>9}  {'滞后成本':>9}  {'推荐度'}")
    print(f"  {'─'*W}")
    for m in fil_valid:
        rec = recommend(m)
        print(f"  {m['fund_name'][:26]:<28} {m['n_signals']:>4}  "
              f"{fmt(m['fil_alpha_6m'],'pp'):>9} {fmt(m['fil_alpha_1y'],'pp'):>9}  "
              f"{fmt(m['end_alpha_1y'],'pp'):>9}  "
              f"{fmt(m['lag_cost'],'pp'):>9}  {rec}")

    # ── 三段结论 ─────────────────────────────────────────────────────────────
    strong, selective, avoid = [], [], []
    for m in valid:
        r = recommend(m)
        if "强烈" in r:   strong.append(m)
        elif "★" in r and ("眼光" in r or "选择" in r): selective.append(m)
        else:             avoid.append(m)

    print(f"\n{'═'*W}")
    print(f"  【投资结论】")
    print(f"{'═'*W}")

    print(f"\n  ✅ 强烈推荐跟随  (1Yα>10pp，一致性>60%，滞后成本>-5pp)")
    for m in strong or [None]:
        if m:
            print(f"     • {m['fund_name']}  "
                  f"(跟{m['best_signal']}，1Yα={fmt(m['end_alpha_1y'],'pp')}，"
                  f"一致性={int((m.get('consistency') or 0)*100)}%，"
                  f"滞后={fmt(m['lag_cost'],'pp')})")
        else: print("     暂无")

    print(f"\n  ⚡ 选择性跟随  (alpha高但不稳定，或滞后成本高需快速执行)")
    for m in selective or [None]:
        if m:
            r = recommend(m)
            reason = ("眼光准但披露到执行需快" if "眼光" in r
                      else f"1Yα={fmt(m['end_alpha_1y'],'pp')}，一致性{int((m.get('consistency') or 0)*100)}%")
            print(f"     • {m['fund_name']}  ({reason})")
        else: print("     暂无")

    print(f"\n  ❌ 不建议跟随  (alpha低或负)")
    for m in avoid or [None]:
        if m:
            print(f"     • {m['fund_name']}  "
                  f"(1Yα={fmt(m['end_alpha_1y'],'pp')}，一致性{int((m.get('consistency') or 0)*100)}%)")
        else: print("     暂无")
    print()

    # ── 分时段对比表 ─────────────────────────────────────────────────────────
    print(f"\n{'═'*W}")
    print(f"  【分时段回测】  阶段一:{PHASE_1[0]}~{PHASE_1[1]}（熊市）  |  阶段二:{PHASE_2[0]}~{PHASE_2[1]}（牛市）")
    print(f"{'═'*W}")
    print(f"  {'基金':<28} {'阶段一1Yα':>10} {'阶段一一致性':>10} {'阶段二1Yα':>10} {'阶段二一致性':>10}  {'风格判断'}")
    print(f"  {'─'*W}")
    def _f(v):
        """从 DB 读回的值可能是 float/int/str/None，统一转 float 或 None。"""
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    phase_valid = [m for m in valid
                   if _f(m.get("ph1_alpha_1y")) is not None or _f(m.get("ph2_alpha_1y")) is not None]
    phase_valid.sort(key=lambda x: (_f(x.get("ph2_alpha_1y")) or 0) + (_f(x.get("ph1_alpha_1y")) or 0),
                     reverse=True)
    for m in phase_valid:
        ph1c = _f(m.get("ph1_consistency"))
        ph2c = _f(m.get("ph2_consistency"))
        c1 = f"{ph1c*100:.0f}%" if ph1c is not None else "  N/A"
        c2 = f"{ph2c*100:.0f}%" if ph2c is not None else "  N/A"
        style_icon = {"全天候":"🌐","牛市成长型":"🚀","熊市价值型":"🛡","需观察":"❓"}.get(m.get("style",""), "")
        print(f"  {m['fund_name'][:26]:<28} {fmt(_f(m.get('ph1_alpha_1y')),'pp'):>10} {c1:>10} "
              f"{fmt(_f(m.get('ph2_alpha_1y')),'pp'):>10} {c2:>10}  "
              f"{style_icon} {m.get('style','需观察')}")
    print()


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="13F 回测")
    parser.add_argument("--fund",   help="基金名称（部分匹配）")
    parser.add_argument("--all",    action="store_true", help="回测全部基金")
    parser.add_argument("--prices", action="store_true", help="只填充价格不拉新数据")
    parser.add_argument("--force",  action="store_true", help="强制重算所有基金指标（忽略增量缓存）")
    args = parser.parse_args()

    global _price_conn
    conn = sqlite3.connect(BT_DB)
    _price_conn = conn          # 让 get_price() 使用同一个连接
    init_bt_db(conn)

    with open(FUND_POOL_FILE, encoding="utf-8") as f:
        pool = json.load(f)["funds"]

    if args.fund:
        funds = [f for f in pool if args.fund.lower() in f["name"].lower()]
    elif args.all:
        funds = pool
    else:
        funds = [f for f in pool if "TCI" in f["name"]]

    if not funds:
        print("未找到匹配的基金"); return

    print(f"\n{'═'*72}")
    print(f"  13F 回测系统  ─  {'全部基金' if args.all else funds[0]['name']}")
    print(f"  回测窗口: 过去 {N_YEARS} 年  |  基准: {SPY}")
    print(f"{'═'*72}")

    total = len(funds)

    # Step 1: 拉取历史数据
    if not args.prices:
        print(f"\n  ── Step 1/3  拉取历史13F ──────────────────────────────────")
        for i, fund in enumerate(funds):
            print(f"  [{i+1}/{total}] {fund['name'][:34]}")
            fetch_fund_history(fund, conn, target_quarters=N_YEARS * 4 + 2)
            time.sleep(0.5)

    # Step 1b: 统一重计算 action
    print(f"\n  ── Step 2a/3  重计算持仓变化信号 ───────────────────────────────")
    for i, fund in enumerate(funds):
        recompute_actions(conn, fund["cik"])
        dist = conn.execute(
            "SELECT action, COUNT(*) FROM bt_holdings WHERE cik=? GROUP BY action",
            (fund["cik"],)).fetchall()
        dist_str = "  ".join(f"{a}:{c}" for a, c in sorted(dist))
        print(f"  [{i+1}/{total}] {fund['name'][:28]:<28}  {dist_str}")

    # Step 2: 填充价格（同时填季末价 + 披露日价格）
    print(f"\n  ── Step 2b/3  填充价格数据（yfinance）──────────────────────────")
    funds_with_new_data: set[str] = set()
    for i, fund in enumerate(funds):
        print(f"  [{i+1}/{total}] {fund['name'][:34]}", flush=True)
        had_new = fill_prices(conn, cik=fund["cik"], verbose=True)
        if had_new:
            funds_with_new_data.add(fund["cik"])

    # Step 3: 计算指标 + 输出（增量：只重算有新数据或尚无结果的基金）
    print(f"\n  ── Step 3/3  计算回测指标 ──────────────────────────────────────")
    # style IS NOT NULL 才算完整结果；NULL 说明分阶段数据缺失或被清除，需重算
    already_done = {r[0] for r in conn.execute(
        "SELECT cik FROM bt_fund_results WHERE style IS NOT NULL").fetchall()}

    all_metrics = []
    total = len(funds)
    for i, fund in enumerate(funds):
        cik = fund["cik"]
        skip = (not args.force
                and cik in already_done
                and cik not in funds_with_new_data)
        if skip:
            print(f"  [{i+1}/{total}] {fund['name'][:30]:<30} ↩ 无新数据，跳过")
            continue

        print(f"  [{i+1}/{total}] {fund['name'][:30]:<30} ... ", end="", flush=True)
        m = compute_fund_metrics(conn, cik, fund["name"])
        if not m:
            print("无数据")
            continue
        a1y = m.get("end_alpha_1y")
        a1y_str = f"{a1y:+.1f}pp" if a1y is not None else "N/A"
        rec = recommend(m)[:6]
        print(f"✓  {m['n_signals']:>3}个信号   季末1Yα: {a1y_str:<10}  {rec}")
        all_metrics.append(m)
        if not args.all:
            print_fund_report(m)

    if all_metrics:
        save_results_to_db(conn, all_metrics)

    if args.all:
        # 排行需要全部基金数据（含跳过的）
        all_saved = []
        for row in conn.execute("""
            SELECT cik, fund_name, n_signals, n_quarters,
                   end_alpha_6m, end_alpha_1y, end_hit_6m, end_hit_1y,
                   fil_alpha_6m, fil_alpha_1y, new_alpha_1y, n_new,
                   add_alpha_1y, n_add, best_signal, consistency, lag_cost,
                   ph1_alpha_1y, ph1_consistency, ph2_alpha_1y, ph2_consistency, style
            FROM bt_fund_results
        """).fetchall():
            keys = ["cik","fund_name","n_signals","n_quarters",
                    "end_alpha_6m","end_alpha_1y","end_hit_6m","end_hit_1y",
                    "fil_alpha_6m","fil_alpha_1y","new_alpha_1y","n_new",
                    "add_alpha_1y","n_add","best_signal","consistency","lag_cost",
                    "ph1_alpha_1y","ph1_consistency","ph2_alpha_1y","ph2_consistency","style"]
            m = dict(zip(keys, row))
            m["quarters"] = []          # 排行表不需要逐季明细
            all_saved.append(m)
        if all_saved:
            print_ranking(all_saved)

    print_cache_stats()
    conn.close()


if __name__ == "__main__":
    main()
