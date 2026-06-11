"""
fetch_holdings.py
从 SEC EDGAR 拉取基金池内所有基金最近2季13F持仓，存入 holdings/ 目录。
每个文件命名为 {CIK}_{year}Q{n}.json，包含完整持仓列表及仓位占比。
运行: python fetch_holdings.py
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HEADERS = {"User-Agent": "13F Research Tool research@example.com"}
HOLDINGS_DIR = Path("holdings")
HOLDINGS_DIR.mkdir(exist_ok=True)

QUARTER_MAP = {"03": "Q1", "06": "Q2", "09": "Q3", "12": "Q4"}


# ── 网络工具 ──────────────────────────────────────────────────────────────────

def edgar_get(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        print(f"      HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"      Error: {e}")
        return None


# ── 申报索引 ──────────────────────────────────────────────────────────────────

def get_13f_filings(cik: str, n: int = 4) -> list[dict]:
    """返回最近 n 次 13F-HR（含修正版）的申报元数据。"""
    cik_padded = cik.lstrip("0").zfill(10)
    data = edgar_get(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    if not data:
        return []

    info = json.loads(data)
    recent = info.get("filings", {}).get("recent", {})
    forms        = recent.get("form", [])
    dates        = recent.get("filingDate", [])
    accessions   = recent.get("accessionNumber", [])
    periods      = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])

    results = []
    seen_periods = set()
    for i, form in enumerate(forms):
        if form not in ("13F-HR", "13F-HR/A"):
            continue
        period = periods[i]
        # 同一报告期只取最新一份（13F-HR/A 覆盖 13F-HR）
        if period in seen_periods:
            continue
        seen_periods.add(period)
        results.append({
            "form":           form,
            "date":           dates[i],
            "period":         period,
            "accession":      accessions[i],
            "primary_doc":    primary_docs[i] if i < len(primary_docs) else "",
        })
        if len(results) >= n:
            break

    return results


def period_to_quarter(period: str) -> str:
    """'2024-12-31' → '2024Q4'"""
    yr, mo, _ = period.split("-")
    return f"{yr}{QUARTER_MAP.get(mo, 'Q?')}"


# ── infotable 定位 ─────────────────────────────────────────────────────────────

# EDGAR 13F infotable 常见文件名，按优先级排列
_INFOTABLE_CANDIDATES = [
    "Form13fInfoTable.xml",
    "form13fInfoTable.xml",
    "infotable.xml",
    "InfoTable.xml",
]

def get_infotable_url(cik: str, accession: str) -> str | None:
    """
    定位 infotable XML。
    策略：先逐一尝试常见文件名，失败后解析 filing 目录 HTML 找 XML 文件。
    注：浏览器地址栏中的 xslForm13F_X02/ 是 EDGAR 渲染层，
        实际文件就在 accession 目录根下。
    """
    cik_raw    = str(int(cik.lstrip("0") or "0"))
    acc_nodash = accession.replace("-", "")
    base       = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_nodash}"

    # 1. 直接尝试常见文件名（HEAD 请求快速探测）
    for fname in _INFOTABLE_CANDIDATES:
        url  = f"{base}/{fname}"
        data = edgar_get(url)
        time.sleep(0.1)
        if data and b"<" in data:   # 是真实的 XML 内容
            return url

    # 2. 兜底：解析目录 HTML，找 infotable XML
    folder = edgar_get(f"{base}/")
    if not folder:
        return None
    html = folder.decode("utf-8", errors="replace")

    # EDGAR 目录页 href 可能是绝对路径(/Archives/...)或相对文件名
    # 提取所有 .xml 链接，统一取文件名部分
    all_hrefs = re.findall(r'href="([^"]+\.xml)"', html, re.IGNORECASE)
    filenames = []
    for href in all_hrefs:
        fname = href.split("/")[-1]   # 取最后一段作为文件名
        if fname:
            filenames.append(fname)

    # 优先：含 infotable 关键字
    for fname in filenames:
        if "infotable" in fname.lower():
            return f"{base}/{fname}"

    # 次选：其他 xml，排除全文 submission txt 和已知非持仓文件
    skip_patterns = (acc_nodash, "primary", "cover", "header")
    for fname in filenames:
        fl = fname.lower()
        if not any(p in fl for p in skip_patterns):
            return f"{base}/{fname}"

    return None


# ── XML 解析 ───────────────────────────────────────────────────────────────────

def strip_namespaces(xml_bytes: bytes) -> str:
    """剥离所有 XML 命名空间声明和前缀，让 ElementTree 用简单标签名查询。"""
    text = xml_bytes.decode("utf-8", errors="replace")
    # 去掉所有 xmlns 属性（含 xmlns:prefix="..."）
    text = re.sub(r'\s+xmlns(?::[a-zA-Z0-9_-]+)?="[^"]*"', "", text)
    # 去掉开标签里的 prefix:（<ns:tag → <tag）
    text = re.sub(r"<([a-zA-Z0-9_-]+):([a-zA-Z0-9_-])", r"<\2", text)
    # 去掉闭标签里的 prefix:（</ns:tag → </tag）
    text = re.sub(r"</([a-zA-Z0-9_-]+):([a-zA-Z0-9_-])", r"</\2", text)
    # 去掉属性里的 prefix:（xsi:type="..." → type="..."）
    text = re.sub(r'\s+[a-zA-Z0-9_-]+:([a-zA-Z0-9_-]+=)', r" \1", text)
    return text


def parse_infotable(xml_bytes: bytes) -> list[dict]:
    """解析 infotable XML → 持仓列表（已按市值降序排列，含仓位占比）。"""
    root = ET.fromstring(strip_namespaces(xml_bytes))

    def g(node, tag: str) -> str:
        el = node.find(tag)
        return el.text.strip() if el is not None and el.text else ""

    raw = []
    for entry in root.findall(".//infoTable"):
        value_str  = g(entry, "value").replace(",", "")
        shares_str = g(entry, "sshPrnamt").replace(",", "")
        raw.append({
            "issuer":    g(entry, "nameOfIssuer"),
            "cusip":     g(entry, "cusip"),
            "class":     g(entry, "titleOfClass"),
            "value_raw": int(value_str)  if value_str.isdigit() else 0,
            "shares":    int(shares_str) if shares_str.isdigit() else 0,
            "sh_type":   g(entry, "sshPrnamtType"),
            "put_call":  g(entry, "putCall"),
        })

    # 新版 13F schema value 为实际美元；旧版为千美元。
    # 阈值：最大单笔 > $500M (5e8) → 实际美元单位，统一除 1000 转 value_k
    # 旧格式千美元最大值通常 < 50,000,000 (=$50B)，新格式实际美元 > 500,000,000
    max_val = max((h["value_raw"] for h in raw), default=0)
    to_k    = (lambda v: v // 1000) if max_val >= 500_000_000 else (lambda v: v)

    holdings = []
    for h in raw:
        holdings.append({
            "issuer":   h["issuer"],
            "cusip":    h["cusip"],
            "class":    h["class"],
            "value_k":  to_k(h["value_raw"]),   # 统一：千美元
            "shares":   h["shares"],
            "sh_type":  h["sh_type"],
            "put_call": h["put_call"],
        })

    holdings.sort(key=lambda x: x["value_k"], reverse=True)

    total = sum(h["value_k"] for h in holdings)
    for h in holdings:
        h["pct"] = round(h["value_k"] / total * 100, 2) if total else 0.0

    return holdings


# ── 单基金拉取 ─────────────────────────────────────────────────────────────────

def fetch_fund(fund: dict) -> None:
    name = fund["name"]
    cik  = fund["cik"]
    print(f"\n  [{name}]  CIK:{cik}")

    filings = get_13f_filings(cik, n=4)
    time.sleep(0.2)

    if not filings:
        print("    ✗ 未找到13F申报")
        return

    for filing in filings:
        quarter  = period_to_quarter(filing["period"])
        out_file = HOLDINGS_DIR / f"{cik}_{quarter}.json"

        if out_file.exists():
            print(f"    ✓ {quarter} 已缓存，跳过")
            continue

        print(f"    → {quarter} ({filing['date']}) 获取索引...")
        infotable_url = get_infotable_url(cik, filing["accession"])
        time.sleep(0.2)

        if not infotable_url:
            print(f"    ✗ {quarter} 找不到 infotable.xml")
            continue

        xml_bytes = edgar_get(infotable_url)
        time.sleep(0.2)

        if not xml_bytes:
            print(f"    ✗ {quarter} XML 下载失败")
            continue

        holdings = parse_infotable(xml_bytes)
        total_k  = sum(h["value_k"] for h in holdings)

        result = {
            "fund":         name,
            "cik":          cik,
            "quarter":      quarter,
            "period_end":   filing["period"],
            "filing_date":  filing["date"],
            "accession":    filing["accession"],
            "total_value_k": total_k,
            "position_count": len(holdings),
            "holdings":     holdings,
        }

        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    ✓ {quarter}: {len(holdings)} 只持仓  总市值 ${total_k/1_000_000:.1f}B")


# ── 主程序 ─────────────────────────────────────────────────────────────────────

def main():
    with open("fund_pool.json", encoding="utf-8") as f:
        config = json.load(f)

    funds = config["funds"]
    print(f"\n{'='*62}")
    print(f"  13F 持仓拉取  ({len(funds)} 家基金 × 最近4季)")
    print(f"{'='*62}")

    for fund in funds:
        fetch_fund(fund)
        time.sleep(0.3)

    files = list(HOLDINGS_DIR.glob("*.json"))
    print(f"\n{'='*62}")
    print(f"  完成：holdings/ 目录共 {len(files)} 个文件")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
