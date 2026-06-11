"""
dashboard.py  ─  13F 机构持仓看板  (Notion/Linear 极简风格)
streamlit run dashboard.py
"""

import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import sqlite3, json, html as _html
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# ── 页面配置 ──────────────────────────────────────────────────────────────────

st.set_page_config(layout="wide", page_title="13F 机构持仓", page_icon="📊")

# ── 全局 CSS ──────────────────────────────────────────────────────────────────

st.markdown("""
<style>
/* 全局字体和背景 */
html, body, .stApp {
    background: #f8f9fa !important;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                 'Helvetica Neue', Arial, sans-serif;
    color: #111827;
}
.block-container { padding: 2rem 3rem !important; max-width: 1400px !important; }

/* 顶部栏 */
header[data-testid="stHeader"] {
    background: #ffffff !important;
    border-bottom: 1px solid #e5e7eb !important;
}

/* KPI 指标卡 */
div[data-testid="metric-container"] {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 20px 24px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,.04);
}
div[data-testid="metric-container"] label {
    color: #6b7280 !important;
    font-size: 12px !important;
    text-transform: uppercase;
    letter-spacing: .5px;
}
div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
    color: #111827 !important;
    font-size: 26px !important;
    font-weight: 700 !important;
}
div[data-testid="metric-container"] div[data-testid="stMetricDelta"] {
    color: #6b7280 !important;
}

/* Tab */
div[data-testid="stTabs"] [role="tablist"] {
    border-bottom: 1px solid #e5e7eb;
    gap: 4px;
    background: transparent;
}
div[data-testid="stTabs"] button[role="tab"] {
    font-size: 14px;
    color: #6b7280;
    border-radius: 6px;
    padding: 6px 16px;
    border: none;
    background: transparent;
}
div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
    color: #111827 !important;
    font-weight: 500;
    background: #f3f4f6;
}

/* DataFrame */
.stDataFrame {
    border: 1px solid #e5e7eb !important;
    border-radius: 12px !important;
    overflow: hidden !important;
}

/* Text input */
.stTextInput input {
    background: #ffffff !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 8px !important;
    color: #111827 !important;
    font-size: 14px;
}
.stTextInput input:focus {
    border-color: #3b82f6 !important;
    box-shadow: 0 0 0 3px rgba(59,130,246,.1) !important;
}

/* Expander */
.streamlit-expanderHeader {
    background: #ffffff !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 8px !important;
    color: #374151 !important;
}

/* Slider */
.stSlider [data-baseweb="slider"] { background: #f3f4f6; }

/* 信号表格 */
.sig-table {
    width: 100%;
    border-collapse: collapse;
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    overflow: hidden;
    font-size: 13.5px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
.sig-table thead tr { background: #f3f4f6; }
.sig-table th {
    color: #6b7280;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .5px;
    font-weight: 600;
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid #e5e7eb;
}
.sig-table td {
    padding: 10px 14px;
    color: #374151;
    border-bottom: 1px solid #f3f4f6;
    vertical-align: middle;
}
.sig-table td a, .sig-table td a:visited {
    color: #374151 !important;
    text-decoration: none !important;
    pointer-events: none !important;
    cursor: default !important;
}
.sig-table tr:last-child td { border-bottom: none; }
.sig-table tr:nth-child(even) td { background: #f9fafb; }
.sig-table tr:hover td { background: #eff6ff; }

/* 信号标签 */
.tag {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 11.5px;
    font-weight: 600;
    border: 1px solid;
}
.tag-new  { background:#eff6ff; color:#2563eb; border-color:#bfdbfe; }
.tag-add  { background:#f0fdf4; color:#16a34a; border-color:#bbf7d0; }
.tag-trim { background:#fffbeb; color:#d97706; border-color:#fde68a; }
.tag-exit { background:#fef2f2; color:#dc2626; border-color:#fecaca; }

/* 进度条 */
.bar-bg { background:#f3f4f6; border-radius:4px; height:6px; margin-top:4px; min-width:90px; }
.bar-fg { border-radius:4px; height:6px; }

/* 判断框 */
.verdict-box {
    border-radius: 10px;
    padding: 16px 20px;
    margin-top: 16px;
    border: 1px solid;
}
.verdict-warn    { background:#fffbeb; border-color:#fde68a; }
.verdict-ok      { background:#f0fdf4; border-color:#bbf7d0; }
.verdict-below   { background:#eff6ff; border-color:#bfdbfe; }
.verdict-title   { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
.verdict-body    { font-size: 13px; color: #4b5563; }

hr { border: none; border-top: 1px solid #e5e7eb !important; margin: 16px 0; }

/* 共识等级徽章 */
.badge-s4 { background:#d1fae5;color:#065f46;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap; }
.badge-s3 { background:#dcfce7;color:#16a34a;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap; }
.badge-s2 { background:#dbeafe;color:#1d4ed8;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap; }
.badge-s1 { background:#f3f4f6;color:#6b7280;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap; }
/* 新仓 / 共识内联徽章 */
.badge-new { background:#dcfce7;color:#16a34a;padding:1px 6px;border-radius:8px;font-size:10px;font-weight:700;margin-left:4px; }
.badge-con { background:#dbeafe;color:#1d4ed8;padding:1px 6px;border-radius:8px;font-size:10px;font-weight:700;margin-left:4px; }
/* 新仓预警区块 */
.alert-block { background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 20px;margin-bottom:16px; }
.alert-title { font-weight:700;font-size:14px;margin-bottom:10px;color:#111827; }
/* 脚注 */
.footnote { color:#9ca3af; font-size:11.5px; margin-top:6px; }
</style>
""", unsafe_allow_html=True)

# ── 常量 ──────────────────────────────────────────────────────────────────────

DB_PATH       = Path("holdings.db")
DATA_DIR      = Path("data")
CT_CACHE_FILE = Path("cusip_ticker_cache.json")
SC_CACHE_FILE = Path("sector_cache.json")

# 云端模式：holdings.db 不存在时自动切换到 data/ CSV
USE_CSV = not DB_PATH.exists()

SECTOR_NORM = {
    "Financial Services":  "Financials",
    "Consumer Cyclical":   "Consumer Discretionary",
    "Consumer Defensive":  "Consumer Staples",
    "Basic Materials":     "Materials",
    "Health Care":         "Healthcare",
}

Q_COLORS = ["#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb"]

LIGHT_PLOTLY = dict(
    paper_bgcolor="#ffffff",
    plot_bgcolor="#ffffff",
    font=dict(color="#374151",
              family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"),
    margin=dict(t=30, b=20, l=0, r=0),
)

# 推荐组：(icon, rec_label, allowed_actions)
RECOMMEND_GROUPS = [
    ("🟢", "★★★★ 强烈推荐",      ["NEW", "ADD"]),
    ("🔵", "★★★  眼光准需快速执行", ["NEW", "ADD"]),
    ("🟡", "★★   选择性跟随",     ["NEW", "ADD"]),
]


# ── 数据加载（缓存）──────────────────────────────────────────────────────────

def _ct_path() -> Path:
    """JSON cache: prefer data/ on cloud, local file otherwise."""
    cloud = DATA_DIR / "cusip_ticker_cache.json"
    return cloud if USE_CSV and cloud.exists() else CT_CACHE_FILE

def _sc_path() -> Path:
    cloud = DATA_DIR / "sector_cache.json"
    return cloud if USE_CSV and cloud.exists() else SC_CACHE_FILE

@st.cache_data(ttl=3600)
def load_ct() -> dict:
    p = _ct_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

@st.cache_data(ttl=3600)
def load_sc() -> dict:
    p = _sc_path()
    raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {k: SECTOR_NORM.get(v, v) for k, v in raw.items()}

@st.cache_data(ttl=3600)
def load_df() -> pd.DataFrame:
    if USE_CSV:
        holdings = pd.read_csv(DATA_DIR / "holdings.csv")
        filings  = pd.read_csv(DATA_DIR / "filings.csv")
        funds    = pd.read_csv(DATA_DIR / "funds.csv")
        df = (holdings[holdings["put_call"].isna() | (holdings["put_call"] == "")]
              .merge(filings[["id", "cik", "quarter", "period"]],
                     left_on="filing_id", right_on="id", suffixes=("", "_f"))
              .merge(funds[["cik", "name"]].rename(columns={"name": "fund_name",
                                                             "cik":  "cik_fund"}),
                     left_on="cik", right_on="cik_fund")
              [["cik", "fund_name", "quarter", "period",
                "cusip", "issuer", "value", "shares", "pct"]]
              .sort_values(["quarter", "cik", "value"], ascending=[True, True, False]))
    else:
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql("""
            SELECT f.cik, fd.name AS fund_name, f.quarter, f.period,
                   h.cusip, h.issuer, h.value, h.shares, h.pct
            FROM holdings h
            JOIN filings f  ON h.filing_id = f.id
            JOIN funds   fd ON f.cik = fd.cik
            WHERE h.put_call IS NULL OR h.put_call = ''
            ORDER BY f.quarter, f.cik, h.value DESC
        """, conn)
        conn.close()
    df["issuer"] = df["issuer"].fillna("").astype(str).str.strip()
    return df

@st.cache_data(ttl=3600)
def load_quarters() -> list[str]:
    if USE_CSV:
        filings = pd.read_csv(DATA_DIR / "filings.csv")
        return sorted(filings["quarter"].dropna().unique().tolist())
    conn = sqlite3.connect(str(DB_PATH))
    qs = [r[0] for r in conn.execute(
        "SELECT DISTINCT quarter FROM filings ORDER BY quarter").fetchall()]
    conn.close()
    return qs

@st.cache_data(ttl=3600)
def load_filing_dates() -> dict:
    """Returns {(cik, quarter): filed_date} for price lookups at disclosure time."""
    if USE_CSV:
        filings = pd.read_csv(DATA_DIR / "filings.csv")
        return {(str(r.cik), str(r.quarter)): str(r.filed_date)
                for r in filings[["cik", "quarter", "filed_date"]].itertuples()}
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT cik, quarter, filed_date FROM filings").fetchall()
    conn.close()
    return {(str(cik), str(quarter)): str(filed_date) for cik, quarter, filed_date in rows}

@st.cache_data(ttl=300)
def fetch_now(ticker: str) -> float | None:
    if not ticker or len(ticker) > 12 or any(c in ticker for c in "/*"):
        return None
    try:
        import yfinance as yf
        return float(yf.Ticker(ticker).fast_info.last_price)
    except Exception:
        return None

@st.cache_data(ttl=3600)
def fetch_history_6mo(ticker: str) -> pd.DataFrame:
    """6个月日线数据，用于信号行展开图。"""
    if not ticker or any(c in ticker for c in "/* "):
        return pd.DataFrame()
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="6mo")
        if hist.empty:
            return pd.DataFrame()
        hist = hist.reset_index()
        # Date 列统一为 tz-naive date string，方便 vline 对齐
        hist["Date"] = pd.to_datetime(hist["Date"]).dt.tz_localize(None)
        return hist
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=86400)
def search_ticker_by_name(company_name: str) -> str:
    """用公司名通过 yf.Search 反查 ticker，结果缓存24小时。"""
    if not company_name or company_name in ("—", "nan"):
        return ""
    try:
        import yfinance as yf
        result = yf.Search(company_name[:40], max_results=1)
        quotes = getattr(result, "quotes", [])
        if quotes:
            sym = quotes[0].get("symbol", "")
            # 过滤掉带交易所后缀的非美股代码（如 "AAPL.L"）
            if sym and "." not in sym and len(sym) <= 10:
                return sym
    except Exception:
        pass
    return ""

@st.cache_data(ttl=3600)
def fetch_hist(ticker: str, date_str: str) -> float | None:
    if not ticker or len(ticker) > 12 or any(c in ticker for c in "/*"):
        return None
    try:
        import yfinance as yf
        d     = datetime.strptime(date_str, "%Y-%m-%d").date()
        start = (d - timedelta(days=7)).isoformat()
        end   = (d + timedelta(days=3)).isoformat()
        hist  = yf.Ticker(ticker).history(start=start, end=end)
        return float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        return None


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def tag_html(action: str) -> str:
    m = {"NEW": ("tag-new","🆕 新仓"),
         "ADD": ("tag-add","▲ 加仓"),
         "TRIM":("tag-trim","▼ 减仓"),
         "EXIT":("tag-exit","❌ 清仓")}
    cls, label = m.get(action, ("","─"))
    return f'<span class="tag {cls}">{label}</span>'

def bar_html(pct: float, max_pct: float = 50, color: str = "#3b82f6") -> str:
    w = min(100, pct / max_pct * 100) if max_pct else 0
    return (f'<div class="bar-bg"><div class="bar-fg" '
            f'style="width:{w:.0f}%;background:{color};"></div></div>')

_MANUAL_TK: dict[str, str] = {
    "G3643J108": "FLUT",
    "M7S64H106": "MNDY",
    "G54050102": "IHG",
    "N07059210": "ASML",
    "G20567101": "CRH",
    "G0750C108": "ARGX",
    "G8T5AN108": "SPOT",
}

def get_ticker(cusip: str, ct: dict) -> str:
    t = _MANUAL_TK.get(cusip) or ct.get(cusip, "")
    if not t or len(t) > 10 or any(c in t for c in "/* "):
        return ""
    return t


# ── 顶部指标 ──────────────────────────────────────────────────────────────────

def compute_kpis(df: pd.DataFrame, sc: dict, quarters: list[str]):
    n_funds = df["cik"].nunique()
    if len(quarters) < 2:
        return n_funds, ("—", 0), ("—", 0), ("—", 0)

    cq, pq = quarters[-1], quarters[-2]
    curr = df[df["quarter"] == cq].copy()
    prev = df[df["quarter"] == pq].copy()

    curr["sector"] = curr["cusip"].map(lambda c: sc.get(c, "Other"))
    by_sec = (curr[curr["sector"] != "Other"]
              .groupby("sector")["cik"].nunique()
              .sort_values(ascending=False))
    top_sec = (by_sec.index[0], int(by_sec.iloc[0])) if len(by_sec) else ("—", 0)

    prev_pairs = set(zip(prev["cusip"], prev["cik"]))
    curr_pairs = set(zip(curr["cusip"], curr["cik"]))
    new_map: dict[str, set] = defaultdict(set)
    for cusip, cik in (curr_pairs - prev_pairs):
        new_map[cusip].add(cik)
    if new_map:
        tc  = max(new_map, key=lambda c: len(new_map[c]))
        row = curr[curr["cusip"] == tc]
        iss = str(row["issuer"].iloc[0]).strip()[:24] if not row.empty else tc
        top_new = (iss, len(new_map[tc]))
    else:
        top_new = ("—", 0)

    exit_map: dict[str, set] = defaultdict(set)
    for cusip, cik in (prev_pairs - curr_pairs):
        exit_map[cusip].add(cik)
    if exit_map:
        tc  = max(exit_map, key=lambda c: len(exit_map[c]))
        row = prev[prev["cusip"] == tc]
        iss = str(row["issuer"].iloc[0]).strip()[:24] if not row.empty else tc
        top_exit = (iss, len(exit_map[tc]))
    else:
        top_exit = ("—", 0)

    return n_funds, top_sec, top_new, top_exit


# ── Tab 3: 行业聚焦 ───────────────────────────────────────────────────────────

def tab_sector(df: pd.DataFrame, sc: dict, quarters: list[str], ct: dict | None = None):
    df = df.copy()
    df["sector"] = df["cusip"].map(lambda c: sc.get(c, "Other"))

    display_qs = [q for q in quarters if q != "2025Q1"]
    agg = (df[df["quarter"].isin(display_qs) & (df["sector"] != "Other")]
           .groupby(["quarter", "sector"])["cik"].nunique()
           .reset_index(name="fund_count"))

    valid = agg.groupby("sector")["fund_count"].max()
    valid = valid[valid >= 3].index.tolist()
    agg   = agg[agg["sector"].isin(valid)]

    st.markdown("#### 各行业持有基金数")

    colors = Q_COLORS[-len(display_qs):]
    fig = px.bar(
        agg, x="sector", y="fund_count", color="quarter",
        barmode="group",
        color_discrete_sequence=colors,
        labels={"fund_count":"持有基金数", "sector":"行业", "quarter":"季度"},
    )
    fig.update_layout(
        **LIGHT_PLOTLY,
        height=360,
        legend=dict(orientation="h", y=1.06, x=0,
                    font=dict(size=12, color="#6b7280")),
        bargap=0.25, bargroupgap=0.05,
    )
    fig.update_xaxes(showgrid=False, tickfont=dict(color="#6b7280", size=12),
                     linecolor="#e5e7eb")
    fig.update_yaxes(gridcolor="#f3f4f6", tickfont=dict(color="#6b7280", size=12),
                     linecolor="#e5e7eb", title_text="持有基金数",
                     title_font=dict(color="#9ca3af", size=11))
    fig.update_traces(marker_line_width=0)
    st.plotly_chart(fig, use_container_width=True, key="sector_bar_chart")

    st.markdown("#### 行业明细")
    pivot = (agg.pivot(index="sector", columns="quarter", values="fund_count")
               .reindex(columns=display_qs).fillna(0).astype(int))

    def trend_arrow(row):
        v = [int(row.get(q, 0)) for q in display_qs]
        if len(v) < 2: return "─"
        d = v[-1] - v[-2]
        return "↑↑" if d >= 3 else "↑" if d > 0 else "↓↓" if d <= -3 else "↓" if d < 0 else "─"

    def chg_str(row):
        v = [int(row.get(q, 0)) for q in display_qs]
        if len(v) < 2: return "─"
        d = v[-1] - v[-2]
        return f"+{d}" if d > 0 else str(d)

    pivot["趋势"]  = pivot.apply(trend_arrow, axis=1)
    pivot["较上季"] = pivot.apply(chg_str, axis=1)
    pivot = pivot.reset_index().rename(columns={"sector": "行业"})
    pivot = pivot.sort_values(display_qs[-1], ascending=False)

    header_cols = ["行业"] + display_qs + ["趋势", "较上季"]
    th = "".join(f"<th>{c}</th>" for c in header_cols)
    rows_html = ""
    for i, (_, row) in enumerate(pivot.iterrows()):
        bg = "#ffffff" if i % 2 == 0 else "#f9fafb"
        td = f'<td style="color:#111827;font-weight:500;">{_html.escape(str(row["行业"]))}</td>'
        for q in display_qs:
            v = int(row.get(q, 0))
            c = "#3b82f6" if v >= 10 else "#374151"
            td += f'<td style="color:{c};font-weight:{"600" if v>=10 else "400"};">{v}</td>'
        trend_c = "#16a34a" if "↑" in row["趋势"] else "#dc2626" if "↓" in row["趋势"] else "#9ca3af"
        chg_c   = "#16a34a" if "+" in row["较上季"] else "#dc2626" if "-" in row["较上季"] else "#9ca3af"
        td += (f'<td style="color:{trend_c};font-weight:600;">{row["趋势"]}</td>'
               f'<td style="color:{chg_c};font-weight:600;">{row["较上季"]}</td>')
        rows_html += f'<tr style="background:{bg};">{td}</tr>'

    st.markdown(f"""<table style="width:100%;border-collapse:collapse;background:#fff;
            border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;font-size:13.5px;">
        <thead><tr style="background:#f3f4f6;">{th}</tr></thead>
        <tbody>{rows_html}</tbody>
    </table>""", unsafe_allow_html=True)

    if ct and quarters:
        render_consensus_block(df, ct, quarters[-1])


# ── 群体共识 ──────────────────────────────────────────────────────────────────

def compute_consensus(df: pd.DataFrame, ct: dict, latest_q: str) -> tuple[pd.DataFrame, dict]:
    curr = df[df["quarter"] == latest_q].copy()
    if curr.empty:
        return pd.DataFrame(), {}

    total_funds = curr["cik"].nunique()

    grp = (curr.groupby("cusip")
           .agg(issuer=("issuer", "first"),
                n_funds=("cik", "nunique"),
                total_pct=("pct", "sum"))
           .reset_index())
    grp["ticker"] = grp["cusip"].map(lambda c: get_ticker(c, ct))

    top3_map: dict[str, list] = {}
    for cusip, sub in curr.sort_values("pct", ascending=False).groupby("cusip"):
        top3_map[cusip] = sub.head(3)[["fund_name", "pct"]].values.tolist()
    grp["top3_funds"] = grp["cusip"].map(top3_map)
    grp["coverage"]   = (grp["n_funds"] / total_funds * 100).round(0).astype(int)

    all_map = dict(zip(grp["cusip"], grp["n_funds"]))
    top20 = (grp[grp["n_funds"] >= 3]
             .sort_values(["n_funds", "total_pct"], ascending=False)
             .head(20)
             .reset_index(drop=True))
    return top20, all_map


def _consensus_badge_html(n: int) -> str:
    if n >= 8:
        return f'<span class="badge-s4">★★★★ {n}家</span>'
    elif n >= 6:
        return f'<span class="badge-s3">★★★ {n}家</span>'
    elif n >= 4:
        return f'<span class="badge-s2">★★ {n}家</span>'
    else:
        return f'<span class="badge-s1">★ {n}家</span>'


def render_consensus_block(df: pd.DataFrame, ct: dict, latest_q: str):
    top20, _ = compute_consensus(df, ct, latest_q)
    if top20.empty:
        return

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("#### 🤝 群体共识 Top 20")
    st.caption(f"基于 {latest_q} 最新季度 · 同时持有家数 ≥ 3 家的股票")

    rows_html = ""
    for i, (_, r) in enumerate(top20.iterrows()):
        bg     = "#ffffff" if i % 2 == 0 else "#f9fafb"
        ticker = r["ticker"] or "—"
        issuer = _html.escape(str(r["issuer"]).strip()[:32])
        n      = int(r["n_funds"])
        cov    = f"{r['coverage']}%"
        badge  = _consensus_badge_html(n)
        top3   = r.get("top3_funds") or []
        top3_str = "&nbsp;&nbsp;".join(
            f'<span style="color:#374151;">{_html.escape(fn[:14])}</span>'
            f'<span style="color:#9ca3af;font-size:11px;">({p:.1f}%)</span>'
            for fn, p in top3[:3]
        )
        rows_html += f"""<tr style="background:{bg};">
          <td style="color:#2563eb;font-weight:600;width:72px;">{ticker}</td>
          <td style="color:#374151;max-width:200px;">{issuer}</td>
          <td style="text-align:center;width:120px;">{badge}</td>
          <td style="text-align:center;width:64px;color:#6b7280;">{cov}</td>
          <td style="font-size:12px;">{top3_str}</td>
        </tr>"""

    st.markdown(f"""<table class="sig-table">
      <thead><tr>
        <th>Ticker</th><th>公司名</th><th>共识等级</th><th>覆盖率</th><th>主要基金（前3家）</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>""", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)


# ── Tab 2: 本季信号 ───────────────────────────────────────────────────────────

def classify(curr_pct, prev_pct, curr_sh, prev_sh) -> str:
    if prev_sh == 0:
        return "NEW" if curr_sh > 0 else "HOLD"
    if curr_sh == 0:
        return "EXIT"
    d = curr_pct - prev_pct
    return "ADD" if d >= 2 else "TRIM" if d <= -2 else "HOLD"


def _build_per_fund_signals(df: pd.DataFrame, cq: str, pq: str) -> pd.DataFrame:
    """Per-fund, per-cusip NEW/ADD signals (not aggregated across funds)."""
    curr = df[df["quarter"] == cq]
    prev = df[df["quarter"] == pq]

    c_idx = curr.set_index(["cusip", "cik"])[["pct", "shares", "issuer", "fund_name", "period"]]
    p_idx = prev.set_index(["cusip", "cik"])[["pct", "shares"]]

    merged = c_idx.join(p_idx, how="left", rsuffix="_p").reset_index()
    merged[["pct_p", "shares_p"]] = merged[["pct_p", "shares_p"]].fillna(0)
    merged[["pct", "shares"]]     = merged[["pct", "shares"]].fillna(0)

    prev_iss = prev.drop_duplicates("cusip").set_index("cusip")["issuer"]
    def _iss(row):
        v = row["issuer"]
        s = str(v).strip() if (v is not None and str(v).lower() != "nan") else ""
        return s if s else str(prev_iss.get(row["cusip"], "—")).strip()
    merged["issuer"] = merged.apply(_iss, axis=1)

    merged["action"] = merged.apply(
        lambda r: classify(r["pct"], r["pct_p"],
                           int(r["shares"]), int(r["shares_p"])), axis=1)
    return merged[merged["action"].isin(["NEW", "ADD"])].copy()


def _render_multi_new(df: pd.DataFrame, ct: dict, cq: str, pq: str):
    """多家同时新建仓 block."""
    curr = df[df["quarter"] == cq]
    prev = df[df["quarter"] == pq]
    if curr.empty or prev.empty:
        return

    prev_pairs = set(zip(prev["cusip"], prev["cik"]))
    new_rows = []
    for _, r in curr.iterrows():
        if (r["cusip"], r["cik"]) not in prev_pairs:
            new_rows.append({
                "cusip":     r["cusip"],
                "fund_name": r["fund_name"],
                "pct":       float(r["pct"]),
                "value_m":   float(r["value"]) / 1000,
                "issuer":    str(r["issuer"]).strip(),
            })

    if not new_rows:
        return

    new_df = pd.DataFrame(new_rows)
    new_df["ticker"] = new_df["cusip"].map(lambda c: get_ticker(c, ct))

    by_cusip = (new_df.groupby("cusip")
                .agg(ticker    =("ticker",    "first"),
                     issuer    =("issuer",    "first"),
                     n_funds   =("fund_name", "count"),
                     total_vm  =("value_m",   "sum"),
                     funds_list=("fund_name", list),
                     pcts_list =("pct",       list))
                .reset_index()
                .sort_values("n_funds", ascending=False))

    multi = by_cusip[by_cusip["n_funds"] > 1]
    if multi.empty:
        return

    st.markdown(f'<div class="alert-title">🔥 多家同时新建仓（{len(multi)} 只）</div>',
                unsafe_allow_html=True)

    rows_m = ""
    for i, (_, r) in enumerate(multi.iterrows()):
        bg  = "#ffffff" if i % 2 == 0 else "#f9fafb"
        tk  = r["ticker"] or r["cusip"][:8]
        iss = _html.escape(str(r["issuer"]).strip()[:28])
        pairs = list(zip(r["funds_list"], r["pcts_list"]))
        pairs_str = "&nbsp;&nbsp;".join(
            f'<b>{_html.escape(fn[:14])}</b>({p:.1f}%)'
            for fn, p in sorted(pairs, key=lambda x: -x[1])[:4]
        )
        rows_m += f"""<tr style="background:{bg};">
          <td style="color:#2563eb;font-weight:700;width:72px;">{tk}</td>
          <td style="color:#374151;max-width:200px;">{iss}</td>
          <td style="text-align:center;width:72px;">
            <span style="background:#fee2e2;color:#dc2626;padding:2px 10px;
              border-radius:12px;font-size:12px;font-weight:700;">{int(r['n_funds'])} 家</span></td>
          <td style="text-align:right;width:100px;color:#374151;">${r['total_vm']:,.0f}M</td>
          <td style="font-size:12px;">{pairs_str}</td>
        </tr>"""

    st.markdown(f"""<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:12px;">
      <thead><tr style="background:#fef2f2;color:#6b7280;font-size:11px;text-transform:uppercase;">
        <th style="padding:8px 12px;">Ticker</th><th style="padding:8px 12px;">公司名</th>
        <th style="padding:8px 8px;text-align:center;">新建仓家数</th>
        <th style="padding:8px 8px;text-align:right;">合计市值</th>
        <th style="padding:8px 12px;">基金列表（+占比）</th>
      </tr></thead><tbody>{rows_m}</tbody></table>""", unsafe_allow_html=True)


def _price_cell(price: float | None) -> str:
    if price is None:
        return '<td style="text-align:right;padding:8px 8px;color:#9ca3af;">—</td>'
    return f'<td style="text-align:right;padding:8px 8px;color:#374151;">${price:.1f}</td>'


def _gain_cell(p_now: float | None, p_filed: float | None) -> str:
    if p_now is None or p_filed is None or p_filed == 0:
        return '<td style="text-align:right;padding:8px 8px;color:#9ca3af;">—</td>'
    chg = (p_now - p_filed) / p_filed * 100
    if chg > 20:
        color, emoji = "#dc2626", "🔴"
    elif chg >= 0:
        color, emoji = "#d97706", "🟡"
    elif chg >= -10:
        color, emoji = "#16a34a", "🟢"
    else:
        color, emoji = "#2563eb", "🔵"
    return (f'<td style="text-align:right;padding:8px 8px;'
            f'color:{color};font-weight:700;">{emoji} {chg:+.1f}%</td>')


def _render_recommend_signals(df: pd.DataFrame, ct: dict, cq: str, pq: str,
                               bt_fund_df: pd.DataFrame | None):
    """推荐基金新建仓/加仓，按推荐度分组。"""
    if bt_fund_df is None or bt_fund_df.empty:
        st.caption("推荐基金信号：请先运行回测 `python backtest.py --all`")
        return

    per_fund = _build_per_fund_signals(df, cq, pq)
    if per_fund.empty:
        return

    # 优先用缓存 ticker，缺失时用 yf.Search 反查
    def resolve_ticker(row):
        tk = get_ticker(row["cusip"], ct)
        if tk:
            return tk
        return search_ticker_by_name(str(row["issuer"]).strip())

    per_fund["ticker"] = per_fund.apply(resolve_ticker, axis=1)

    rec_map = {str(fn).strip(): str(rec).strip()
               for fn, rec in zip(bt_fund_df["fund_name"], bt_fund_df["recommend"])}

    filing_dates = load_filing_dates()

    # 仓位过滤 slider
    min_pct = st.slider(
        "最小仓位过滤（过滤掉试探性小仓位）",
        min_value=0.0, max_value=3.0, value=0.5, step=0.1,
        format="%.1f%%",
        key="signal_min_pct",
        help="只显示仓位 ≥ 该值的信号，排除试探性建仓"
    )

    any_rendered = False
    for g_idx, (icon, rec_label, allowed_actions) in enumerate(RECOMMEND_GROUPS):
        group_funds = [fn for fn, rec in rec_map.items() if rec == rec_label]
        if not group_funds:
            continue

        rows = per_fund[
            per_fund["fund_name"].isin(group_funds) &
            per_fund["action"].isin(allowed_actions) &
            (per_fund["pct"] >= min_pct)
        ].sort_values(["fund_name", "pct"], ascending=[True, False])

        if rows.empty:
            continue

        action_desc = "NEW 和 ADD 信号"
        st.markdown(
            f'<div style="font-weight:700;font-size:14px;color:#111827;'
            f'margin:18px 0 8px 0;">{icon} {rec_label} — {action_desc}</div>',
            unsafe_allow_html=True)

        # ── 表头（数据列 + 走势按钮列）──────────────────────────────────────
        _RS = ("display:flex;align-items:center;font-size:13px;"
               "border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;"
               "border-bottom:1px solid #f3f4f6;")
        _F  = ("flex:{f};padding:6px {p}px;overflow:hidden;"
               "white-space:nowrap;text-overflow:ellipsis;")
        def _c(f, p=8): return _F.format(f=f, p=p)

        hdr_data, hdr_btn = st.columns([11, 1])
        with hdr_data:
            st.markdown(f"""<div style="{_RS}background:#f3f4f6;
                    border-top:1px solid #e5e7eb;border-radius:10px 10px 0 0;
                    font-size:11px;color:#6b7280;text-transform:uppercase;
                    font-weight:600;letter-spacing:.4px;">
              <span style="{_c(2.5,14)}">基金</span>
              <span style="{_c(0.9)}">Ticker</span>
              <span style="{_c(0.8)}text-align:right;">当前仓位</span>
              <span style="{_c(1.0)}">变化幅度</span>
              <span style="{_c(1.1)}">信号类型</span>
              <span style="{_c(0.9)}text-align:right;">季末价</span>
              <span style="{_c(0.9)}text-align:right;">披露日价</span>
              <span style="{_c(0.9)}text-align:right;">今日价</span>
              <span style="{_c(1.1)}text-align:right;">披露后涨幅</span>
            </div>""", unsafe_allow_html=True)
        with hdr_btn:
            st.markdown(
                '<div style="font-size:11px;color:#6b7280;text-transform:uppercase;'
                'font-weight:600;letter-spacing:.4px;padding:10px 4px;text-align:center;">'
                '走势</div>', unsafe_allow_html=True)

        # ── 数据行 ────────────────────────────────────────────────────────────
        for i, (_, r) in enumerate(rows.iterrows()):
            bg  = "#ffffff" if i % 2 == 0 else "#f9fafb"
            tk  = r["ticker"] or "—"
            tk_style = "color:#2563eb;font-weight:700;" if tk != "—" else "color:#9ca3af;"
            fn  = _html.escape(str(r["fund_name"])[:28])
            fn_safe = "".join(c for c in str(r["fund_name"]) if c.isalnum())[:12]

            # 变化幅度
            if r["action"] == "NEW":
                chg_html = f'<span style="color:#2563eb;font-weight:700;">+{r["pct"]:.1f}%</span>'
            else:
                delta = float(r["pct"]) - float(r.get("pct_p", 0) or 0)
                chg_c = "#065f46" if delta >= 5 else "#16a34a" if delta >= 3 else "#4ade80"
                chg_html = f'<span style="color:{chg_c};font-weight:700;">+{delta:.1f}%</span>'

            # 三价格
            period_date = str(r.get("period", "") or "")
            filed_date  = filing_dates.get((str(r["cik"]), cq), None)
            if tk and tk != "—":
                p_end   = fetch_hist(tk, period_date) if period_date else None
                p_filed = fetch_hist(tk, filed_date)  if filed_date  else None
                p_now   = fetch_now(tk)
            else:
                p_end = p_filed = p_now = None

            def _pspan(p, col=0.9):
                if p is None:
                    return f'<span style="{_c(col)}text-align:right;color:#9ca3af;">—</span>'
                return f'<span style="{_c(col)}text-align:right;color:#374151;">${p:.1f}</span>'

            def _gspan(pn, pf):
                if pn is None or pf is None or pf == 0:
                    return f'<span style="{_c(1.1)}text-align:right;color:#9ca3af;">—</span>'
                chg = (pn - pf) / pf * 100
                if chg > 20:     color, emoji = "#dc2626", "🔴"
                elif chg >= 0:   color, emoji = "#d97706", "🟡"
                elif chg >= -10: color, emoji = "#16a34a", "🟢"
                else:            color, emoji = "#2563eb", "🔵"
                return (f'<span style="{_c(1.1)}text-align:right;'
                        f'color:{color};font-weight:700;">{emoji} {chg:+.1f}%</span>')

            # 行 HTML + toggle 放在同一行的两列中
            row_data, row_btn = st.columns([11, 1])
            with row_data:
                st.markdown(f"""<div style="{_RS}background:{bg};">
                  <span style="{_c(2.5,14)}font-weight:600;color:#374151;">{fn}</span>
                  <span style="{_c(0.9)}{tk_style}">{tk}</span>
                  <span style="{_c(0.8)}text-align:right;color:#374151;">{r['pct']:.1f}%</span>
                  <span style="{_c(1.0)}">{chg_html}</span>
                  <span style="{_c(1.1)}">{tag_html(r['action'])}</span>
                  {_pspan(p_end)}{_pspan(p_filed)}{_pspan(p_now)}{_gspan(p_now, p_filed)}
                </div>""", unsafe_allow_html=True)
            with row_btn:
                toggle_key = f"sig_t_{g_idx}_{i}_{tk}_{fn_safe}"
                show_chart = st.toggle("", key=toggle_key,
                                       label_visibility="collapsed",
                                       disabled=(tk == "—"))

            # ── 价格走势图（toggle 打开时显示）────────────────────────────────
            if show_chart and tk and tk != "—":
                hist = fetch_history_6mo(tk)
                with st.container():
                    if hist.empty:
                        st.caption(f"  {tk} 价格数据不可用")
                    else:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(
                            x=hist["Date"], y=hist["Close"],
                            mode="lines",
                            line=dict(color="#3b82f6", width=2),
                            fill="tonexty", fillcolor="rgba(59,130,246,0.06)",
                        ))
                        if period_date:
                            fig.add_vline(x=period_date,
                                          line_color="#f59e0b", line_width=1.5,
                                          line_dash="dash",
                                          annotation_text="季末建仓",
                                          annotation_font_color="#f59e0b",
                                          annotation_position="top left")
                        if filed_date:
                            fig.add_vline(x=filed_date,
                                          line_color="#3b82f6", line_width=1.5,
                                          line_dash="dot",
                                          annotation_text="披露日",
                                          annotation_font_color="#3b82f6",
                                          annotation_position="top right")
                        fig.add_scatter(
                            x=[hist["Date"].iloc[-1]],
                            y=[hist["Close"].iloc[-1]],
                            mode="markers+text",
                            marker=dict(size=9, color="#dc2626",
                                        line=dict(width=2, color="#ffffff")),
                            text=[f"  今日 ${hist['Close'].iloc[-1]:.1f}"],
                            textposition="middle right",
                            textfont=dict(size=11, color="#dc2626"),
                            showlegend=False,
                        )
                        y_min = hist["Close"].min() * 0.95
                        y_max = hist["Close"].max() * 1.05
                        fig.update_layout(
                            **LIGHT_PLOTLY, height=200, showlegend=False,
                            yaxis=dict(gridcolor="#f3f4f6", tickprefix="$",
                                       tickfont=dict(color="#6b7280", size=11),
                                       range=[y_min, y_max]),
                            xaxis=dict(showgrid=False, linecolor="#e5e7eb",
                                       tickfont=dict(color="#6b7280", size=11)),
                        )
                        chart_key = f"sig_chart_{g_idx}_{i}_{tk}_{fn_safe}"
                        st.plotly_chart(fig, use_container_width=True, key=chart_key)

                        kc1, kc2, kc3 = st.columns(3)
                        with kc1:
                            st.metric("季末价", f"${p_end:.1f}" if p_end else "—")
                        with kc2:
                            st.metric("披露日价", f"${p_filed:.1f}" if p_filed else "—")
                        with kc3:
                            if p_now and p_filed and p_filed > 0:
                                chg_v = (p_now - p_filed) / p_filed * 100
                                st.metric("今日价", f"${p_now:.1f}",
                                          delta=f"{chg_v:+.1f}% 披露后")
                            else:
                                st.metric("今日价", f"${p_now:.1f}" if p_now else "—")

        any_rendered = True

    if not any_rendered:
        st.caption(f"本季推荐基金暂无仓位 ≥ {min_pct:.1f}% 的信号（可降低滑块阈值）")


def tab_signals(df: pd.DataFrame, ct: dict, quarters: list[str],
                bt_fund_df: pd.DataFrame | None = None):
    if len(quarters) < 2:
        st.info("需要至少 2 季数据")
        return

    cq, pq = quarters[-1], quarters[-2]

    # ── Block 1: 多家同时新建仓 ────────────────────────────────────────────────
    st.markdown('<div class="alert-block">', unsafe_allow_html=True)
    _render_multi_new(df, ct, cq, pq)
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Block 2: 推荐基金信号（按推荐度分组）─────────────────────────────────
    st.markdown("#### 📋 推荐基金本季新建仓 / 加仓")
    _render_recommend_signals(df, ct, cq, pq, bt_fund_df)


# ── Tab 4: 逻辑验证 ───────────────────────────────────────────────────────────

def tab_thesis(df: pd.DataFrame, ct: dict, quarters: list[str]):
    tk_to_cusips: dict[str, list] = defaultdict(list)
    for cusip, tk in ct.items():
        if tk:
            tk_to_cusips[tk.upper()].append(cusip)

    user_tk = st.text_input(
        "股票代码", placeholder="输入 Ticker（如 NVDA、META、GE）或公司名关键词",
    ).strip().upper()

    if not user_tk:
        st.markdown('<p style="color:#9ca3af;text-align:center;margin-top:48px;">'
                    '输入股票代码查看基金池持仓历史与 Thesis 验证</p>',
                    unsafe_allow_html=True)
        return

    cusips = tk_to_cusips.get(user_tk, [])
    if not cusips:
        hits   = df[df["issuer"].str.contains(user_tk, case=False, na=False)]["cusip"].unique()
        cusips = list(hits[:5])

    if not cusips:
        st.error(f"未找到 **{user_tk}** 的持仓记录")
        return

    stock = df[df["cusip"].isin(cusips)].copy()
    if stock.empty:
        st.error(f"基金池中无 {user_tk} 持仓")
        return

    issuer  = _html.escape(str(stock["issuer"].iloc[0]).strip())
    ticker  = get_ticker(cusips[0], ct) or user_tk
    p_now   = fetch_now(ticker)

    earliest_q = stock["quarter"].min()
    early      = stock[stock["quarter"] == earliest_q]
    tv = float(early["value"].sum()) * 1000
    ts = float(early["shares"].sum())
    avg_cost = tv / ts if ts > 0 else None

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("今日价格", f"${p_now:.2f}" if p_now else "—")
    with c2:
        st.metric("池内估算平均成本",
                  f"${avg_cost:.2f}" if avg_cost else "—",
                  help=f"基于最早持仓季 ({earliest_q}) value÷shares 估算")
    with c3:
        if p_now and avg_cost and avg_cost > 0:
            chg = (p_now - avg_cost) / avg_cost * 100
            st.metric("自平均成本涨幅", f"{chg:+.1f}%")
        else:
            st.metric("自平均成本涨幅", "—")

    st.markdown(f"**{issuer}** &nbsp;&nbsp;`{ticker}`")
    st.markdown("<hr>", unsafe_allow_html=True)

    hold = (stock.groupby("quarter")["cik"].nunique()
                 .reindex(quarters, fill_value=0).reset_index())
    hold.columns = ["quarter", "count"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hold["quarter"], y=hold["count"],
        mode="lines+markers+text",
        text=hold["count"], textposition="top center",
        textfont=dict(color="#374151", size=12),
        line=dict(color="#3b82f6", width=2.5),
        marker=dict(size=10, color="#3b82f6",
                    line=dict(width=2, color="#ffffff")),
        fill="tozeroy", fillcolor="rgba(59,130,246,0.06)",
    ))
    ymax = max(int(hold["count"].max()) + 2, 3)
    fig.update_layout(
        **LIGHT_PLOTLY, height=240, showlegend=False,
        yaxis=dict(gridcolor="#f3f4f6", range=[0, ymax], dtick=1,
                   tickfont=dict(color="#9ca3af", size=11),
                   title=dict(text="持有基金数", font=dict(color="#9ca3af", size=11))),
        xaxis=dict(showgrid=False, linecolor="#e5e7eb",
                   tickfont=dict(color="#6b7280", size=12)),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"thesis_hold_{ticker}")

    st.markdown("#### 各基金持仓占比 (%)")
    pivot = (stock.pivot_table(index="fund_name", columns="quarter",
                               values="pct", aggfunc="sum")
                  .reindex(columns=quarters, fill_value=0)
                  .fillna(0).round(2))
    pivot = pivot[(pivot > 0).any(axis=1)].sort_values(quarters[-1], ascending=False)
    pivot = pivot.reset_index().rename(columns={"fund_name": "基金"})
    st.dataframe(pivot, use_container_width=True, hide_index=True)

    if p_now and avg_cost and avg_cost > 0:
        chg = (p_now - avg_cost) / avg_cost * 100
        if chg > 50:
            st.markdown(f"""<div class="verdict-box verdict-warn">
                <div class="verdict-title" style="color:#d97706;">⚠️ 估值警告</div>
                <div class="verdict-body">相较池内估算平均成本已上涨 <strong>{chg:.1f}%</strong>，
                请重新评估当前估值是否已 priced in。机构买入价与今日价差距显著。</div>
            </div>""", unsafe_allow_html=True)
        elif chg >= 0:
            st.markdown(f"""<div class="verdict-box verdict-ok">
                <div class="verdict-title" style="color:#16a34a;">✅ Thesis 进行中</div>
                <div class="verdict-body">相较估算平均成本上涨 <strong>{chg:.1f}%</strong>，
                在合理区间。继续跟踪基本面以验证机构买入逻辑。</div>
            </div>""", unsafe_allow_html=True)
        else:
            st.markdown(f"""<div class="verdict-box verdict-below">
                <div class="verdict-title" style="color:#2563eb;">📊 低于平均成本</div>
                <div class="verdict-body">当前价低于估算平均成本 <strong>{abs(chg):.1f}%</strong>，
                需验证 thesis 是否仍然成立，或评估是否为加仓机会。</div>
            </div>""", unsafe_allow_html=True)


# ── Tab 1: 回测分析 ───────────────────────────────────────────────────────────

BT_DB = Path("backtest.db")

RECOMMEND_STYLE = {
    "★★★★ 强烈推荐":     ("background:#f0fdf4; color:#16a34a; border:1px solid #bbf7d0;",  "🟢"),
    "★★★  眼光准需快速执行": ("background:#eff6ff; color:#2563eb; border:1px solid #bfdbfe;", "🔵"),
    "★★   选择性跟随":    ("background:#fffbeb; color:#d97706; border:1px solid #fde68a;",  "🟡"),
    "★    不建议":        ("background:#f9fafb; color:#9ca3af; border:1px solid #e5e7eb;",  "⚪"),
}

@st.cache_data(ttl=1800)
def load_bt_fund_results() -> pd.DataFrame:
    if not BT_DB.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(BT_DB))
    try:
        df = pd.read_sql("SELECT * FROM bt_fund_results ORDER BY end_alpha_1y DESC NULLS LAST", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df

@st.cache_data(ttl=1800)
def load_bt_quarter_results(cik: str) -> pd.DataFrame:
    if not BT_DB.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(BT_DB))
    try:
        df = pd.read_sql(
            "SELECT * FROM bt_quarter_results WHERE cik=? ORDER BY quarter",
            conn, params=(cik,))
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


def tab_backtest():
    if not BT_DB.exists():
        st.info("backtest.db 不存在，请先运行：`python backtest.py --all`")
        return

    fund_df = load_bt_fund_results()
    if fund_df.empty:
        st.info("回测结果为空，请先运行：`python backtest.py --all`")
        return

    # ── 基金排行榜 ────────────────────────────────────────────────────────────
    st.markdown("#### 基金回测排行榜 — 季末 1Y Alpha 降序")
    st.caption("信号 = NEW + ADD 主动加仓信号数；Alpha = 相对标普500的超额回报；一致性 = 正Alpha季度占比")

    def rec_badge(rec_str):
        rec_str = str(rec_str).strip() if rec_str else "★    不建议"
        style, icon = RECOMMEND_STYLE.get(rec_str,
            ("background:#f9fafb;color:#9ca3af;border:1px solid #e5e7eb;", "⚪"))
        return f'<span style="padding:2px 10px;border-radius:20px;font-size:12px;{style}">{icon} {rec_str}</span>'

    rows_html = ""
    for i, (_, r) in enumerate(fund_df.iterrows()):
        bg = "#ffffff" if i % 2 == 0 else "#f9fafb"

        def fv(v, suf="%"):
            if v is None or (hasattr(v, '__class__') and str(v) == 'nan'): return "—"
            try:
                flt = float(v)
                return f"{flt:+.1f}{suf}"
            except: return "—"

        def fh(v):
            if v is None or str(v) == 'nan': return "—"
            try: return f"{float(v):.0f}%"
            except: return "—"

        a1y_val  = float(r.get("end_alpha_1y") or 0)
        a1y_c    = "#16a34a" if a1y_val > 0 else "#dc2626"
        best_sig = str(r.get("best_signal","—"))
        rec_html = rec_badge(r.get("recommend",""))
        con_val  = r.get("consistency")
        con_str  = f"{float(con_val)*100:.0f}%" if con_val and str(con_val) != 'nan' else "—"

        rows_html += f"""<tr style="background:{bg};">
          <td style="font-weight:600;color:#111827;">{r['fund_name']}</td>
          <td style="text-align:center;">{int(r.get('n_signals') or 0)}</td>
          <td style="text-align:center;">{fh(r.get('end_hit_6m'))}</td>
          <td style="text-align:center;color:{'#16a34a' if float(r.get('end_alpha_6m') or 0)>0 else '#dc2626'};">{fv(r.get('end_alpha_6m'))}</td>
          <td style="text-align:center;">{fh(r.get('end_hit_1y'))}</td>
          <td style="text-align:center;font-weight:700;color:{a1y_c};">{fv(r.get('end_alpha_1y'))}</td>
          <td style="text-align:center;">{con_str}</td>
          <td style="text-align:center;font-weight:600;color:#2563eb;">跟{best_sig}</td>
          <td>{rec_html}</td>
        </tr>"""

    st.markdown(f"""<table style="width:100%;border-collapse:collapse;background:#fff;
            border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;font-size:13px;">
      <thead><tr style="background:#f3f4f6;color:#6b7280;font-size:11px;text-transform:uppercase;">
        <th style="padding:10px 14px;text-align:left;">基金</th>
        <th style="padding:10px 8px;">信号数</th>
        <th style="padding:10px 8px;">6M胜率</th>
        <th style="padding:10px 8px;">6M超额</th>
        <th style="padding:10px 8px;">1Y胜率</th>
        <th style="padding:10px 8px;">1Y超额</th>
        <th style="padding:10px 8px;">一致性</th>
        <th style="padding:10px 8px;">最优信号</th>
        <th style="padding:10px 14px;text-align:left;">推荐度</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>""", unsafe_allow_html=True)
    st.markdown('<p class="footnote">* 超额收益 = 相对标普500的超额回报</p>',
                unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── 选基金看详情 ──────────────────────────────────────────────────────────
    fund_names = fund_df["fund_name"].tolist()
    fund_ciks  = dict(zip(fund_df["fund_name"], fund_df["cik"]))

    selected = st.selectbox("选择基金查看季度明细", fund_names,
                            key="backtest_fund_selector",
                            label_visibility="collapsed")

    if not selected:
        return

    cik      = fund_ciks.get(selected, "")
    q_df     = load_bt_quarter_results(cik)
    fund_row = fund_df[fund_df["fund_name"] == selected].iloc[0]

    col_info_spacer, col_info = st.columns([2, 5])
    with col_info:
        m1, m2, m3, m4 = st.columns(4)
        def safe_f(v, suf="%"):
            try: return f"{float(v):+.1f}{suf}"
            except: return "—"
        with m1: st.metric("1Y Alpha（季末）", safe_f(fund_row.get("end_alpha_1y")))
        with m2: st.metric("披露日 1Y Alpha",  safe_f(fund_row.get("fil_alpha_1y")))
        with m3: st.metric("滞后成本",          safe_f(fund_row.get("lag_cost")))
        with m4:
            con = fund_row.get("consistency")
            st.metric("一致性", f"{float(con)*100:.0f}%" if con and str(con)!='nan' else "—")

    if q_df.empty:
        st.info(f"{selected} 无季度数据")
        return

    st.markdown(f"#### {selected} — 季度明细")

    def color_cell(val):
        try:
            v = float(val)
            if v > 0:
                return f'<td style="text-align:center;color:#16a34a;font-weight:600;">{v:+.1f}%</td>'
            elif v < 0:
                return f'<td style="text-align:center;color:#dc2626;">{v:+.1f}%</td>'
            else:
                return f'<td style="text-align:center;color:#9ca3af;">0.0%</td>'
        except:
            return '<td style="text-align:center;color:#9ca3af;">—</td>'

    q_rows = ""
    for i, (_, r) in enumerate(q_df.iterrows()):
        bg = "#ffffff" if i % 2 == 0 else "#f9fafb"
        def fh2(v):
            try: return f"{float(v):.0f}%"
            except: return "—"
        q_rows += f"""<tr style="background:{bg};">
          <td style="padding:8px 14px;font-weight:600;color:#374151;">{r['quarter']}</td>
          <td style="padding:8px 8px;text-align:center;">{int(r.get('n_signals') or 0)}</td>
          <td style="padding:8px 8px;text-align:center;color:#2563eb;">{int(r.get('n_new') or 0)}</td>
          <td style="padding:8px 8px;text-align:center;color:#16a34a;">{int(r.get('n_add') or 0)}</td>
          {color_cell(r.get('end_alpha_6m'))}
          {color_cell(r.get('end_alpha_1y'))}
          <td style="padding:8px 8px;text-align:center;">{fh2(r.get('end_hit_6m'))}</td>
          <td style="padding:8px 8px;text-align:center;">{fh2(r.get('end_hit_1y'))}</td>
          {color_cell(r.get('fil_alpha_1y'))}
        </tr>"""

    st.markdown(f"""<table style="width:100%;border-collapse:collapse;background:#fff;
            border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;font-size:13px;">
      <thead><tr style="background:#f3f4f6;color:#6b7280;font-size:11px;text-transform:uppercase;">
        <th style="padding:10px 14px;text-align:left;">季度</th>
        <th style="padding:10px 8px;">总信号</th>
        <th style="padding:10px 8px;color:#2563eb;">新建仓</th>
        <th style="padding:10px 8px;color:#16a34a;">加仓</th>
        <th style="padding:10px 8px;">季末6M超额</th>
        <th style="padding:10px 8px;">季末1Y超额</th>
        <th style="padding:10px 8px;">6M胜率</th>
        <th style="padding:10px 8px;">1Y胜率</th>
        <th style="padding:10px 8px;">披露日1Y超额</th>
      </tr></thead>
      <tbody>{q_rows}</tbody>
    </table>""", unsafe_allow_html=True)
    st.markdown('<p class="footnote">* 超额收益 = 相对标普500的超额回报</p>',
                unsafe_allow_html=True)

    # ── Alpha 趋势折线图 ──────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(f"#### {selected} — 季末 1Y Alpha 趋势")

    plot_df = q_df.dropna(subset=["end_alpha_1y"]).copy()
    if plot_df.empty:
        st.caption("1Y Alpha 数据不足（需等待持仓满1年）")
        return

    plot_df["end_alpha_1y"] = plot_df["end_alpha_1y"].astype(float)

    fig = go.Figure()

    for _, row in plot_df.iterrows():
        v = row["end_alpha_1y"]
        fig.add_shape(type="rect",
            x0=row["quarter"], x1=row["quarter"],
            y0=0, y1=v,
            line=dict(width=0),
            fillcolor="#bbf7d0" if v > 0 else "#fecaca",
            opacity=0.3, layer="below")

    fig.add_trace(go.Scatter(
        x=plot_df["quarter"], y=plot_df["end_alpha_1y"],
        mode="lines+markers+text",
        text=[f"{v:+.1f}%" for v in plot_df["end_alpha_1y"]],
        textposition="top center",
        textfont=dict(size=11, color="#374151"),
        line=dict(color="#3b82f6", width=2.5),
        marker=dict(size=9, color=plot_df["end_alpha_1y"].apply(
            lambda v: "#16a34a" if v > 0 else "#dc2626"),
            line=dict(width=2, color="#ffffff")),
        name="季末1Y Alpha",
    ))

    fig.add_hline(y=0, line_dash="dash", line_color="#9ca3af", line_width=1.5,
                  annotation_text="SPY 基准 (0%)", annotation_position="right")

    bursts = plot_df[plot_df["end_alpha_1y"] > 20]
    for _, row in bursts.iterrows():
        fig.add_annotation(x=row["quarter"], y=row["end_alpha_1y"] + 3,
                           text="🚀 爆发季", showarrow=False,
                           font=dict(size=11, color="#16a34a"))

    ymax = max(plot_df["end_alpha_1y"].max() + 10, 15)
    ymin = min(plot_df["end_alpha_1y"].min() - 5, -10)

    fig.update_layout(
        **LIGHT_PLOTLY, height=320, showlegend=False,
        yaxis=dict(gridcolor="#f3f4f6", range=[ymin, ymax],
                   title=dict(text="季末 1Y Alpha (%)", font=dict(color="#9ca3af", size=11)),
                   ticksuffix="%", tickfont=dict(color="#6b7280", size=11)),
        xaxis=dict(showgrid=False, linecolor="#e5e7eb",
                   tickfont=dict(color="#6b7280", size=12)),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"bt_alpha_{cik}")


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    hc1, hc2 = st.columns([7, 1])
    with hc1:
        st.markdown(
            '<h1 style="color:#111827;font-weight:700;margin-bottom:2px;font-size:28px;">'
            '📊 13F 机构持仓看板</h1>',
            unsafe_allow_html=True)
        st.markdown(
            '<p style="color:#9ca3af;margin:0;font-size:13px;">'
            'Smart Money Tracker · SEC EDGAR 13F · 20 家顶级机构</p>',
            unsafe_allow_html=True)
    with hc2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("↻ 刷新", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    quarters = load_quarters()
    df       = load_df()
    ct       = load_ct()
    sc       = load_sc()
    bt_df    = load_bt_fund_results()

    if df.empty:
        st.error("holdings.db 无数据，请先运行 `python fetch_all.py`")
        return

    cq = quarters[-1] if quarters else "—"
    disp_qs = [q for q in quarters if q != "2025Q1"]

    n_funds, top_sec, top_new, top_exit = compute_kpis(df, sc, quarters)
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("基金池规模", f"{n_funds} 家",
                  f"覆盖 {len(disp_qs)} 季完整数据")
    with k2:
        st.metric("最强共识行业", top_sec[0],
                  f"{top_sec[1]} 家基金持有 · {cq}")
    with k3:
        st.metric("本季最多新仓", top_new[0],
                  f"{top_new[1]} 家同季建仓")
    with k4:
        st.metric("本季最大减仓", top_exit[0],
                  f"{top_exit[1]} 家同季退出")

    st.markdown("<br>", unsafe_allow_html=True)

    # Tab 顺序：回测分析 → 本季信号 → 行业聚焦 → 逻辑验证
    t1, t2, t3, t4 = st.tabs([
        "📊  回测分析", "📈  本季信号", "🏭  行业聚焦", "🔍  逻辑验证"
    ])
    with t1:
        tab_backtest()
    with t2:
        tab_signals(df, ct, quarters, bt_fund_df=bt_df)
    with t3:
        tab_sector(df, sc, quarters, ct=ct)
    with t4:
        tab_thesis(df, ct, quarters)


if __name__ == "__main__":
    main()
