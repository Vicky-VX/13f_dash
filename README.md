# 13F 机构持仓看板

实时追踪顶级对冲基金 13F 申报持仓的 Streamlit 看板。

## 功能

- 各季度机构持仓对比
- 持仓变动分析（新建/增持/减持/清仓）
- 行业分布与集中度
- 聪明钱选股推荐
- 策略回测

## 本地运行

```bash
pip install -r requirements.txt
streamlit run dashboard.py
```

## 数据说明

- 数据来源：SEC EDGAR 13F 申报文件
- 本地模式：读取 `holdings.db`（SQLite）
- 云端模式：读取 `data/` 目录下的 CSV 文件（自动检测）
- 季度数据每季度自动更新（通过 GitHub Actions）

## 部署

已部署至 [Streamlit Cloud](https://streamlit.io/cloud)。
