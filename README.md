# 📈 My Trading Journal & Performance Analytics

自動化交易日誌與機構級量化分析儀表板，基於 **Pepperstone cTrader MCP** 同步的真實成交數據與 OHLC K線生成。

---

## 🚀 專案特點
* **多週期 K 線聯動 (Multi-Timeframe Replay):** 點擊任何一筆交易，立即展示 **15m / 5m / 1m (各 120 支真實 K 線)** 的進出場位置與盈虧路徑。
* **純粹價格行為 (Clean Price Action):** 完全剔除成交量噪聲，進出場標籤外移至頂部/底部留白區，Y 軸精確高亮價格，0% 遮擋蠟燭圖。
* **量化 KPI 儀表板:** 即時計算勝率、盈虧比 (Payoff Ratio)、獲利因子 (Profit Factor)、單筆期望值 (EV) 與最大日內回撤。
* **零依賴與純靜態:** 採用原生 SVG 與純靜態 HTML，支援 GitHub Pages 免費託管，手機、iPad 或電腦瀏覽器直接開箱即用。

---

## 📁 目錄結構

```text
Trading Journal/
├── index.html                   # 主頁 (最新互動儀表板，點擊交易即時切換多週期K線)
├── dashboards/                  # 各交易日專屬互動 HTML 儀表板
│   ├── 2026-11-18.html          # 11月18日儀表板 (盈利 +$6.94)
│   └── 2026-11-19.html          # 11月19日儀表板 (回撤 -$11.65)
├── reports/                     # 各交易日詳細量化分析報告 (Markdown)
│   ├── 2026-11-18.md
│   └── 2026-11-19.md
└── charts/                      # 靜態高解析度 SVG K 線圖檔
```

---

## 🌐 啟用 GitHub Pages (免伺服器網頁版)

如果你希望在手機或任何瀏覽器隨時查閱：
1. 將本專案推送至 GitHub **Private (私有)** 儲存庫。
2. 進入儲存庫的 **Settings > Pages**。
3. 在 **Branch** 選擇 `main`，資料夾選擇 `/ (root)`，點擊 **Save**。
4. 稍等 1 分鐘，GitHub 會生成一個專屬網址（例如：`https://<your-username>.github.io/trading-journal/`），隨時隨地查閱最新交易數據。

---

*注意：本專案僅包含成交點位與行情 K 線，不包含任何 API Token 或帳號密碼，安全無虞。*
