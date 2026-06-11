"""
fetch_all.py
从 SEC EDGAR 拉取基金池内所有基金最近4季13F持仓，统一落入 holdings.db。

关键修复：第三方申报人（accession前缀 ≠ 基金CIK）时，
先解析 index.htm 确认实际文件名，再下载。
支持两种文档格式：
  - 完整提交 .txt（内嵌 <informationTable>）
  - 独立 infotable .xml
"""

import re
import sys
import json
import time
import sqlite3
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

FUND_POOL = "fund_pool.json"
DB_PATH   = "holdings.db"
N_QTRS    = 4
HEADERS   = {"User-Agent": "13F Research Tool research@example.com"}
SLEEP     = 0.25

QUARTER_MAP = {"03": "Q1", "06": "Q2", "09": "Q3", "12": "Q4"}


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"    Error: {e}")
        return None


# ── 申报列表 ──────────────────────────────────────────────────────────────────

def get_accessions(cik: str, n: int) -> list[str]:
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
           f"?action=getcompany&CIK={cik}&type=13F-HR"
           f"&dateb=&owner=include&count=20&search_text=")
    html = fetch(url)
    time.sleep(SLEEP)
    if not html:
        return []
    accs = re.findall(r'(\d{10}-\d{2}-\d{6})-index', html)
    return list(dict.fromkeys(accs))[:n]


def get_filing_meta(cik: str, acc: str) -> tuple[str, str]:
    acc_clean = acc.replace("-", "")
    for suffix in ("-index.htm", "-index.html"):
        html = fetch(f"https://www.sec.gov/Archives/edgar/data"
                     f"/{cik}/{acc_clean}/{acc}{suffix}")
        time.sleep(SLEEP)
        if html:
            period = re.search(r'Period of Report[^\d]*(\d{4}-\d{2}-\d{2})', html)
            filed  = re.search(r'Filing Date[^\d]*(\d{4}-\d{2}-\d{2})', html)
            return (period.group(1) if period else "?",
                    filed.group(1)  if filed  else "?")
    return "?", "?"


def period_to_quarter(period: str) -> str:
    try:
        yr, mo, _ = period.split("-")
        return f"{yr}{QUARTER_MAP.get(mo, '??')}"
    except Exception:
        return period


# ── 关键修复：从 index.htm 定位实际文档 URL ───────────────────────────────────

def get_document_url(cik: str, acc: str) -> tuple[str, str]:
    """
    解析 filing index 页面，返回 (url, fmt)。
    fmt = 'xml'  → 独立 infotable XML，直接解析
    fmt = 'txt'  → 完整提交文本，搜索 informationTable 段落
    fmt = ''     → 找不到，放弃

    关键规则：
    - 过滤 xslForm13F_X02/ 路径（EDGAR 样式渲染层，非原始文件）
    - 过滤 primary_doc.xml（封面，不含持仓）
    - 剩余第一个 .xml 即为 infotable
    """
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

    # 提取所有 /Archives/... href，去掉渲染层路径
    all_hrefs = re.findall(r'href="(/Archives/edgar/data/[^"]+)"', index_html)
    hrefs = [h for h in all_hrefs
             if "xslForm13F_X02" not in h          # 过滤样式渲染路径
             and "xslForm13F_X03" not in h]

    # 候选 xml：排除封面 primary_doc.xml
    xml_hrefs = [h for h in hrefs
                 if h.lower().endswith(".xml")
                 and "primary_doc" not in h.lower()]

    if xml_hrefs:
        return "https://www.sec.gov" + xml_hrefs[0], "xml"

    # 兜底：完整提交 .txt
    txt_hrefs = [h for h in hrefs if h.lower().endswith(".txt")]
    if txt_hrefs:
        return "https://www.sec.gov" + txt_hrefs[0], "txt"

    return "", ""


# ── XML 命名空间剥离 ──────────────────────────────────────────────────────────

def strip_ns(xml_str: str) -> str:
    xml_str = re.sub(r'\s+xmlns(?::[a-zA-Z0-9_-]+)?="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+=(?:"[^"]*"|\'[^\']*\')', '', xml_str)
    xml_str = re.sub(r'<([a-zA-Z0-9_-]+):',  '<',  xml_str)
    xml_str = re.sub(r'</([a-zA-Z0-9_-]+):', '</', xml_str)
    return xml_str


# ── 持仓解析（两种格式）──────────────────────────────────────────────────────

def extract_holdings_from_xml(content: str) -> tuple[list[dict], int]:
    """直接解析独立 infotable XML（先剥离命名空间再匹配）。"""
    cleaned = strip_ns(content)
    m = re.search(r'(<informationTable[^>]*>.*?</informationTable>)',
                  cleaned, re.DOTALL | re.IGNORECASE)
    if not m:
        return [], 0
    try:
        root = ET.fromstring(m.group(1))
    except ET.ParseError as e:
        print(f"    XML 解析失败: {e}")
        return [], 0
    return _parse_root(root)


def extract_holdings_from_txt(content: str) -> tuple[list[dict], int]:
    """从完整提交 .txt 中提取 informationTable 段落后解析（先剥离命名空间）。"""
    cleaned = strip_ns(content)
    m = re.search(r'(<informationTable[^>]*>.*?</informationTable>)',
                  cleaned, re.DOTALL | re.IGNORECASE)
    if not m:
        return [], 0
    try:
        root = ET.fromstring(m.group(1))
    except ET.ParseError as e:
        print(f"    XML 解析失败: {e}")
        return [], 0
    return _parse_root(root)


def _parse_root(root) -> tuple[list[dict], int]:
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
            "class":    g(info, "titleOfClass"),
            "value":    val,
            "shares":   shares,
            "put_call": g(info, "putCall"),
        })

    if not holdings:
        return [], 0

    # 单位统一：新版为实际美元，旧版为千美元
    max_val = max(h["value"] for h in holdings)
    if max_val >= 500_000_000:
        for h in holdings:
            h["value"] = h["value"] // 1000
        total = total // 1000

    for h in holdings:
        h["pct"] = round(h["value"] / total * 100, 4) if total else 0.0
    holdings.sort(key=lambda x: x["value"], reverse=True)
    return holdings, total


def parse_holdings(cik: str, acc: str) -> tuple[list[dict], int]:
    url, fmt = get_document_url(cik, acc)
    if not url:
        return [], 0

    content = fetch(url)
    time.sleep(SLEEP)
    if not content:
        return [], 0

    if fmt == "xml":
        return extract_holdings_from_xml(content)
    else:
        return extract_holdings_from_txt(content)


# ── 数据库 ────────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS funds (
        cik     TEXT PRIMARY KEY,
        name    TEXT,
        manager TEXT,
        style   TEXT,
        layer   TEXT
    );
    CREATE TABLE IF NOT EXISTS filings (
        id           INTEGER PRIMARY KEY,
        cik          TEXT,
        quarter      TEXT,
        period       TEXT,
        filed_date   TEXT,
        accession_no TEXT UNIQUE,
        total_value  INTEGER,
        num_holdings INTEGER
    );
    CREATE TABLE IF NOT EXISTS holdings (
        id        INTEGER PRIMARY KEY,
        filing_id INTEGER REFERENCES filings(id),
        cik       TEXT,
        quarter   TEXT,
        issuer    TEXT,
        cusip     TEXT,
        class     TEXT,
        value     INTEGER,
        shares    INTEGER,
        pct       REAL,
        put_call  TEXT,
        UNIQUE(filing_id, cusip, put_call)
    );
    CREATE INDEX IF NOT EXISTS idx_h_cusip   ON holdings(cusip);
    CREATE INDEX IF NOT EXISTS idx_h_quarter ON holdings(quarter);
    CREATE INDEX IF NOT EXISTS idx_h_cik     ON holdings(cik);
    """)
    conn.commit()


def upsert_fund(conn, fund: dict):
    conn.execute("""
        INSERT OR REPLACE INTO funds (cik, name, manager, style, layer)
        VALUES (?, ?, ?, ?, ?)
    """, (fund["cik"], fund["name"], fund.get("manager", ""),
          fund.get("style", ""), fund.get("layer", "")))
    conn.commit()


def save_filing(conn, cik, quarter, period, filed, acc, holdings, total) -> int:
    cur = conn.execute("""
        INSERT OR IGNORE INTO filings
        (cik, quarter, period, filed_date, accession_no, total_value, num_holdings)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (cik, quarter, period, filed, acc, total, len(holdings)))
    if cur.lastrowid == 0:
        row = conn.execute(
            "SELECT id FROM filings WHERE accession_no=?", (acc,)).fetchone()
        return row[0] if row else -1
    fid = cur.lastrowid
    conn.executemany("""
        INSERT OR IGNORE INTO holdings
        (filing_id, cik, quarter, issuer, cusip, class, value, shares, pct, put_call)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(fid, cik, quarter,
           h["issuer"], h["cusip"], h["class"],
           h["value"], h["shares"], h["pct"], h["put_call"])
          for h in holdings])
    conn.commit()
    return fid


# ── 单基金拉取 ────────────────────────────────────────────────────────────────

def fetch_fund(fund: dict, conn, target_ciks: set | None = None) -> dict:
    name = fund["name"]
    cik  = fund["cik"]
    cik_raw = cik.lstrip("0")

    if target_ciks and cik not in target_ciks:
        return {"ok": 0, "skip": 1, "fail": 0}

    upsert_fund(conn, fund)
    print(f"\n  [{name}]  CIK:{cik}")

    accs = get_accessions(cik_raw, N_QTRS)
    if not accs:
        print(f"    ✗ 无法获取申报列表")
        return {"ok": 0, "skip": 0, "fail": 1}

    ok = skip = fail = 0
    for acc in accs:
        if conn.execute("SELECT 1 FROM filings WHERE accession_no=?",
                        (acc,)).fetchone():
            q = conn.execute("SELECT quarter FROM filings WHERE accession_no=?",
                             (acc,)).fetchone()[0]
            print(f"    ✓ {q} 已缓存")
            skip += 1
            continue

        period, filed = get_filing_meta(cik_raw, acc)
        quarter = period_to_quarter(period)
        print(f"    → {quarter} ({filed})  ", end="", flush=True)

        holdings, total = parse_holdings(cik_raw, acc)
        if not holdings:
            print("✗ 解析失败")
            fail += 1
            continue

        save_filing(conn, cik, quarter, period, filed, acc, holdings, total)
        print(f"✓  {len(holdings)} 只  ${total/1e6:.1f}B")
        ok += 1

    return {"ok": ok, "skip": skip, "fail": fail}


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    import sys as _sys
    # 支持 --only 参数过滤指定基金（按CIK或名称片段）
    only_filter = None
    if "--only" in _sys.argv:
        idx = _sys.argv.index("--only")
        only_filter = set(_sys.argv[idx+1:])

    with open(FUND_POOL, encoding="utf-8") as f:
        config = json.load(f)
    funds = config["funds"]

    # 过滤：名称包含关键词 或 CIK 匹配
    if only_filter:
        funds = [f for f in funds
                 if any(kw.lower() in f["name"].lower() or kw in f["cik"]
                        for kw in only_filter)]
        print(f"  → 仅处理: {[f['name'] for f in funds]}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print(f"\n{'═'*64}")
    print(f"  全量13F拉取  {len(funds)} 家基金 × 最近{N_QTRS}季")
    print(f"  目标数据库: {DB_PATH}")
    print(f"{'═'*64}")

    total_ok = total_skip = total_fail = 0
    for fund in funds:
        r = fetch_fund(fund, conn)
        total_ok   += r["ok"]
        total_skip += r["skip"]
        total_fail += r["fail"]
        time.sleep(0.3)

    n_filings  = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    n_holdings = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    conn.close()

    print(f"\n{'═'*64}")
    print(f"  完成  ✓{total_ok} 新增  ↩{total_skip} 已缓存  ✗{total_fail} 失败")
    print(f"  holdings.db: {n_filings} filings  {n_holdings:,} 条持仓记录")
    print(f"{'═'*64}\n")


if __name__ == "__main__":
    main()
