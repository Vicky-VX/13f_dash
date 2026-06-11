"""
verify_ciks.py
验证 fund_pool.json 中所有26家基金的 CIK 是否能在 SEC EDGAR 正常拉到13F数据
运行: python verify_ciks.py
"""

import json
import sys
import time
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8")

EDGAR_HEADERS = {
    "User-Agent": "13F Research Tool research@example.com",  # SEC要求提供联系方式
}

def get_company_info(cik: str) -> dict | None:
    """从 EDGAR 拉取公司基本信息，验证 CIK 有效"""
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}

def get_latest_13f(cik: str) -> dict | None:
    """拉取该 CIK 最近一次13F申报信息"""
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])

        # 找最近一次 13F-HR
        for i, form in enumerate(forms):
            if form in ("13F-HR", "13F-HR/A"):
                return {
                    "form": form,
                    "date": dates[i] if i < len(dates) else "N/A",
                    "accession": accessions[i] if i < len(accessions) else "N/A"
                }
        return {"form": "NOT FOUND", "date": None, "accession": None}

    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}

def main():
    with open("fund_pool.json", encoding="utf-8") as f:
        config = json.load(f)

    funds = config["funds"]
    print(f"\n{'='*72}")
    print(f"  13F基金池 CIK 验证报告  ({len(funds)} 家机构)")
    print(f"{'='*72}\n")

    results = []
    ok_count = 0
    warn_count = 0
    fail_count = 0

    for fund in funds:
        name = fund["name"]
        cik  = fund["cik"]
        note = fund.get("note", "")

        info = get_company_info(cik)
        time.sleep(0.15)  # 遵守 SEC 限速 (10 req/s)

        if "error" in info:
            status = "FAIL"
            edgar_name = ""
            latest = {"error": info["error"]}
            fail_count += 1
        else:
            edgar_name = info.get("name", "")
            latest = get_latest_13f(cik)
            time.sleep(0.15)

            if "error" in latest:
                status = "FAIL"
                fail_count += 1
            elif latest.get("form") == "NOT FOUND":
                status = "WARN"
                warn_count += 1
            else:
                status = "OK"
                ok_count += 1

        results.append({
            "name": name,
            "cik": cik,
            "edgar_name": edgar_name,
            "status": status,
            "latest_13f": latest,
            "note": note
        })

        # 实时打印
        icon = "✓" if status == "OK" else ("△" if status == "WARN" else "✗")
        latest_date = latest.get("date", "") if "error" not in latest else latest.get("error", "")
        print(f"  {icon}  {name[:38]:<38}  CIK:{cik}  {latest_date}")
        if status != "OK":
            print(f"       └─ {latest}")
        if "需验证" in note:
            print(f"       └─ ⚠ 注意：{note}")

    print(f"\n{'─'*72}")
    print(f"  结果汇总：✓ {ok_count} 正常   △ {warn_count} 无13F记录   ✗ {fail_count} 失败")
    print(f"{'─'*72}\n")

    # 保存结果
    with open("verify_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("  结果已保存至 verify_results.json\n")

    if fail_count > 0 or warn_count > 0:
        print("  需要修正的 CIK：")
        for r in results:
            if r["status"] != "OK":
                print(f"    - {r['name']} ({r['cik']}): {r['latest_13f']}")
        print()

if __name__ == "__main__":
    main()
