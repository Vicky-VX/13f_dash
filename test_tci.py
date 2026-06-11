"""
test_tci.py
单独测试 TCI Fund Management 的 13F 数据抓取
CIK: 0001647251

运行: python test_tci.py
成功后会打印最近4季持仓，并生成 tci_test.db
"""

import re
import sys
import time
import sqlite3
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8")

# ── 配置 ──────────────────────────────────────────────────────────────────────
CIK       = "1647251"          # TCI Fund Management（不含前导零）
N_QTRS    = 4                  # 抓最近几季
DB_PATH   = "tci_test.db"
HEADERS   = {"User-Agent": "13F Research Tool research@example.com"}
SLEEP     = 0.2                # 遵守 SEC 限速

# ── HTTP ──────────────────────────────────────────────────────────────────────
def get(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"  ✗ {e}")
        return None

# ── 步骤1：从 EDGAR browse 页面拿申报列表 ─────────────────────────────────────
def get_filing_accessions(cik, n):
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
           f"?action=getcompany&CIK={cik}&type=13F-HR"
           f"&dateb=&owner=include&count=10&search_text=")
    print(f"步骤1：获取申报列表\n  {url}")
    html = get(url)
    time.sleep(SLEEP)
    if not html:
        return []

    # 提取所有 accession numbers（格式 XXXXXXXXXX-YY-XXXXXX）
    accs = re.findall(r'(\d{10}-\d{2}-\d{6})-index', html)
    accs = list(dict.fromkeys(accs))  # 去重保序
    print(f"  找到 {len(accs)} 条申报，取前 {n} 条")
    return accs[:n]

# ── 步骤2：从 index 页面拿 period 和 filed 日期 ────────────────────────────────
def get_filing_meta(cik, accession):
    acc_clean = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{accession}-index.htm"
    html = get(url)
    time.sleep(SLEEP)
    if not html:
        # 部分文件用 .html 后缀
        url2 = url.replace("-index.htm", "-index.html")
        html = get(url2)
        time.sleep(SLEEP)
    if not html:
        return None, None

    period = re.search(r'Period of Report[^\d]*(\d{4}-\d{2}-\d{2})', html)
    filed  = re.search(r'Filing Date[^\d]*(\d{4}-\d{2}-\d{2})', html)
    return (period.group(1) if period else "?",
            filed.group(1)  if filed  else "?")

# ── 步骤3：下载 .txt 完整文件，解析持仓 XML ────────────────────────────────────
def get_holdings(cik, accession):
    acc_clean = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{accession}.txt"
    print(f"  下载: {url}")
    text = get(url)
    time.sleep(SLEEP)
    if not text:
        return [], 0

    # 提取 informationTable 段落
    m = re.search(r'<informationTable[^>]*>(.*?)</informationTable>', text, re.DOTALL)
    if not m:
        print("  ✗ 未找到 informationTable")
        return [], 0

    # 剥离所有命名空间：声明、带前缀的属性、标签前缀
    xml = m.group(0)
    xml = re.sub(r'\s+xmlns(?::[a-zA-Z0-9_-]+)?="[^"]*"', '', xml)   # xmlns 声明
    xml = re.sub(r'\s+[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+=(?:"[^"]*"|\'[^\']*\')', '', xml)  # prefix:attr="val"
    xml = re.sub(r'<([a-zA-Z0-9_-]+):',  '<',  xml)    # 开标签前缀
    xml = re.sub(r'</([a-zA-Z0-9_-]+):', '</', xml)    # 闭标签前缀

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        print(f"  ✗ XML 解析失败: {e}")
        return [], 0

    holdings = []
    total = 0
    for info in root.iter("infoTable"):
        def g(tag):
            el = info.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        val    = int(g("value") or 0)
        sh_el  = info.find("shrsOrPrnAmt")
        shares = int(sh_el.find("sshPrnamt").text) if sh_el is not None and sh_el.find("sshPrnamt") is not None else 0
        total += val
        holdings.append({
            "issuer":  g("nameOfIssuer"),
            "class":   g("titleOfClass"),
            "cusip":   g("cusip"),
            "value":   val,
            "shares":  shares,
        })

    for h in holdings:
        h["pct"] = round(h["value"] / total * 100, 2) if total else 0
    holdings.sort(key=lambda x: x["value"], reverse=True)

    # 统一单位：若最大值 >= 5亿，视为实际美元，转成千美元存储
    max_val = max((h["value"] for h in holdings), default=0)
    if max_val >= 500_000_000:
        for h in holdings:
            h["value"] = h["value"] // 1000
        total = total // 1000

    return holdings, total

# ── 数据库 ────────────────────────────────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS filings (
        id           INTEGER PRIMARY KEY,
        cik          TEXT,
        period       TEXT,
        filed_date   TEXT,
        accession_no TEXT UNIQUE,
        total_value  INTEGER,
        num_holdings INTEGER
    );
    CREATE TABLE IF NOT EXISTS holdings (
        id        INTEGER PRIMARY KEY,
        filing_id INTEGER REFERENCES filings(id),
        period    TEXT,
        issuer    TEXT,
        cusip     TEXT,
        value     INTEGER,
        shares    INTEGER,
        pct       REAL,
        UNIQUE(filing_id, cusip)
    );
    """)
    conn.commit()

def save(conn, cik, accession, period, filed, holdings, total):
    cur = conn.execute(
        "INSERT OR IGNORE INTO filings (cik,period,filed_date,accession_no,total_value,num_holdings) VALUES (?,?,?,?,?,?)",
        (cik, period, filed, accession, total, len(holdings))
    )
    fid = cur.lastrowid
    for h in holdings:
        conn.execute(
            "INSERT OR IGNORE INTO holdings (filing_id,period,issuer,cusip,value,shares,pct) VALUES (?,?,?,?,?,?,?)",
            (fid, period, h["issuer"], h["cusip"], h["value"], h["shares"], h["pct"])
        )
    conn.commit()
    return fid

# ── 打印结果 ──────────────────────────────────────────────────────────────────
def print_holdings(period, holdings, total):
    print(f"\n  ┌{'─'*58}┐")
    print(f"  │  {period}   {len(holdings)} 支持仓   总值 ${total/1e6:.1f}B{'':<12}│")
    print(f"  ├{'─'*58}┤")
    print(f"  │  {'持仓名称':<36} {'市值($M)':>8} {'占比':>6}  │")
    print(f"  ├{'─'*58}┤")
    for h in holdings:
        name = h["issuer"][:35]
        print(f"  │  {name:<36} {h['value']/1e3:>8,.0f} {h['pct']:>5.1f}%  │")
    print(f"  └{'─'*58}┘")

# ── 主程序 ────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'═'*62}")
    print(f"  TCI Fund Management  CIK:{CIK}  最近{N_QTRS}季")
    print(f"{'═'*62}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # 1. 获取申报列表
    accessions = get_filing_accessions(CIK, N_QTRS)
    if not accessions:
        print("\n✗ 无法获取申报列表，请检查网络连接")
        return

    # 2. 逐季下载
    for acc in accessions:
        print(f"\n步骤2+3：处理 {acc}")
        period, filed = get_filing_meta(CIK, acc)
        print(f"  Period: {period}  Filed: {filed}")

        # 检查是否已存在
        if conn.execute("SELECT 1 FROM filings WHERE accession_no=?", (acc,)).fetchone():
            print(f"  ↩ 已存在，跳过")
            # 直接打印已有数据
            rows = conn.execute(
                "SELECT issuer,value,pct FROM holdings h JOIN filings f ON h.filing_id=f.id WHERE f.accession_no=? ORDER BY value DESC",
                (acc,)
            ).fetchall()
            holdings = [{"issuer": r[0], "value": r[1], "pct": r[2]} for r in rows]
            total = sum(h["value"] for h in holdings)
            print_holdings(period, holdings, total)
            continue

        holdings, total = get_holdings(CIK, acc)
        if not holdings:
            continue

        save(conn, CIK, acc, period, filed, holdings, total)
        print_holdings(period, holdings, total)

    print(f"\n{'═'*62}")
    print(f"  数据已存入 {DB_PATH}")
    print(f"  查询示例：sqlite3 {DB_PATH} 'SELECT period,issuer,pct FROM holdings ORDER BY period DESC,pct DESC'")
    print(f"{'═'*62}\n")
    conn.close()

if __name__ == "__main__":
    main()
