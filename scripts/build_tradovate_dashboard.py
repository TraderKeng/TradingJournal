#!/usr/bin/env python3
"""
Tradovate Trading Journal & Dashboard Builder
Reads Tradovate Orders CSV exports from `Tradovate Report/`,
matches round-trip trades via FIFO, deducts per-contract commissions
(MNQ: $1.00/contract, MGC: $1.60/contract), merges TradingView OHLC bars,
and generates `tradovate/data_tradovate.json` and `tradovate/index.html`.
"""

import csv
import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

REPO_DIR = "/Users/lamkenneth/Claude/Trading Journal"
REPORT_DIR = os.path.join(REPO_DIR, "Tradovate Report")
OUT_DIR = os.path.join(REPO_DIR, "tradovate")
BARS_CACHE_PATH = os.path.join(OUT_DIR, "bars_cache.json")
DATA_JSON_PATH = os.path.join(OUT_DIR, "data_tradovate.json")
HTML_OUT_PATH = os.path.join(OUT_DIR, "index.html")
PLANS_INDEX_PATH = os.path.join(REPO_DIR, "plans", "plans_index.json")

HKT = timezone(timedelta(hours=8))
NY_TZ = ZoneInfo("America/New_York")

# Per-contract point multiplier (fallback if Notional Value is missing)
MULTIPLIERS = {
    "MGC": 10.0,
    "MNQ": 2.0,
    "MES": 5.0,
    "MYM": 0.5,
    "MCL": 100.0,
    "NQ": 20.0,
    "ES": 50.0,
    "GC": 100.0,
}

# Round-trip commission per contract (USD)
COMMISSION_PER_CONTRACT = {
    "MNQ": 1.00,
    "MGC": 1.60,
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def classify_session(dt_hkt: datetime):
    """Classify entry timestamp into trading session matching cTrader dashboard."""
    h_hkt = dt_hkt.hour + dt_hkt.minute / 60.0
    if 9.0 <= h_hkt < 11.0:
        return "asia", "亞盤 (09:00-11:00)"

    dt_ny = dt_hkt.astimezone(NY_TZ)
    h_ny = dt_ny.hour + dt_ny.minute / 60.0
    if 2.0 <= h_ny < 5.0:
        return "london", "歐盤 (NY 02:00-05:00)"
    if 7.0 <= h_ny < 9.0:
        return "premarket", "PreMarket (NY 07:00-09:00)"
    if 9.5 <= h_ny < 11.0:
        return "ny", "美盤 (NY 09:30-11:00)"
    return "other", "其它時段"


def parse_dt_hkt(s: str) -> datetime:
    dt = datetime.strptime(s.strip(), "%m/%d/%Y %H:%M:%S")
    return dt.replace(tzinfo=HKT)


def load_filled_orders():
    csv_files = sorted(glob.glob(os.path.join(REPORT_DIR, "*.csv")))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {REPORT_DIR}")

    by_order_id = {}
    for path in csv_files:
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                status = row.get("Status", "").strip()
                if status != "Filled":
                    continue
                oid = row.get("orderId", "").strip() or row.get("Order ID", "").strip()
                by_order_id[oid] = row

    orders = list(by_order_id.values())
    orders.sort(key=lambda r: (parse_dt_hkt(r["Fill Time"]), int(r["orderId"].strip())))
    return orders


def infer_multiplier(row) -> float:
    prod = row["Product"].strip()
    try:
        nv = float(row["Notional Value"].replace(",", "").strip())
        ap = float(row["decimalFillAvg"].strip())
        fq = float(row["filledQty"].strip())
        if ap > 0 and fq > 0:
            return round(nv / (ap * fq), 4)
    except Exception:
        pass
    return MULTIPLIERS.get(prod, 1.0)


def match_trades(orders):
    """FIFO round-trip matching per contract, grouping identical (entry_oid, exit_oid) lots."""
    queues = defaultdict(list)  # contract -> list of dicts
    raw_matches = []

    for r in orders:
        contract = r["Contract"].strip()
        prod = r["Product"].strip()
        side = r["B/S"].strip().upper()  # BUY / SELL
        qty = int(float(r["filledQty"].strip()))
        price = round(float(r["decimalFillAvg"].strip()), 4)
        dt_hkt = parse_dt_hkt(r["Fill Time"])
        oid = r["orderId"].strip()
        mult = infer_multiplier(r)

        q = queues[contract]
        for _ in range(qty):
            if not q or q[0]["side"] == side:
                q.append({
                    "oid": oid,
                    "side": side,
                    "price": price,
                    "dt": dt_hkt,
                    "prod": prod,
                    "contract": contract,
                    "mult": mult,
                })
            else:
                entry = q.pop(0)
                raw_matches.append({
                    "entry_oid": entry["oid"],
                    "exit_oid": oid,
                    "sym": prod,
                    "contract": contract,
                    "side": entry["side"],
                    "in_price": entry["price"],
                    "out_price": price,
                    "in_dt": entry["dt"],
                    "out_dt": dt_hkt,
                    "mult": entry["mult"],
                    "vol": 1,
                })

    # Group lots sharing the exact same (entry_oid, exit_oid) into one multi-contract trade
    grouped = []
    index_by_pair = {}
    for m in raw_matches:
        key = (m["entry_oid"], m["exit_oid"])
        if key in index_by_pair:
            grouped[index_by_pair[key]]["vol"] += 1
        else:
            index_by_pair[key] = len(grouped)
            grouped.append(m)

    # Sort matched trades chronologically by entry time, then exit time
    grouped.sort(key=lambda t: (t["in_dt"], t["out_dt"]))

    # Finalize fields
    trades = []
    for m in grouped:
        vol = m["vol"]
        in_p = m["in_price"]
        out_p = m["out_price"]
        mult = m["mult"]
        sym = m["sym"]

        if m["side"] == "BUY":
            gross_pnl = (out_p - in_p) * mult * vol
        else:
            gross_pnl = (in_p - out_p) * mult * vol

        comm_rate = COMMISSION_PER_CONTRACT.get(sym, 0.0)
        commission = round(comm_rate * vol, 2)
        net_pnl = round(gross_pnl - commission, 2)
        pts_diff = round(abs(out_p - in_p), 2)

        sess_code, sess_name = classify_session(m["in_dt"])
        date_key = m["out_dt"].strftime("%Y-%m-%d")

        trades.append({
            "date_key": date_key,
            "sym": sym,
            "contract": m["contract"],
            "side": m["side"],
            "vol": vol,
            "in_price": round(in_p, 2),
            "out_price": round(out_p, 2),
            "pts_diff": pts_diff,
            "in_ts": int(m["in_dt"].timestamp() * 1000),
            "out_ts": int(m["out_dt"].timestamp() * 1000),
            "in_time": m["in_dt"].strftime("%H:%M:%S"),
            "out_time": m["out_dt"].strftime("%H:%M:%S"),
            "gross_pnl": round(gross_pnl, 2),
            "commission": commission,
            "pnl": net_pnl,
            "isWin": net_pnl > 0,
            "session_code": sess_code,
            "session_name": sess_name,
        })

    return trades, queues


def build_journal_db(trades, bars):
    days_map = defaultdict(list)
    for t in trades:
        days_map[t["date_key"]].append(t)

    days = {}
    for date_key in sorted(days_map.keys()):
        day_trades = days_map[date_key]
        day_trades.sort(key=lambda t: (t["in_ts"], t["out_ts"]))

        formatted_trades = []
        for idx, t in enumerate(day_trades, start=1):
            t_copy = dict(t)
            del t_copy["date_key"]
            t_copy["id"] = idx
            formatted_trades.append(t_copy)

        dt_obj = datetime.strptime(date_key, "%Y-%m-%d")
        wins = [t for t in formatted_trades if t["pnl"] > 0]
        losses = [t for t in formatted_trades if t["pnl"] < 0]
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        net_pnl = round(sum(t["pnl"] for t in formatted_trades), 2)
        total_gross_pnl = round(sum(t["gross_pnl"] for t in formatted_trades), 2)
        total_comm = round(sum(t["commission"] for t in formatted_trades), 2)

        win_rate = round((len(wins) / len(formatted_trades)) * 100, 1) if formatted_trades else 0.0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        payoff = round(avg_win / avg_loss, 2) if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0)

        days[date_key] = {
            "date": date_key,
            "day_num": dt_obj.day,
            "day_of_week": WEEKDAY_NAMES[dt_obj.weekday()],
            "net_pnl": net_pnl,
            "gross_pnl": total_gross_pnl,
            "commission": total_comm,
            "is_win": net_pnl >= 0,
            "trades_count": len(formatted_trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "payoff": payoff,
            "trades": formatted_trades,
        }

    all_months = sorted(set(d[:7] for d in days.keys()))
    latest_month = all_months[-1] if all_months else "2026-11"
    total_pnl = round(sum(d["net_pnl"] for d in days.values()), 2)
    total_gross = round(sum(d["gross_pnl"] for d in days.values()), 2)
    total_comm = round(sum(d["commission"] for d in days.values()), 2)
    total_contracts = sum(t["vol"] for d in days.values() for t in d["trades"])

    summary = {
        "platform": "Tradovate",
        "account": "LFF02567128570001",
        "month": latest_month,
        "months": all_months,
        "total_pnl": total_pnl,
        "total_gross_pnl": total_gross,
        "total_commission": total_comm,
        "total_contracts": total_contracts,
        "trading_days": len(days),
        "win_days": sum(1 for d in days.values() if d["net_pnl"] > 0),
        "loss_days": sum(1 for d in days.values() if d["net_pnl"] < 0),
        "total_trades": sum(d["trades_count"] for d in days.values()),
        "win_trades": sum(d["win_count"] for d in days.values()),
        "loss_trades": sum(d["loss_count"] for d in days.values()),
    }

    return {
        "summary": summary,
        "days": days,
        "bars": bars,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-HK">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TraderKeng | Tradovate 期貨交易日誌與量化月曆 (Trading Journal)</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    /* Custom scrollbars */
    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: #0b0e14;
    }
    ::-webkit-scrollbar-thumb {
      background: #1e2638;
      border-radius: 4px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: #2b354d;
    }

    .custom-scrollbar::-webkit-scrollbar {
      width: 6px;
    }
    .custom-scrollbar::-webkit-scrollbar-track {
      background: #0d121c;
      border-radius: 4px;
    }
    .custom-scrollbar::-webkit-scrollbar-thumb {
      background: #1e2638;
      border-radius: 4px;
    }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover {
      background: #2d3b55;
    }
    .cal-cell {
      min-height: 100px;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .cal-cell.active-day {
      cursor: pointer;
    }
    .cal-cell.active-day:hover {
      transform: translateY(-2px);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);
    }
    .metric-card {
      transition: all 0.15s ease-in-out;
    }
    .metric-card:hover {
      transform: translateY(-1px);
    }
    .session-chip {
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .session-chip:hover {
      transform: translateY(-1px);
    }
    .session-chip.active-session {
      border-color: #3b82f6;
      background-color: rgba(59, 130, 246, 0.18);
    }
    .filter-pill {
      cursor: pointer;
      transition: all 0.12s ease;
    }
    .trade-row {
      cursor: pointer;
      transition: background-color 0.12s ease;
    }
    .trade-row:hover {
      background-color: rgba(59, 130, 246, 0.10);
    }
    .trade-row.active-row {
      background-color: rgba(59, 130, 246, 0.22);
      border-left: 4px solid #3b82f6;
    }
  </style>
</head>
<body class="bg-[#0b0e14] text-[#e1e7ec] antialiased h-screen flex flex-col overflow-hidden">

  <!-- ================= TOP APP HEADER ================= -->
  <header class="flex-shrink-0 flex flex-col sm:flex-row sm:items-center justify-between px-4 sm:px-6 py-2.5 border-b border-[#1e2638] bg-[#0d121c] gap-2.5 z-30">
    <div class="flex items-center gap-3">
      <span class="inline-flex items-center justify-center w-8 h-8 rounded-lg bg-amber-500/10 text-amber-400 font-extrabold text-sm border border-amber-500/20 shadow-sm">
        TV
      </span>
      <div>
        <h1 class="text-sm sm:text-base font-bold tracking-tight text-white flex items-center gap-2">
          <span>TraderKeng Trading Journal</span>
          <span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/20">Tradovate Futures</span>
        </h1>
        <p class="text-[11px] text-[#8b9bb4]">Tradovate 期貨實盤日誌 (LFF02567128570001) ｜ 香港時間 (HKT) ｜ 估算手續費: MNQ $1 / MGC $1.6</p>
      </div>
    </div>

    <div class="flex items-center gap-2 flex-wrap">
      <!-- Platform Switcher (cTrader <-> Tradovate) -->
      <div class="flex items-center gap-1 bg-[#121824] p-1 rounded-xl border border-[#1e2638] text-xs font-semibold">
        <a href="../index.html" class="px-2.5 py-1.5 rounded-lg text-[#8b9bb4] hover:text-white transition-all">cTrader</a>
        <span class="px-2.5 py-1.5 rounded-lg bg-amber-500/15 text-amber-400 border border-amber-500/30 shadow-sm">Tradovate</span>
      </div>

      <!-- Navigation Switcher -->
      <div class="flex items-center gap-1.5 bg-[#121824] p-1 rounded-xl border border-[#1e2638] text-xs font-semibold">
        <button onclick="showView('calendar')" id="nav-btn-cal" class="px-3 py-1.5 rounded-lg bg-[#1e2638] text-white shadow-sm flex items-center gap-1.5 transition-all">
          <svg class="w-3.5 h-3.5 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
          日曆月視圖 (Calendar)
        </button>
        <button onclick="showView('daily')" id="nav-btn-daily" class="px-3 py-1.5 rounded-lg text-[#8b9bb4] hover:text-white flex items-center gap-1.5 transition-all">
          <svg class="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
          單日詳細覆盤 (Daily Detail)
        </button>
      </div>
    </div>
  </header>

  <!-- ================= VIEW 1: CALENDAR VIEW (LEFT: DATA PANE, RIGHT: CALENDAR) ================= -->
  <main id="view-calendar" class="flex-1 overflow-hidden p-3 sm:p-5 flex flex-col lg:flex-row gap-4">

    <!-- LEFT COLUMN: INDEPENDENT SCROLLABLE DATA PANE -->
    <div class="w-full lg:w-[460px] xl:w-[500px] flex-shrink-0 overflow-y-auto space-y-3.5 pr-1.5 custom-scrollbar flex flex-col">

      <!-- 1. MONTH TOP KPI CARDS (2-Column Grid) -->
      <div id="monthly-kpis-container" class="grid grid-cols-2 gap-2.5 flex-shrink-0">
        <!-- Dynamically populated via JS -->
      </div>

      <!-- 1.5 DAILY NET CUMULATIVE P&L CURVE -->
      <div class="bg-[#121824] border border-[#1e2638] rounded-2xl p-4 shadow-sm space-y-2.5 flex-shrink-0">
        <div class="flex items-center justify-between border-b border-[#1e2638] pb-2">
          <div class="flex items-center gap-2">
            <h2 class="text-sm font-bold text-white">Daily net cumulative P&amp;L</h2>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-[#0b0e14] text-[#8b9bb4] border border-[#1e2638]" title="每日累計淨盈虧曲線（已扣估算手續費）">累計淨值曲線</span>
          </div>
          <span id="cum-pnl-badge" class="text-xs font-mono font-extrabold text-emerald-400">+$0.00</span>
        </div>
        <div id="cumulative-pnl-chart" class="relative w-full h-[210px] rounded-xl bg-[#0b0e14] border border-[#1e2638] overflow-hidden">
          <!-- Rendered dynamically via SVG -->
        </div>
      </div>

      <!-- 2. MONTHLY SESSION BREAKDOWN BLOCK -->
      <div class="bg-[#121824] border border-[#1e2638] rounded-2xl p-4 shadow-sm space-y-3 flex-shrink-0">
        <div class="flex items-center justify-between border-b border-[#1e2638] pb-2.5">
          <div class="flex items-center gap-2">
            <span class="p-1 rounded-lg bg-blue-500/10 text-blue-400 border border-blue-500/20">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            </span>
            <div>
              <h2 class="text-sm font-bold text-white">時段合計表現 (Session Totals)</h2>
              <p class="text-[10px] text-[#8b9bb4]">亞盤 / 歐盤 / PreMarket / 美盤 / 其它</p>
            </div>
          </div>
          <span id="session-total-badge" class="text-[11px] text-[#8b9bb4] font-mono">共 0 筆</span>
        </div>

        <!-- 5 Session Summary Cards -->
        <div id="monthly-sessions-container" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2 gap-2">
          <!-- Dynamically populated via JS -->
        </div>

        <!-- Dynamic Quant Insight Bar -->
        <div id="session-insight-bar" class="px-3 py-2 rounded-xl bg-[#0b0e14] border border-[#1e2638] text-[11px] leading-relaxed">
          <!-- Dynamically populated via JS -->
        </div>
      </div>

      <!-- 3. MONTHLY SYMBOL BREAKDOWN & ALPHA -->
      <div class="bg-[#121824] border border-[#1e2638] rounded-2xl p-4 shadow-sm space-y-2.5 flex-shrink-0">
        <div class="flex items-center justify-between border-b border-[#1e2638] pb-2">
          <div class="flex items-center gap-2">
            <span class="p-1 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/></svg>
            </span>
            <div>
              <h2 class="text-sm font-bold text-white">品種表現分佈 (Symbol Alpha)</h2>
              <p class="text-[10px] text-[#8b9bb4]">核心盈利 vs 虧損流血點 (已扣估算手續費)</p>
            </div>
          </div>
          <span id="top-symbol-badge" class="text-[11px] font-mono text-emerald-400 font-bold">--</span>
        </div>
        <div id="monthly-symbols-container" class="grid grid-cols-1 gap-2">
          <!-- Dynamically populated via JS -->
        </div>
      </div>

    </div>

    <!-- RIGHT COLUMN: PINNED TRADING CALENDAR -->
    <div class="flex-1 flex flex-col min-w-0 bg-[#121824] border border-[#1e2638] rounded-2xl p-4 sm:p-5 shadow-sm overflow-y-auto">

      <!-- Calendar Header (Arrow Month Stepper) -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-3 mb-3 border-b border-[#1e2638] gap-3 flex-shrink-0">
        <div class="flex items-center gap-2.5 flex-wrap">
          <div class="inline-flex items-center gap-1 bg-[#0b0e14] px-1.5 py-1 rounded-xl border border-[#1e2638]">
            <button onclick="stepCalendarMonth(-1)" id="btn-cal-prev" title="上一個月" class="w-7 h-7 rounded-lg flex items-center justify-center text-[#8b9bb4] hover:text-white hover:bg-[#1e2638] transition-all">
              <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z"/></svg>
            </button>
            <span id="calendar-month-title" class="min-w-[142px] text-center text-sm sm:text-base font-bold text-white tracking-tight select-none px-1">
              November 2026
            </span>
            <button onclick="stepCalendarMonth(1)" id="btn-cal-next" title="下一個月" class="w-7 h-7 rounded-lg flex items-center justify-center text-[#8b9bb4] hover:text-white hover:bg-[#1e2638] transition-all">
              <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"/></svg>
            </button>
          </div>

          <button onclick="jumpToCurrentMonth()" id="btn-this-month" class="px-3 py-1.5 rounded-xl bg-[#1e2638] hover:bg-[#2b354d] text-white text-xs font-semibold border border-[#2b354d] transition-all">
            This month
          </button>

          <button onclick="toggleAllOverview()" id="btn-scope-all" class="px-2.5 py-1.5 rounded-xl bg-[#0b0e14] hover:bg-[#1e2638] text-[#8b9bb4] hover:text-white text-xs font-semibold border border-[#1e2638] transition-all">
            全部統計
          </button>
        </div>

        <div class="flex items-center gap-3 text-xs">
          <span class="inline-flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span> 盈利日 (Win)</span>
          <span class="inline-flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-rose-400"></span> 虧損日 (Loss)</span>
        </div>
      </div>

      <!-- Weekday Headers -->
      <div class="grid grid-cols-7 gap-2 mb-2 text-center text-xs font-bold text-[#8b9bb4] uppercase flex-shrink-0">
        <div class="py-1">Sun</div>
        <div class="py-1 text-white">Mon</div>
        <div class="py-1 text-white">Tue</div>
        <div class="py-1 text-white">Wed</div>
        <div class="py-1 text-white">Thu</div>
        <div class="py-1 text-white">Fri</div>
        <div class="py-1">Sat</div>
      </div>

      <!-- Calendar Grid Cells -->
      <div class="grid grid-cols-7 gap-2 sm:gap-2.5 text-xs font-mono flex-1 auto-rows-fr" id="calendar-grid">
        <!-- Rendered dynamically via JS -->
      </div>
    </div>

  </main>

  <!-- ================= VIEW 2: DAILY DETAIL VIEW (3-PANE LAYOUT) ================= -->
  <main id="view-daily" class="flex-1 flex flex-col overflow-hidden hidden px-3 sm:px-6 pb-3">

    <!-- 1. PART 1: TOP FIXED BLOCK -->
    <div class="flex-shrink-0 pt-2 pb-2.5 space-y-2.5 z-20 bg-[#0b0e14] border-b border-[#1e2638]">

      <!-- Sub-header bar -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
        <div class="flex items-center gap-3">
          <button onclick="showView('calendar')" class="px-3 py-1.5 rounded-xl bg-[#1e2638] hover:bg-[#2b354d] text-white text-xs font-semibold flex items-center gap-1.5 transition-all shadow-sm">
            <svg class="w-3.5 h-3.5 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"/></svg>
            返回日曆總覽
          </button>
          <div class="h-5 w-px bg-[#1e2638]"></div>
          <div>
            <h2 id="daily-header-title" class="text-sm sm:text-base font-bold text-white">--</h2>
            <p id="daily-header-subtitle" class="text-[11px] text-[#8b9bb4]">--</p>
          </div>
        </div>

        <!-- Arrow Date Stepper -->
        <div class="relative flex items-center gap-2">
          <div class="inline-flex items-center gap-1 bg-[#121824] px-1.5 py-1 rounded-xl border border-[#1e2638]">
            <button onclick="stepTradingDate(-1)" id="btn-date-prev" title="上一個交易日" class="w-7 h-7 rounded-lg flex items-center justify-center text-[#8b9bb4] hover:text-white hover:bg-[#1e2638] disabled:opacity-30 disabled:cursor-not-allowed transition-all">
              <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z"/></svg>
            </button>
            <button onclick="toggleDateDropdown()" id="btn-date-center" title="點擊展開所有交易日快速跳轉" class="min-w-[195px] px-2.5 py-1 rounded-lg hover:bg-[#1e2638]/70 text-xs sm:text-sm font-bold text-white font-mono flex items-center justify-center gap-1.5 transition-all">
              <span id="date-stepper-label">2026-11-20</span>
              <span id="date-stepper-pnl" class="text-xs font-extrabold text-emerald-400">+$0.00</span>
              <svg class="w-3 h-3 text-[#8b9bb4]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
            </button>
            <button onclick="stepTradingDate(1)" id="btn-date-next" title="下一個交易日" class="w-7 h-7 rounded-lg flex items-center justify-center text-[#8b9bb4] hover:text-white hover:bg-[#1e2638] disabled:opacity-30 disabled:cursor-not-allowed transition-all">
              <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"/></svg>
            </button>
          </div>

          <button onclick="jumpToLatestDate()" id="btn-latest-date" class="px-3 py-1.5 rounded-xl bg-[#1e2638] hover:bg-[#2b354d] text-white text-xs font-semibold border border-[#2b354d] transition-all flex-shrink-0">
            最新交易日
          </button>

          <!-- Mini Calendar Date Picker Popover -->
          <div id="date-dropdown-menu" class="hidden absolute right-0 top-11 w-[280px] bg-[#121824] border border-[#2b354d] rounded-2xl shadow-2xl p-3.5 z-50 select-none">
            <div class="flex items-center justify-between mb-3">
              <button onclick="stepMiniCalMonth(-1); event.stopPropagation();" class="w-7 h-7 rounded-lg flex items-center justify-center text-[#8b9bb4] hover:text-white hover:bg-[#1e2638] transition-all" title="上一個月">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>
              </button>
              <span id="mini-cal-title" class="text-sm font-bold text-white tracking-tight">November 2026</span>
              <button onclick="stepMiniCalMonth(1); event.stopPropagation();" class="w-7 h-7 rounded-lg flex items-center justify-center text-[#8b9bb4] hover:text-white hover:bg-[#1e2638] transition-all" title="下一個月">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
              </button>
            </div>
            <div class="grid grid-cols-7 gap-1 text-center text-[11px] font-semibold text-[#8b9bb4] mb-1.5">
              <div>Su</div><div>Mo</div><div>Tu</div><div>We</div><div>Th</div><div>Fr</div><div>Sa</div>
            </div>
            <div id="mini-cal-grid" class="grid grid-cols-7 gap-1 text-center font-mono text-xs">
            </div>
            <div class="mt-3 pt-2 border-t border-[#1e2638] flex items-center justify-between text-[10px] text-[#8b9bb4]">
              <span class="inline-flex items-center gap-1"><span class="w-2.5 h-0.5 bg-emerald-400 rounded"></span> 盈利日</span>
              <span class="inline-flex items-center gap-1"><span class="w-2.5 h-0.5 bg-rose-400 rounded"></span> 虧損日</span>
              <button onclick="jumpToLatestDate(); toggleDateDropdown(false);" class="text-blue-400 hover:underline font-sans font-semibold">最新交易日</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 5 Fixed KPI Cards -->
      <div id="daily-kpi-container" class="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <!-- Populated dynamically via JS -->
      </div>

      <!-- TIME SESSION CLASSIFICATION BLOCK -->
      <div class="bg-[#121824] border border-[#1e2638] rounded-xl px-3 py-2 flex flex-col md:flex-row md:items-center justify-between gap-2 shadow-sm">
        <div class="flex items-center gap-2 flex-shrink-0">
          <span class="text-xs font-bold text-white flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            交易時段分類表現 (Session Analytics):
          </span>
          <span class="text-[10px] text-[#8b9bb4]">點擊時段直接篩選訂單</span>
        </div>

        <!-- Session Pills Container -->
        <div id="session-chips-container" class="flex flex-wrap items-center gap-1.5">
          <!-- Populated dynamically via JS -->
        </div>
      </div>

    </div>

    <!-- 2. LOWER SPLIT SECTION: LEFT LOG vs RIGHT CHART -->
    <div class="flex-1 flex flex-col lg:flex-row gap-3.5 overflow-hidden pt-2">

      <!-- PART 2: LEFT PANEL - EXECUTION LOG -->
      <section class="w-full lg:w-[500px] xl:w-[540px] flex-shrink-0 flex flex-col bg-[#121824] border border-[#1e2638] rounded-2xl overflow-hidden shadow-sm">

        <!-- Sticky Controls & Filters Area -->
        <div class="flex-shrink-0 px-3.5 py-2.5 bg-[#121824] border-b border-[#1e2638] space-y-2">
          <div class="flex items-center justify-between">
            <h3 class="text-xs sm:text-sm font-bold text-white flex items-center gap-1.5">
              <span>📋 交易執行日誌 (Execution Log)</span>
              <span id="filtered-count-badge" class="text-[11px] px-2 py-0.2 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20 font-mono">0筆</span>
            </h3>
            <span class="text-[10px] text-[#8b9bb4]">已扣估算手續費 ｜ 獨立滾動</span>
          </div>

          <!-- Multi-Filter Bar -->
          <div class="flex flex-wrap items-center justify-between gap-1.5 pt-1 border-t border-[#1e2638]/60 text-xs">
            <div class="flex items-center gap-1" id="symbol-filters-container">
              <!-- Populated via JS -->
            </div>

            <div class="flex items-center gap-1 bg-[#0b0e14] p-0.5 rounded-lg border border-[#1e2638] text-[11px] font-semibold">
              <button onclick="setWinLossFilter('all')" id="btn-filter-all" class="filter-pill px-2 py-1 rounded bg-[#1e2638] text-white">全部</button>
              <button onclick="setWinLossFilter('win')" id="btn-filter-win" class="filter-pill px-2 py-1 rounded text-[#8b9bb4] hover:text-emerald-400">盈利</button>
              <button onclick="setWinLossFilter('loss')" id="btn-filter-loss" class="filter-pill px-2 py-1 rounded text-[#8b9bb4] hover:text-rose-400">虧損</button>
            </div>
          </div>
        </div>

        <!-- Scrollable Table Body Container -->
        <div class="flex-1 overflow-y-auto">
          <table class="w-full text-left">
            <thead class="text-[#8b9bb4] uppercase text-xs font-bold border-b border-[#1e2638] bg-[#0f141e] sticky top-0 z-10">
              <tr>
                <th class="py-2.5 px-3">#</th>
                <th class="py-2.5 px-2">時間</th>
                <th class="py-2.5 px-2">品種</th>
                <th class="py-2.5 px-2">方向</th>
                <th class="py-2.5 px-2 text-right">進場</th>
                <th class="py-2.5 px-2 text-right">出場</th>
                <th class="py-2.5 px-3 text-right">淨盈虧</th>
              </tr>
            </thead>
            <tbody id="trade-table-body" class="divide-y divide-[#1e2638] font-mono text-sm">
              <!-- Populated dynamically via JS -->
            </tbody>
          </table>
        </div>
      </section>

      <!-- PART 3: RIGHT PANEL - MULTI-TIMEFRAME CHART -->
      <section class="flex-1 flex flex-col bg-[#121824] border-2 border-blue-500/30 rounded-2xl overflow-y-auto custom-scrollbar shadow-xl p-3.5 sm:p-4 min-w-0">

        <!-- Chart Controls Header -->
        <div class="flex-shrink-0 flex flex-col sm:flex-row sm:items-center justify-between pb-2.5 mb-2.5 border-b border-[#1e2638] gap-2">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span id="viewer-trade-badge" class="px-2 py-0.5 rounded text-xs font-bold bg-blue-500/10 text-blue-400 border border-blue-500/20 flex-shrink-0">Trade #1</span>
              <h3 id="viewer-trade-title" class="text-xs sm:text-sm font-bold text-white truncate">--</h3>
            </div>
            <p id="viewer-trade-subtitle" class="text-[11px] text-[#8b9bb4] mt-0.5 truncate">--</p>
          </div>

          <!-- Right Panel Controls: Plan Overlay + Timeframe / Plan Tab -->
          <div class="flex items-center gap-1.5 flex-wrap flex-shrink-0">
            <button onclick="togglePlanLevels()" id="btn-toggle-levels" class="px-2.5 py-1.5 rounded-xl bg-purple-500/15 text-purple-300 border border-purple-500/35 text-xs font-bold flex items-center gap-1 transition-all" title="喺 K 線圖疊加開市前計劃嘅支持阻力位">
              <span>📐 計劃價位</span>
              <span id="levels-state-dot" class="w-1.5 h-1.5 rounded-full bg-purple-400"></span>
            </button>

            <div class="flex items-center gap-1 p-1 bg-[#0b0e14] rounded-xl border border-[#1e2638] text-xs font-medium">
              <button onclick="switchActiveTf('1m')" id="btn-tf-1m" class="tf-btn px-2.5 py-1 rounded-lg bg-[#1e2638] text-blue-400 font-bold shadow-xs">1m</button>
              <button onclick="switchActiveTf('5m')" id="btn-tf-5m" class="tf-btn px-2.5 py-1 rounded-lg text-[#8b9bb4] hover:text-white">5m</button>
              <button onclick="switchActiveTf('15m')" id="btn-tf-15m" class="tf-btn px-2.5 py-1 rounded-lg text-[#8b9bb4] hover:text-white">15m</button>
              <button onclick="switchActiveTf('all')" id="btn-tf-all" class="tf-btn px-2.5 py-1 rounded-lg text-[#8b9bb4] hover:text-white">三圖</button>
              <div class="w-px h-4 bg-[#1e2638] mx-0.5"></div>
              <button onclick="switchActiveTf('plan')" id="btn-tf-plan" class="tf-btn px-2.5 py-1 rounded-lg text-purple-300 hover:bg-purple-500/15 font-bold flex items-center gap-1 transition-all">
                <span>📐 計劃</span>
              </button>
            </div>
          </div>
        </div>

        <!-- Fixed SVG Chart Viewport -->
        <div id="chart-viewport" class="w-full min-h-[380px] sm:min-h-[430px] flex-shrink-0 rounded-xl bg-[#131722] border border-[#1e2638] flex items-center justify-center p-1">
          <!-- Rendered dynamically via JS -->
        </div>

        <!-- Fixed Stats summary bar under the chart -->
        <div id="viewer-stats-bar" class="flex-shrink-0 grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2.5 pt-2.5 border-t border-[#1e2638] text-xs">
          <!-- Populated dynamically via JS -->
        </div>

        <!-- TRADE REVIEW, RATING & COMMENTARY PANEL -->
        <div id="trade-review-panel" class="flex-shrink-0 mt-3 pt-3 border-t border-[#1e2638] bg-[#0b0e14] rounded-xl p-3.5 border border-[#1e2638] space-y-3">

          <!-- Rating Header & Auto-save Status -->
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-[#1e2638]/70 pb-2.5">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-xs font-bold text-white flex items-center gap-1.5">
                <svg class="w-4 h-4 text-amber-400" fill="currentColor" viewBox="0 0 20 20"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z"/></svg>
                訂單執行評分:
              </span>
              <div id="review-stars-box" class="flex items-center gap-1 cursor-pointer">
                <span onclick="setReviewRating(1)" onmouseover="hoverReviewRating(1)" onmouseout="resetHoverRating()" id="star-1" class="text-lg text-gray-600 transition-colors">★</span>
                <span onclick="setReviewRating(2)" onmouseover="hoverReviewRating(2)" onmouseout="resetHoverRating()" id="star-2" class="text-lg text-gray-600 transition-colors">★</span>
                <span onclick="setReviewRating(3)" onmouseover="hoverReviewRating(3)" onmouseout="resetHoverRating()" id="star-3" class="text-lg text-gray-600 transition-colors">★</span>
                <span onclick="setReviewRating(4)" onmouseover="hoverReviewRating(4)" onmouseout="resetHoverRating()" id="star-4" class="text-lg text-gray-600 transition-colors">★</span>
                <span onclick="setReviewRating(5)" onmouseover="hoverReviewRating(5)" onmouseout="resetHoverRating()" id="star-5" class="text-lg text-gray-600 transition-colors">★</span>
              </div>
              <span id="review-rating-desc" class="text-[11px] text-[#8b9bb4] font-medium ml-1">點擊星星評分</span>
              <div class="h-4 w-px bg-[#1e2638] mx-1"></div>
              <span class="text-[11px] font-bold text-[#8b9bb4]">Follow System:</span>
              <div class="inline-flex items-center gap-1 bg-[#121824] p-0.5 rounded-lg border border-[#1e2638] text-[11px] font-bold">
                <button onclick="setFollowSystem('YES')" id="btn-follow-yes" class="px-2 py-0.5 rounded text-[#8b9bb4] hover:text-emerald-400 transition-all">✅ YES</button>
                <button onclick="setFollowSystem('NO')" id="btn-follow-no" class="px-2 py-0.5 rounded text-[#8b9bb4] hover:text-rose-400 transition-all">⚠️ NO</button>
              </div>
            </div>

            <div class="flex items-center gap-2 text-xs">
              <span id="review-saved-badge" class="text-[10px] text-emerald-400 font-bold opacity-0 transition-opacity flex items-center gap-1">
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>
                ✓ 已儲存
              </span>
              <button onclick="exportReviewsJson()" class="px-2.5 py-1 rounded-lg text-[11px] bg-[#1e2638] hover:bg-[#2b354d] text-gray-300 hover:text-white border border-[#2b354d] flex items-center gap-1 transition-all" title="導出/備份所有覆盤心得與評分">
                <svg class="w-3.5 h-3.5 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4"/></svg>
                導出備份
              </button>
            </div>
          </div>

          <!-- Trade Psychology & Setup Tags -->
          <div>
            <div class="text-[11px] font-semibold text-[#8b9bb4] mb-1.5 flex items-center justify-between">
              <span>覆盤標籤 (點選快速標註):</span>
              <span class="text-[10px] text-gray-500">可多選</span>
            </div>
            <div id="review-tags-box" class="flex flex-wrap items-center gap-1.5 text-xs">
              <!-- Populated via JS -->
            </div>
          </div>

          <!-- Commentary Textarea -->
          <div class="relative">
            <textarea id="review-comment-textarea" oninput="onCommentChange()" rows="2"
              placeholder="在此輸入此 Trade 的進出場覆盤心得、交易心態、犯錯原因或下次改善點（輸入即時自動保存）..."
              class="w-full bg-[#121824] border border-[#1e2638] rounded-xl p-2.5 text-xs text-gray-200 placeholder-[#4f5d75] focus:outline-none focus:border-blue-500/70 focus:ring-1 focus:ring-blue-500/50 resize-none font-sans leading-relaxed"></textarea>
          </div>

        </div>
      </section>

    </div>
  </main>

  <!-- JAVASCRIPT LOGIC & EMBEDDED DATA -->
  <script>
    const journalDb = __JOURNAL_DB_JSON__;

    const allDatesSorted = Object.keys(journalDb.days).sort();
    let currentSelectedDate = allDatesSorted[allDatesSorted.length - 1] || '';
    let currentTradeId = 1;
    let currentTf = '1m';
    let currentCalendarMonth = journalDb.summary.month || (currentSelectedDate ? currentSelectedDate.substring(0, 7) : '2026-11');
    let currentOverviewScope = currentCalendarMonth; // default to current month

    // Filter states
    let selectedSymbols = new Set();
    let selectedWinLoss = 'all';
    let selectedSession = 'all';

    // ==========================================
    // 1. TRADE REVIEW & RATING SYSTEM (LocalStorage)
    // ==========================================
    const REVIEWS_STORAGE_KEY = 'traderkeng_tradovate_reviews';

    const dailyPlansDb = __DAILY_PLANS_JSON__;
    const PLAN_IMG_PREFIX = '../';
    const SYMBOL_TO_PLAN_FAMILY = {
      'MNQ': 'NQ', 'NQ': 'NQ', 'NAS100': 'NQ',
      'MGC': 'GC', 'GC': 'GC', 'XAUUSD': 'GC',
      'MES': 'ES', 'ES': 'ES', 'US500': 'ES',
      'HK50': 'HSF', 'HSF': 'HSF',
      'BTCUSD': 'BTC', 'BTC': 'BTC'
    };

    function getPlanForTrade(dateKey, sym) {
      const dayPlans = dailyPlansDb[dateKey];
      if (!dayPlans) return null;
      const family = SYMBOL_TO_PLAN_FAMILY[sym] || sym;
      return dayPlans[sym] || dayPlans[family] || null;
    }

    let showPlanLevelsOnChart = true;
    let currentPlanChartIdx = 0;

    function togglePlanLevels() {
      showPlanLevelsOnChart = !showPlanLevelsOnChart;
      const btn = document.getElementById('btn-toggle-levels');
      const dot = document.getElementById('levels-state-dot');
      if (btn) {
        btn.className = showPlanLevelsOnChart
          ? 'px-2.5 py-1.5 rounded-xl bg-purple-500/15 text-purple-300 border border-purple-500/35 text-xs font-bold flex items-center gap-1 transition-all'
          : 'px-2.5 py-1.5 rounded-xl bg-[#0b0e14] text-[#8b9bb4] border border-[#1e2638] text-xs font-medium flex items-center gap-1 transition-all';
      }
      if (dot) dot.className = showPlanLevelsOnChart ? 'w-1.5 h-1.5 rounded-full bg-purple-400' : 'w-1.5 h-1.5 rounded-full bg-gray-600';
      renderViewport();
    }

    function openPlanLightbox(src, caption) {
      const lb = document.getElementById('plan-lightbox');
      const img = document.getElementById('lightbox-img');
      const cap = document.getElementById('lightbox-caption');
      if (lb && img) {
        img.src = src;
        if (cap) cap.innerText = caption;
        lb.classList.remove('hidden');
      }
    }

    function closePlanLightbox() {
      const lb = document.getElementById('plan-lightbox');
      if (lb) lb.classList.add('hidden');
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closePlanLightbox();
    });

    function selectPlanChart(idx) {
      currentPlanChartIdx = idx;
      renderViewport();
    }

    function renderPlanViewer(trade) {
      const plan = getPlanForTrade(currentSelectedDate, trade.sym);
      if (!plan || !plan.charts || plan.charts.length === 0) {
        return `
          <div class="p-10 text-center text-gray-400 font-sans text-xs space-y-2">
            <div class="text-amber-400 font-bold text-sm">⚠️ ${currentSelectedDate} 並無 ${trade.sym} 的開市前交易計劃</div>
            <div class="text-[#8b9bb4]">系統已嚴格按品種群組配對（NQ ↔ MNQ/NAS100 ｜ GC ↔ MGC/XAUUSD），不會誤顯示其他品種的計劃圖。</div>
            <div class="pt-1">
              <button onclick="switchActiveTf('5m')" class="px-3 py-1 rounded-lg bg-[#1e2638] hover:bg-[#2b354d] text-blue-300 font-semibold transition-all">➔ 返回 5m K 線覆盤</button>
            </div>
          </div>`;
      }

      if (currentPlanChartIdx >= plan.charts.length) currentPlanChartIdx = plan.charts.length - 1;
      const activeChart = plan.charts[currentPlanChartIdx] || plan.charts[0];
      const imgSrc = PLAN_IMG_PREFIX + activeChart.src;

      const tabsHtml = plan.charts.map((c, idx) => {
        const isAct = idx === currentPlanChartIdx;
        const cls = isAct
          ? 'px-3 py-1 rounded-lg bg-purple-500/20 text-purple-200 border border-purple-500/40 font-bold'
          : 'px-3 py-1 rounded-lg bg-[#0b0e14] text-[#8b9bb4] hover:text-white border border-[#1e2638]';
        return `<button onclick="selectPlanChart(${idx})" class="text-xs transition-all ${cls}">${c.label}</button>`;
      }).join('');

      const levelsHtml = (plan.levels && plan.levels.length > 0)
        ? `<div class="flex-shrink-0 flex flex-wrap items-center gap-1.5 pt-1">
            <span class="text-[10px] font-bold text-purple-300 mr-1">AI 提取關鍵價位:</span>
            ${plan.levels.map(lv => `
              <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-[#0b0e14] border border-[#1e2638] text-[10px] font-mono text-gray-200">
                <span class="w-2 h-2 rounded-full" style="background:${lv.color}"></span>
                ${lv.label}
              </span>
            `).join('')}
          </div>`
        : '';

      return `
        <div class="w-full flex flex-col p-2.5 space-y-2">
          <div class="flex flex-wrap items-center justify-between gap-2 flex-shrink-0">
            <div class="flex items-center gap-1.5 flex-wrap">${tabsHtml}</div>
            <div class="flex items-center gap-2 text-[11px]">
              <span class="text-[#8b9bb4] font-mono">${plan.created_hkt || currentSelectedDate}</span>
              ${activeChart.tv ? `<a href="${activeChart.tv}" target="_blank" class="px-2 py-1 rounded-lg bg-[#0b0e14] hover:bg-[#1e2638] text-gray-300 border border-[#1e2638] transition-all">TV 原圖 ↗</a>` : ''}
              <a href="${plan.notion_url}" target="_blank" class="px-2.5 py-1 rounded-lg bg-[#1e2638] hover:bg-[#2b354d] text-blue-300 font-semibold transition-all">開啟 Notion 原頁 ↗</a>
            </div>
          </div>

          <div class="w-full min-h-[300px] sm:min-h-[350px] bg-[#0b0e14] rounded-xl border border-[#1e2638] flex items-center justify-center p-1.5 relative group">
            <img src="${imgSrc}" alt="${activeChart.label}"
                 onclick="openPlanLightbox('${imgSrc}', '${plan.title.replace(/'/g, '')} — ${activeChart.label}')"
                 class="w-full h-auto max-h-[460px] object-contain rounded-lg cursor-zoom-in transition-transform" />
            <span class="absolute bottom-2 right-2 px-2 py-1 rounded-lg bg-black/70 text-[10px] text-gray-300 opacity-80 group-hover:opacity-100 pointer-events-none">🔍 點擊放大全螢幕檢視</span>
          </div>
          ${levelsHtml}
        </div>
      `;
    }

    function setFollowSystem(val) {
      const db = getReviewsDb();
      const key = getTradeKey(currentSelectedDate, currentTradeId);
      if (!db[key]) db[key] = { rating: 0, tags: [], comment: '', followSystem: '' };
      db[key].followSystem = (db[key].followSystem === val) ? '' : val;
      db[key].updated_at = Date.now();
      saveReviewsDb(db);
      updateFollowSystemDisplay(db[key].followSystem);
    }

    function updateFollowSystemDisplay(val) {
      const btnY = document.getElementById('btn-follow-yes');
      const btnN = document.getElementById('btn-follow-no');
      if (btnY) btnY.className = val === 'YES'
        ? 'px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/40'
        : 'px-2 py-0.5 rounded text-[#8b9bb4] hover:text-emerald-400 transition-all';
      if (btnN) btnN.className = val === 'NO'
        ? 'px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 border border-rose-500/40'
        : 'px-2 py-0.5 rounded text-[#8b9bb4] hover:text-rose-400 transition-all';
    }

    const DEFAULT_REVIEW_TAGS = [
      { name: 'iFVG', type: 'pos' },
      { name: 'Breaker', type: 'pos' },
      { name: 'SMR', type: 'pos' },
      { name: 'FVG', type: 'pos' },
      { name: 'OB', type: 'pos' },
      { name: 'OTE', type: 'pos' },
      { name: '2B / TS', type: 'pos' },
      { name: '✓ 關鍵位狙擊', type: 'pos' },
      { name: '⚠️ 衝動追單 (FOMO)', type: 'neg' },
      { name: '⚠️ 半空進場', type: 'neg' },
      { name: '⚠️ 逆勢摸頂底', type: 'neg' },
      { name: '⚠️ 過早平倉', type: 'neg' }
    ];

    const RATING_DESCRIPTIONS = {
      0: '點擊星星評分',
      1: '★ 1.0 (嚴重失誤 / 衝動違規)',
      2: '★★ 2.0 (執行不佳 / 僥倖扛單)',
      3: '★★★ 3.0 (合格執行 / 達標)',
      4: '★★★★ 4.0 (良好執行 / 計劃精準)',
      5: '★★★★★ 5.0 (完美標杆 / 嚴格知行合一)'
    };

    function getReviewsDb() {
      try {
        const raw = localStorage.getItem(REVIEWS_STORAGE_KEY);
        return raw ? JSON.parse(raw) : {};
      } catch (e) {
        return {};
      }
    }

    function saveReviewsDb(db) {
      try {
        localStorage.setItem(REVIEWS_STORAGE_KEY, JSON.stringify(db));
        showSavedIndicator();
      } catch (e) {
        console.error('Error saving reviews:', e);
      }
    }

    function showSavedIndicator() {
      const el = document.getElementById('review-saved-badge');
      if (!el) return;
      el.style.opacity = '1';
      setTimeout(() => { el.style.opacity = '0'; }, 1800);
    }

    function getTradeKey(dateKey, tradeId) {
      return `${dateKey}_${tradeId}`;
    }

    function loadReviewForCurrentTrade() {
      const db = getReviewsDb();
      const key = getTradeKey(currentSelectedDate, currentTradeId);
      const rev = db[key] || { rating: 0, tags: [], comment: '' };
      updateStarDisplay(rev.rating || 0);
      updateFollowSystemDisplay(rev.followSystem || "");
      renderReviewTags(rev.tags || []);
      const txt = document.getElementById('review-comment-textarea');
      if (txt) txt.value = rev.comment || '';
    }

    function updateStarDisplay(rating) {
      for (let i = 1; i <= 5; i++) {
        const starEl = document.getElementById(`star-${i}`);
        if (!starEl) continue;
        starEl.className = i <= rating
          ? 'text-lg text-amber-400 transition-colors drop-shadow'
          : 'text-lg text-gray-600 hover:text-amber-300 transition-colors';
      }
      const descEl = document.getElementById('review-rating-desc');
      if (descEl) {
        descEl.innerText = RATING_DESCRIPTIONS[rating] || RATING_DESCRIPTIONS[0];
        descEl.className = rating > 0 ? 'text-[11px] text-amber-400 font-bold ml-1' : 'text-[11px] text-[#8b9bb4] font-medium ml-1';
      }
    }

    function hoverReviewRating(val) {
      for (let i = 1; i <= 5; i++) {
        const starEl = document.getElementById(`star-${i}`);
        if (!starEl) continue;
        starEl.className = i <= val ? 'text-lg text-amber-300 transition-colors' : 'text-lg text-gray-700 transition-colors';
      }
    }

    function resetHoverRating() {
      const db = getReviewsDb();
      const key = getTradeKey(currentSelectedDate, currentTradeId);
      const rev = db[key] || { rating: 0 };
      updateStarDisplay(rev.rating || 0);
      updateFollowSystemDisplay(rev.followSystem || "");
    }

    function setReviewRating(rating) {
      const db = getReviewsDb();
      const key = getTradeKey(currentSelectedDate, currentTradeId);
      if (!db[key]) db[key] = { rating: 0, tags: [], comment: '' };
      db[key].rating = (db[key].rating === rating) ? 0 : rating;
      db[key].updated_at = Date.now();
      saveReviewsDb(db);
      updateStarDisplay(db[key].rating);
      applyFilters(false);
    }

    function renderReviewTags(selectedTags) {
      const container = document.getElementById('review-tags-box');
      if (!container) return;
      container.innerHTML = '';
      DEFAULT_REVIEW_TAGS.forEach(tag => {
        const isSelected = selectedTags.includes(tag.name);
        const btn = document.createElement('button');
        if (isSelected) {
          btn.className = tag.type === 'pos'
            ? 'px-2 py-0.5 rounded-lg text-xs font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40 shadow-xs'
            : 'px-2 py-0.5 rounded-lg text-xs font-bold bg-rose-500/20 text-rose-400 border border-rose-500/40 shadow-xs';
        } else {
          btn.className = 'px-2 py-0.5 rounded-lg text-xs font-medium bg-[#121824] hover:bg-[#1a2336] text-[#8b9bb4] hover:text-white border border-[#1e2638] transition-all';
        }
        btn.innerText = tag.name;
        btn.onclick = () => toggleReviewTag(tag.name);
        container.appendChild(btn);
      });
    }

    function toggleReviewTag(tagName) {
      const db = getReviewsDb();
      const key = getTradeKey(currentSelectedDate, currentTradeId);
      if (!db[key]) db[key] = { rating: 0, tags: [], comment: '' };
      if (!db[key].tags) db[key].tags = [];
      const idx = db[key].tags.indexOf(tagName);
      if (idx !== -1) db[key].tags.splice(idx, 1);
      else db[key].tags.push(tagName);
      db[key].updated_at = Date.now();
      saveReviewsDb(db);
      renderReviewTags(db[key].tags);
    }

    let commentTimeout = null;
    function onCommentChange() {
      const txt = document.getElementById('review-comment-textarea').value;
      clearTimeout(commentTimeout);
      commentTimeout = setTimeout(() => {
        const db = getReviewsDb();
        const key = getTradeKey(currentSelectedDate, currentTradeId);
        if (!db[key]) db[key] = { rating: 0, tags: [], comment: '' };
        db[key].comment = txt.trim();
        db[key].updated_at = Date.now();
        saveReviewsDb(db);
        applyFilters(false);
      }, 400);
    }

    function exportReviewsJson() {
      const db = getReviewsDb();
      if (Object.keys(db).length === 0) {
        alert('目前瀏覽器中尚未有任何覆盤記錄！請先在訂單下方點擊星星或輸入筆記。');
        return;
      }
      const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(db, null, 2));
      const downloadAnchor = document.createElement('a');
      downloadAnchor.setAttribute("href", dataStr);
      downloadAnchor.setAttribute("download", `tradovate_reviews_backup_${new Date().toISOString().substring(0, 10)}.json`);
      document.body.appendChild(downloadAnchor);
      downloadAnchor.click();
      downloadAnchor.remove();
    }

    // ==========================================
    // 2. NAVIGATION VIEW SWITCHER
    // ==========================================
    function showView(viewName) {
      if (viewName === 'calendar') {
        document.getElementById('view-calendar').classList.remove('hidden');
        document.getElementById('view-daily').classList.add('hidden');
        document.getElementById('nav-btn-cal').className = "px-3 py-1.5 rounded-lg bg-[#1e2638] text-white shadow-sm flex items-center gap-1.5 transition-all";
        document.getElementById('nav-btn-daily').className = "px-3 py-1.5 rounded-lg text-[#8b9bb4] hover:text-white flex items-center gap-1.5 transition-all";
      } else {
        document.getElementById('view-calendar').classList.add('hidden');
        document.getElementById('view-daily').classList.remove('hidden');
        document.getElementById('nav-btn-daily').className = "px-3 py-1.5 rounded-lg bg-[#1e2638] text-white shadow-sm flex items-center gap-1.5 transition-all";
        document.getElementById('nav-btn-cal').className = "px-3 py-1.5 rounded-lg text-[#8b9bb4] hover:text-white flex items-center gap-1.5 transition-all";
      }
    }

    const MONTH_NAMES_EN = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
    const DEFAULT_MONTH = journalDb.summary.month || '2026-11';

    function stepCalendarMonth(delta) {
      const [y, m] = currentCalendarMonth.split('-').map(Number);
      const d = new Date(y, m - 1 + delta, 1);
      const ny = d.getFullYear();
      const nm = String(d.getMonth() + 1).padStart(2, '0');
      currentCalendarMonth = `${ny}-${nm}`;
      currentOverviewScope = currentCalendarMonth;
      renderMonthSwitcher();
      renderMonthlyOverview();
    }

    function jumpToCurrentMonth() {
      currentCalendarMonth = DEFAULT_MONTH;
      currentOverviewScope = DEFAULT_MONTH;
      renderMonthSwitcher();
      renderMonthlyOverview();
    }

    function toggleAllOverview() {
      currentOverviewScope = (currentOverviewScope === 'all') ? currentCalendarMonth : 'all';
      renderMonthSwitcher();
      renderMonthlyOverview();
    }

    function renderMonthSwitcher() {
      const btnThis = document.getElementById('btn-this-month');
      const btnAll = document.getElementById('btn-scope-all');
      if (btnThis) {
        const isDefault = currentCalendarMonth === DEFAULT_MONTH && currentOverviewScope !== 'all';
        btnThis.className = isDefault
          ? 'px-3 py-1.5 rounded-xl bg-[#1e2638] text-white text-xs font-semibold border border-blue-500/40 transition-all'
          : 'px-3 py-1.5 rounded-xl bg-[#0b0e14] hover:bg-[#1e2638] text-[#8b9bb4] hover:text-white text-xs font-semibold border border-[#1e2638] transition-all';
      }
      if (btnAll) {
        btnAll.className = currentOverviewScope === 'all'
          ? 'px-2.5 py-1.5 rounded-xl bg-blue-500/20 text-blue-400 text-xs font-semibold border border-blue-500/40 transition-all'
          : 'px-2.5 py-1.5 rounded-xl bg-[#0b0e14] hover:bg-[#1e2638] text-[#8b9bb4] hover:text-white text-xs font-semibold border border-[#1e2638] transition-all';
      }
    }

    // ==========================================
    // 3. DYNAMIC MONTHLY STATS & CALENDAR RENDER
    // ==========================================
    function renderMonthlyOverview() {
      let allTrades = [];
      let totalDays = 0;
      let winDays = 0;
      let lossDays = 0;
      let totalGross = 0;
      let totalComm = 0;

      Object.values(journalDb.days).forEach(d => {
        if (currentOverviewScope !== 'all' && !d.date.startsWith(currentOverviewScope)) return;
        totalDays++;
        if (d.net_pnl > 0) winDays++;
        else if (d.net_pnl < 0) lossDays++;
        totalGross += (d.gross_pnl || 0);
        totalComm += (d.commission || 0);
        allTrades = allTrades.concat(d.trades);
      });

      const wins = allTrades.filter(t => t.pnl > 0);
      const losses = allTrades.filter(t => t.pnl < 0);
      const grossProfit = wins.reduce((acc, t) => acc + t.pnl, 0);
      const grossLoss = losses.reduce((acc, t) => acc + t.pnl, 0);
      const netPnl = grossProfit + grossLoss;
      const avgWin = wins.length > 0 ? grossProfit / wins.length : 0;
      const avgLoss = losses.length > 0 ? Math.abs(grossLoss) / losses.length : 0;
      const payoffRatio = avgLoss > 0 ? (avgWin / avgLoss).toFixed(2) : '--';
      const tradeWinRate = allTrades.length > 0 ? ((wins.length / allTrades.length) * 100).toFixed(1) : '0.0';
      const expectancy = allTrades.length > 0 ? (netPnl / allTrades.length).toFixed(2) : '0.00';
      const totalContracts = allTrades.reduce((acc, t) => acc + (t.vol || 1), 0);

      let scopeLabel = '全部期間 (10/30 - 11/20)';
      if (currentOverviewScope !== 'all') {
        const [y, mo] = currentOverviewScope.split('-');
        scopeLabel = `${y}年${parseInt(mo, 10)}月`;
      }

      // 1. Top KPI Cards
      const kpisContainer = document.getElementById('monthly-kpis-container');
      const netColor = netPnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
      const netSign = netPnl >= 0 ? '+' : '-';

      kpisContainer.innerHTML = `
        <div class="col-span-2 metric-card bg-[#121824] border border-[#1e2638] rounded-2xl p-3.5 shadow-sm">
          <div class="flex items-center justify-between">
            <span class="text-xs font-medium text-[#8b9bb4]">${scopeLabel} 累計淨盈虧 (Net P&L)</span>
            <span class="text-[10px] font-mono text-[#8b9bb4]">毛盈虧 ${totalGross >= 0 ? '+' : '-'}$${Math.abs(totalGross).toFixed(2)} ｜ 佣金 -$${totalComm.toFixed(2)}</span>
          </div>
          <div class="text-2xl font-extrabold font-mono mt-1 ${netColor}">${netSign}$${Math.abs(netPnl).toFixed(2)} USD</div>
          <div class="text-[11px] text-[#8b9bb4] mt-1 font-sans">${totalDays} 個交易日 (${winDays}勝 ${lossDays}負) ｜ ${allTrades.length} 筆訂單 (${totalContracts} 口)</div>
        </div>

        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-2xl p-3 shadow-sm">
          <div class="text-xs font-medium text-[#8b9bb4]">總盈利 (Gross Profit)</div>
          <div class="text-lg sm:text-xl font-extrabold font-mono mt-1 text-emerald-400">+$${grossProfit.toFixed(2)}</div>
          <div class="text-[10px] text-emerald-400/80 mt-0.5 font-sans">${wins.length} 筆盈利單</div>
        </div>

        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-2xl p-3 shadow-sm">
          <div class="text-xs font-medium text-[#8b9bb4]">總虧損 (Gross Loss)</div>
          <div class="text-lg sm:text-xl font-extrabold font-mono mt-1 text-rose-400">-$${Math.abs(grossLoss).toFixed(2)}</div>
          <div class="text-[10px] text-rose-400/80 mt-0.5 font-sans">${losses.length} 筆虧損單</div>
        </div>

        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-2xl p-3 shadow-sm">
          <div class="text-xs font-medium text-[#8b9bb4]">平均盈利 / 虧損</div>
          <div class="text-sm sm:text-base font-extrabold font-mono mt-1 text-white">
            <span class="text-emerald-400">+$${avgWin.toFixed(2)}</span> / <span class="text-rose-400">-$${avgLoss.toFixed(2)}</span>
          </div>
          <div class="text-[10px] text-[#8b9bb4] mt-0.5 font-sans">盈虧比: <strong class="text-white">${payoffRatio}:1</strong></div>
        </div>

        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-2xl p-3 shadow-sm">
          <div class="text-xs font-medium text-[#8b9bb4]">訂單總勝率</div>
          <div class="text-lg sm:text-xl font-extrabold font-mono mt-1 text-amber-400">${tradeWinRate}%</div>
          <div class="text-[10px] text-[#8b9bb4] mt-0.5 font-sans">期望值: <strong class="${parseFloat(expectancy) >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${parseFloat(expectancy) >= 0 ? '+' : '-'}$${Math.abs(parseFloat(expectancy)).toFixed(2)}</strong></div>
        </div>
      `;

      document.getElementById('session-total-badge').innerText = `共 ${allTrades.length} 筆`;

      // 2. Session Breakdown
      const sessionDefs = [
        { key: 'asia', label: '亞盤', timeRange: 'HKT 09:00 - 11:00' },
        { key: 'london', label: '歐盤', timeRange: 'NY 02:00 - 05:00' },
        { key: 'premarket', label: 'PreMarket', timeRange: 'NY 07:00 - 09:00' },
        { key: 'ny', label: '美盤', timeRange: 'NY 09:30 - 11:00' },
        { key: 'other', label: '其它時段', timeRange: '其它交收/過夜' }
      ];

      const sessionStats = {};
      sessionDefs.forEach(s => {
        sessionStats[s.key] = { ...s, trades: [], pnl: 0, wins: 0, losses: 0 };
      });

      allTrades.forEach(t => {
        const code = t.session_code || 'other';
        if (sessionStats[code]) {
          sessionStats[code].trades.push(t);
          sessionStats[code].pnl += t.pnl;
          if (t.pnl > 0) sessionStats[code].wins++;
          else if (t.pnl < 0) sessionStats[code].losses++;
        }
      });

      const sessionsContainer = document.getElementById('monthly-sessions-container');
      sessionsContainer.innerHTML = '';

      sessionDefs.forEach(s => {
        const st = sessionStats[s.key];
        const count = st.trades.length;
        const sPnl = st.pnl;
        const sColor = sPnl > 0 ? 'text-emerald-400' : (sPnl < 0 ? 'text-rose-400' : 'text-gray-400');
        const sBg = sPnl > 0 ? 'bg-emerald-500/5 border-emerald-500/20' : (sPnl < 0 ? 'bg-rose-500/5 border-rose-500/20' : 'bg-[#0b0e14] border-[#1e2638]');
        const sSign = sPnl > 0 ? '+' : (sPnl < 0 ? '-' : '');
        const sWr = count > 0 ? ((st.wins / count) * 100).toFixed(1) : '0.0';

        const card = document.createElement('div');
        card.className = `p-3.5 rounded-xl border flex flex-col justify-between ${sBg}`;
        card.innerHTML = `
          <div>
            <div class="flex items-center justify-between">
              <span class="font-extrabold text-xs sm:text-sm text-white">${st.label}</span>
              <span class="text-[10px] font-sans px-1.5 py-0.5 rounded font-bold bg-[#121824] border border-[#1e2638] text-[#8b9bb4]">${count} 筆單</span>
            </div>
            <div class="text-[10px] text-[#8b9bb4] mt-0.5">${st.timeRange}</div>
          </div>
          <div class="my-2.5">
            <div class="text-lg sm:text-xl font-extrabold font-mono ${sColor}">${sSign}$${Math.abs(sPnl).toFixed(2)}</div>
          </div>
          <div class="text-[11px] font-sans flex items-center justify-between text-[#8b9bb4] pt-1.5 border-t border-white/5">
            <span>勝率: <strong class="text-white">${sWr}%</strong></span>
            <span>${st.wins}勝 / ${st.losses}負</span>
          </div>
        `;
        sessionsContainer.appendChild(card);
      });

      // Dynamic Session Insight Bar
      const activeSessions = Object.values(sessionStats).filter(s => s.trades.length > 0);
      const bestSess = activeSessions.slice().sort((a, b) => b.pnl - a.pnl)[0];
      const worstSess = activeSessions.slice().sort((a, b) => a.pnl - b.pnl)[0];
      const insightBar = document.getElementById('session-insight-bar');
      if (bestSess && worstSess && bestSess.key !== worstSess.key) {
        const bestSign = bestSess.pnl >= 0 ? '+' : '-';
        const worstSign = worstSess.pnl >= 0 ? '+' : '-';
        const bestWr = ((bestSess.wins / bestSess.trades.length) * 100).toFixed(1);
        insightBar.innerHTML = `
          <span class="text-amber-400 font-bold">💡 時段優勢洞察：</span>
          <span class="text-gray-300">
            <strong class="text-emerald-400">${bestSess.label}</strong> 表現最佳 (${bestSign}$${Math.abs(bestSess.pnl).toFixed(2)} USD，勝率 ${bestWr}%)；
            而 <strong class="text-rose-400">${worstSess.label}</strong> 表現最弱 (${worstSign}$${Math.abs(worstSess.pnl).toFixed(2)} USD)。
          </span>
        `;
      } else {
        insightBar.innerHTML = `<span class="text-amber-400 font-bold">💡 時段優勢洞察：</span><span class="text-gray-300">點擊右側日曆日期即可按時段篩選並覆盤每一筆 K 線。</span>`;
      }

      // 3. Symbol Breakdown
      const symbolsContainer = document.getElementById('monthly-symbols-container');
      symbolsContainer.innerHTML = '';

      const symKeys = Array.from(new Set(allTrades.map(t => t.sym))).sort((a, b) => {
        const pnlA = allTrades.filter(t => t.sym === a).reduce((s, t) => s + t.pnl, 0);
        const pnlB = allTrades.filter(t => t.sym === b).reduce((s, t) => s + t.pnl, 0);
        return pnlB - pnlA;
      });

      if (symKeys.length > 0) {
        const topSym = symKeys[0];
        const topTrades = allTrades.filter(t => t.sym === topSym);
        const topWins = topTrades.filter(t => t.pnl > 0);
        const topWr = ((topWins.length / topTrades.length) * 100).toFixed(1);
        document.getElementById('top-symbol-badge').innerText = `${topSym} 勝率 ${topWr}%`;
      }

      symKeys.forEach(sym => {
        const sTrades = allTrades.filter(t => t.sym === sym);
        const sWins = sTrades.filter(t => t.pnl > 0);
        const sPnl = sTrades.reduce((acc, t) => acc + t.pnl, 0);
        const sComm = sTrades.reduce((acc, t) => acc + (t.commission || 0), 0);
        const sContracts = sTrades.reduce((acc, t) => acc + (t.vol || 1), 0);
        const sColor = sPnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
        const sSign = sPnl >= 0 ? '+' : '-';
        const sWr = sTrades.length > 0 ? ((sWins.length / sTrades.length) * 100).toFixed(1) : '0.0';
        const tag = sPnl >= 0
          ? '<span class="text-[10px] px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-bold">正收益</span>'
          : '<span class="text-[10px] px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 font-bold">負收益</span>';

        const symCard = document.createElement('div');
        symCard.className = "p-3.5 rounded-xl border border-[#1e2638] bg-[#0b0e14] flex flex-col justify-between";
        symCard.innerHTML = `
          <div class="flex items-center justify-between">
            <span class="font-extrabold text-sm text-white">${sym} <span class="text-[10px] font-normal text-[#8b9bb4]">(${sContracts} 口 · 佣金 -$${sComm.toFixed(2)})</span></span>
            ${tag}
          </div>
          <div class="my-2">
            <span class="text-xl font-extrabold font-mono ${sColor}">${sSign}$${Math.abs(sPnl).toFixed(2)} USD</span>
          </div>
          <div class="text-xs text-[#8b9bb4] flex items-center justify-between pt-1 border-t border-[#1e2638]">
            <span>${sTrades.length} 戰 ${sWins.length} 勝</span>
            <span>勝率 <strong class="text-white">${sWr}%</strong></span>
          </div>
        `;
        symbolsContainer.appendChild(symCard);
      });

      // 4. Daily Net Cumulative P&L Curve
      renderCumulativePnlChart();

      // 5. Calendar Grid
      renderCalendar();
    }

    function renderCumulativePnlChart() {
      const container = document.getElementById('cumulative-pnl-chart');
      const badge = document.getElementById('cum-pnl-badge');
      if (!container) return;

      const filteredDays = Object.values(journalDb.days)
        .filter(d => currentOverviewScope === 'all' || d.date.startsWith(currentOverviewScope))
        .sort((a, b) => a.date.localeCompare(b.date));

      if (filteredDays.length === 0) {
        if (badge) {
          badge.innerText = '$0.00';
          badge.className = 'text-xs font-mono font-extrabold text-[#8b9bb4]';
        }
        container.innerHTML = `<div class="w-full h-full flex items-center justify-center text-xs text-[#8b9bb4]">該時段暫無交易數據</div>`;
        return;
      }

      const points = [{ date: 'Start', daily: 0, cum: 0, isOrigin: true }];
      let running = 0;
      filteredDays.forEach(d => {
        running = Math.round((running + d.net_pnl) * 100) / 100;
        points.push({ date: d.date, daily: d.net_pnl, cum: running, isOrigin: false });
      });

      const finalCum = points[points.length - 1].cum;
      if (badge) {
        badge.innerText = `${finalCum >= 0 ? '+' : '-'}$${Math.abs(finalCum).toFixed(2)}`;
        badge.className = `text-xs font-mono font-extrabold ${finalCum >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
      }

      const W = 460;
      const H = 210;
      const padL = 56;
      const padR = 16;
      const padT = 18;
      const padB = 30;
      const cw = W - padL - padR;
      const ch = H - padT - padB;

      let minVal = Math.min(0, ...points.map(p => p.cum));
      let maxVal = Math.max(0, ...points.map(p => p.cum));
      if (maxVal === minVal) { maxVal += 100; minVal -= 100; }
      const rangePad = (maxVal - minVal) * 0.12;
      minVal -= rangePad;
      maxVal += rangePad;

      const rawStep = (maxVal - minVal) / 4;
      const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
      const norm = rawStep / mag;
      const niceMult = norm <= 1 ? 1 : (norm <= 2 ? 2 : (norm <= 5 ? 5 : 10));
      const step = niceMult * mag;
      const yMin = Math.floor(minVal / step) * step;
      const yMax = Math.ceil(maxVal / step) * step;

      const xAt = (i) => padL + (i / Math.max(points.length - 1, 1)) * cw;
      const yAt = (v) => padT + ch - ((v - yMin) / Math.max(yMax - yMin, 1)) * ch;
      const yZero = yAt(0);

      const coords = points.map((p, i) => ({ ...p, x: xAt(i), y: yAt(p.cum) }));
      const linePath = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
      const areaPath = `${linePath} L${coords[coords.length - 1].x.toFixed(1)},${yZero.toFixed(1)} L${coords[0].x.toFixed(1)},${yZero.toFixed(1)} Z`;

      let svgParts = [];
      svgParts.push(`<svg viewBox="0 0 ${W} ${H}" class="w-full h-full select-none">`);
      svgParts.push(`
        <defs>
          <linearGradient id="cumGreenGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#10b981" stop-opacity="0.45"/>
            <stop offset="100%" stop-color="#10b981" stop-opacity="0.02"/>
          </linearGradient>
          <linearGradient id="cumRedGrad" x1="0" y1="1" x2="0" y2="0">
            <stop offset="0%" stop-color="#f43f5e" stop-opacity="0.48"/>
            <stop offset="100%" stop-color="#f43f5e" stop-opacity="0.03"/>
          </linearGradient>
          <clipPath id="clipAboveZero">
            <rect x="${padL}" y="${padT}" width="${cw}" height="${Math.max(0, yZero - padT).toFixed(1)}"/>
          </clipPath>
          <clipPath id="clipBelowZero">
            <rect x="${padL}" y="${yZero.toFixed(1)}" width="${cw}" height="${Math.max(0, padT + ch - yZero).toFixed(1)}"/>
          </clipPath>
        </defs>
      `);

      for (let v = yMin; v <= yMax + step * 0.01; v += step) {
        const y = yAt(v);
        if (y < padT - 2 || y > padT + ch + 2) continue;
        const isZero = Math.abs(v) < 1e-6;
        const label = v < 0 ? `-$${Math.abs(Math.round(v)).toLocaleString()}` : `$${Math.round(v).toLocaleString()}`;
        svgParts.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="${isZero ? '#334155' : '#1e2638'}" stroke-width="${isZero ? '1.2' : '1'}" stroke-dasharray="${isZero ? '0' : '3 3'}"/>`);
        svgParts.push(`<text x="${padL - 8}" y="${(y + 3.5).toFixed(1)}" fill="${isZero ? '#cbd5e1' : '#8b9bb4'}" font-size="10" font-family="monospace" text-anchor="end">${label}</text>`);
      }

      svgParts.push(`<path d="${areaPath}" fill="url(#cumGreenGrad)" clip-path="url(#clipAboveZero)"/>`);
      svgParts.push(`<path d="${areaPath}" fill="url(#cumRedGrad)" clip-path="url(#clipBelowZero)"/>`);
      svgParts.push(`<path d="${linePath}" fill="none" stroke="#818cf8" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`);

      const dataCoords = coords.slice(1);
      const tickCount = Math.min(4, dataCoords.length);
      const tickIndices = new Set();
      if (tickCount === 1) {
        tickIndices.add(0);
      } else {
        for (let k = 0; k < tickCount; k++) {
          tickIndices.add(Math.round(k * (dataCoords.length - 1) / (tickCount - 1)));
        }
      }
      tickIndices.forEach(idx => {
        const c = dataCoords[idx];
        const parts = c.date.split('-');
        const shortLabel = `${parts[1]}/${parts[2]}`;
        svgParts.push(`<text x="${c.x.toFixed(1)}" y="${H - 9}" fill="#8b9bb4" font-size="10" font-family="monospace" text-anchor="middle">${shortLabel}</text>`);
      });

      dataCoords.forEach((c) => {
        const dotColor = c.cum >= 0 ? '#10b981' : '#f43f5e';
        const dSign = c.daily >= 0 ? '+' : '-';
        const cSign = c.cum >= 0 ? '+' : '-';
        svgParts.push(`
          <g class="cum-pt-group" style="cursor:pointer;"
             onmouseenter="showCumTooltip(event, '${c.date}', '${dSign}$${Math.abs(c.daily).toFixed(2)}', '${cSign}$${Math.abs(c.cum).toFixed(2)}', ${c.daily >= 0}, ${c.cum >= 0})"
             onmouseleave="hideCumTooltip()"
             onclick="loadDate('${c.date}'); showView('daily');">
            <circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="8" fill="transparent"/>
            <circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="2.8" fill="${dotColor}" stroke="#0b0e14" stroke-width="1"/>
          </g>
        `);
      });

      svgParts.push(`</svg>`);
      svgParts.push(`<div id="cum-tooltip" class="hidden pointer-events-none absolute top-2 left-2 bg-[#121824]/95 border border-[#2b354d] rounded-lg px-2.5 py-1.5 text-[11px] font-mono shadow-xl z-20"></div>`);
      container.innerHTML = svgParts.join('');
    }

    function showCumTooltip(evt, dateStr, dailyStr, cumStr, isDailyWin, isCumWin) {
      const tip = document.getElementById('cum-tooltip');
      if (!tip) return;
      tip.innerHTML = `
        <div class="text-white font-bold">${dateStr}</div>
        <div class="text-[#8b9bb4]">單日: <span class="${isDailyWin ? 'text-emerald-400' : 'text-rose-400'} font-bold">${dailyStr}</span></div>
        <div class="text-[#8b9bb4]">累計: <span class="${isCumWin ? 'text-emerald-400' : 'text-rose-400'} font-bold">${cumStr}</span></div>
      `;
      tip.classList.remove('hidden');
    }

    function hideCumTooltip() {
      const tip = document.getElementById('cum-tooltip');
      if (tip) tip.classList.add('hidden');
    }

    function renderCalendar() {
      const grid = document.getElementById('calendar-grid');
      grid.innerHTML = '';

      const [yearStr, monthStr] = currentCalendarMonth.split('-');
      const year = parseInt(yearStr, 10);
      const month = parseInt(monthStr, 10);
      document.getElementById('calendar-month-title').innerText = `${MONTH_NAMES_EN[month - 1]} ${year}`;

      const firstWeekday = new Date(year, month - 1, 1).getDay(); // 0=Sun..6=Sat
      const daysInMonth = new Date(year, month, 0).getDate();

      // Leading empty cells for weekday alignment
      for (let i = 0; i < firstWeekday; i++) {
        const empty = document.createElement('div');
        empty.className = "cal-cell p-2.5 rounded-xl border border-[#1e2638]/40 bg-[#0b0e14]/40 opacity-30";
        grid.appendChild(empty);
      }

      for (let day = 1; day <= daysInMonth; day++) {
        const dayStr = day < 10 ? `0${day}` : `${day}`;
        const dateKey = `${currentCalendarMonth}-${dayStr}`;
        const dayData = journalDb.days[dateKey];

        const cell = document.createElement('div');

        if (dayData) {
          const isWin = dayData.is_win;
          const bgClass = isWin
            ? 'bg-emerald-500/10 border-emerald-500/40 hover:bg-emerald-500/20 text-emerald-400'
            : 'bg-rose-500/10 border-rose-500/40 hover:bg-rose-500/20 text-rose-400';
          const pnlText = isWin ? `+$${dayData.net_pnl.toFixed(2)}` : `-$${Math.abs(dayData.net_pnl).toFixed(2)}`;

          cell.className = `cal-cell active-day p-3 rounded-xl border flex flex-col justify-between ${bgClass}`;
          cell.onclick = () => {
            loadDate(dateKey);
            showView('daily');
          };

          cell.innerHTML = `
            <div class="flex items-center justify-between">
              <span class="font-extrabold text-sm text-white">${day}</span>
            </div>
            <div class="my-1.5 space-y-0.5">
              <span class="text-base font-extrabold block font-mono tracking-tight">${pnlText}</span>
              <span class="text-[11px] text-gray-200 font-sans font-semibold block">勝率 ${dayData.win_rate}%</span>
              <span class="text-[11px] text-gray-200 font-sans font-semibold block">${dayData.trades_count} 單</span>
            </div>
            <div class="text-[10px] text-[#8b9bb4] font-sans flex items-center justify-between gap-1 pt-1 border-t border-current/20 whitespace-nowrap">
              <span>${dayData.win_count}勝 ${dayData.loss_count}負</span>
              <span class="text-blue-400 underline">覆盤 ➔</span>
            </div>
          `;
        } else {
          cell.className = "cal-cell p-2.5 rounded-xl border border-[#1e2638] bg-[#0d121c] opacity-60 flex flex-col justify-between";
          cell.innerHTML = `
            <div class="text-left font-bold text-xs text-[#8b9bb4]">${day}</div>
            <div class="text-center text-[11px] text-[#425068]">--</div>
            <div class="text-[10px] text-transparent">--</div>
          `;
        }
        grid.appendChild(cell);
      }
    }

    // ==========================================
    // 4. DAILY DETAIL VIEW
    // ==========================================
    function stepTradingDate(delta) {
      const idx = allDatesSorted.indexOf(currentSelectedDate);
      if (idx === -1) return;
      const nextIdx = idx + delta;
      if (nextIdx >= 0 && nextIdx < allDatesSorted.length) {
        loadDate(allDatesSorted[nextIdx]);
      }
    }

    function jumpToLatestDate() {
      if (allDatesSorted.length > 0) {
        loadDate(allDatesSorted[allDatesSorted.length - 1]);
      }
    }

    let miniCalMonth = currentSelectedDate ? currentSelectedDate.substring(0, 7) : DEFAULT_MONTH;

    function toggleDateDropdown(forceState) {
      const menu = document.getElementById('date-dropdown-menu');
      if (!menu) return;
      const shouldOpen = typeof forceState === 'boolean' ? forceState : menu.classList.contains('hidden');
      if (shouldOpen) {
        miniCalMonth = currentSelectedDate ? currentSelectedDate.substring(0, 7) : DEFAULT_MONTH;
        renderMiniCalendar();
        menu.classList.remove('hidden');
      } else {
        menu.classList.add('hidden');
      }
    }

    function stepMiniCalMonth(delta) {
      const [y, m] = miniCalMonth.split('-').map(Number);
      const d = new Date(y, m - 1 + delta, 1);
      miniCalMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
      renderMiniCalendar();
    }

    function renderMiniCalendar() {
      const titleEl = document.getElementById('mini-cal-title');
      const grid = document.getElementById('mini-cal-grid');
      if (!grid) return;

      const [yStr, mStr] = miniCalMonth.split('-');
      const y = parseInt(yStr, 10);
      const m = parseInt(mStr, 10);
      if (titleEl) titleEl.innerText = `${MONTH_NAMES_EN[m - 1]} ${y}`;

      grid.innerHTML = '';
      const firstDay = new Date(y, m - 1, 1).getDay();
      const daysInMonth = new Date(y, m, 0).getDate();

      for (let i = 0; i < firstDay; i++) {
        grid.appendChild(document.createElement('div'));
      }

      for (let day = 1; day <= daysInMonth; day++) {
        const dKey = `${miniCalMonth}-${String(day).padStart(2, '0')}`;
        const d = journalDb.days[dKey];
        const isSelected = dKey === currentSelectedDate;

        const btn = document.createElement('button');
        if (d) {
          const isWin = d.is_win;
          const pnlStr = isWin ? `+$${d.net_pnl.toFixed(2)}` : `-$${Math.abs(d.net_pnl).toFixed(2)}`;
          btn.title = `${dKey} (${d.day_of_week}) · ${pnlStr} · ${d.trades_count}單 · 勝率 ${d.win_rate}%`;
          const baseColor = isWin
            ? 'bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400'
            : 'bg-rose-500/15 hover:bg-rose-500/25 text-rose-400';
          const barColor = isWin ? 'bg-emerald-400' : 'bg-rose-400';
          const ringClass = isSelected ? 'ring-2 ring-blue-400 font-extrabold' : 'font-bold';

          btn.className = `relative h-8 rounded-lg flex flex-col items-center justify-center transition-all cursor-pointer ${baseColor} ${ringClass}`;
          btn.innerHTML = `
            <span class="leading-none">${day}</span>
            <span class="w-4 h-[2.5px] rounded-full ${barColor} mt-0.5"></span>
          `;
          btn.onclick = (e) => {
            e.stopPropagation();
            toggleDateDropdown(false);
            loadDate(dKey);
          };
        } else {
          btn.disabled = true;
          btn.className = 'h-8 rounded-lg flex items-center justify-center text-[#4b5563] cursor-default';
          btn.innerText = day;
        }
        grid.appendChild(btn);
      }
    }

    document.addEventListener('click', (e) => {
      const menu = document.getElementById('date-dropdown-menu');
      const btn = document.getElementById('btn-date-center');
      if (menu && btn && !menu.contains(e.target) && !btn.contains(e.target)) {
        menu.classList.add('hidden');
      }
    });

    function renderDateSelectorPills() {
      const idx = allDatesSorted.indexOf(currentSelectedDate);
      const dayData = journalDb.days[currentSelectedDate];
      if (!dayData) return;

      const labelEl = document.getElementById('date-stepper-label');
      const pnlEl = document.getElementById('date-stepper-pnl');
      const prevBtn = document.getElementById('btn-date-prev');
      const nextBtn = document.getElementById('btn-date-next');
      const latestBtn = document.getElementById('btn-latest-date');

      if (labelEl) labelEl.innerText = `${currentSelectedDate} (${dayData.day_of_week})`;
      if (pnlEl) {
        pnlEl.innerText = dayData.is_win ? `+$${dayData.net_pnl.toFixed(0)}` : `-$${Math.abs(dayData.net_pnl).toFixed(0)}`;
        pnlEl.className = dayData.is_win ? 'text-xs font-extrabold text-emerald-400' : 'text-xs font-extrabold text-rose-400';
      }
      if (prevBtn) prevBtn.disabled = (idx <= 0);
      if (nextBtn) nextBtn.disabled = (idx >= allDatesSorted.length - 1);
      if (latestBtn) {
        const isLatest = idx === allDatesSorted.length - 1;
        latestBtn.className = isLatest
          ? 'px-3 py-1.5 rounded-xl bg-[#1e2638] text-white text-xs font-semibold border border-blue-500/40 transition-all flex-shrink-0'
          : 'px-3 py-1.5 rounded-xl bg-[#121824] hover:bg-[#1e2638] text-[#8b9bb4] hover:text-white text-xs font-semibold border border-[#1e2638] transition-all flex-shrink-0';
      }
    }

    function loadDate(dateKey) {
      currentSelectedDate = dateKey;
      const dayData = journalDb.days[dateKey];
      if (!dayData) return;

      selectedSymbols.clear();
      selectedWinLoss = 'all';
      selectedSession = 'all';
      document.getElementById('btn-filter-all').className = 'filter-pill px-2 py-1 rounded bg-[#1e2638] text-white';
      document.getElementById('btn-filter-win').className = 'filter-pill px-2 py-1 rounded text-[#8b9bb4]';
      document.getElementById('btn-filter-loss').className = 'filter-pill px-2 py-1 rounded text-[#8b9bb4]';

      renderDateSelectorPills();

      const pnlFormatted = dayData.is_win ? `+$${dayData.net_pnl.toFixed(2)}` : `-$${Math.abs(dayData.net_pnl).toFixed(2)}`;
      document.getElementById('daily-header-title').innerText = `${dateKey} (${dayData.day_of_week}) 交易詳細覆盤`;
      document.getElementById('daily-header-subtitle').innerText = `${dayData.trades_count} 筆交易 ｜ 淨盈虧: ${pnlFormatted} USD (佣金 -$${(dayData.commission || 0).toFixed(2)}) ｜ 勝率: ${dayData.win_rate}%`;

      const pnlColor = dayData.is_win ? 'text-emerald-400' : 'text-rose-400';
      const pfText = dayData.profit_factor >= 999 ? '∞' : dayData.profit_factor;
      const payoffText = dayData.payoff >= 999 ? '∞' : `${dayData.payoff} : 1`;

      document.getElementById('daily-kpi-container').innerHTML = `
        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-xl p-2.5 shadow-sm">
          <div class="text-[10px] font-medium text-[#8b9bb4]">單日淨盈虧 (Net P&L)</div>
          <div class="text-lg sm:text-xl font-bold font-mono mt-0.5 ${pnlColor}">${pnlFormatted}</div>
          <div class="text-[10px] text-[#8b9bb4]">${dayData.trades_count} 筆 ｜ 佣金 -$${(dayData.commission || 0).toFixed(2)}</div>
        </div>
        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-xl p-2.5 shadow-sm">
          <div class="text-[10px] font-medium text-[#8b9bb4]">真實勝率 (Win Rate)</div>
          <div class="text-lg sm:text-xl font-bold font-mono mt-0.5 text-white">${dayData.win_rate}%</div>
          <div class="text-[10px] text-[#8b9bb4]">${dayData.win_count} 勝 / ${dayData.loss_count} 負</div>
        </div>
        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-xl p-2.5 shadow-sm">
          <div class="text-[10px] font-medium text-[#8b9bb4]">獲利因子 (Profit Factor)</div>
          <div class="text-lg sm:text-xl font-bold font-mono mt-0.5 text-white">${pfText}</div>
          <div class="text-[10px] text-[#8b9bb4]">${dayData.profit_factor >= 1.0 ? '健康正期望值' : '需控制回撤'}</div>
        </div>
        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-xl p-2.5 shadow-sm">
          <div class="text-[10px] font-medium text-[#8b9bb4]">盈虧比 (Payoff Ratio)</div>
          <div class="text-lg sm:text-xl font-bold font-mono mt-0.5 text-white">${payoffText}</div>
          <div class="text-[10px] text-[#8b9bb4]">均盈 / 均損</div>
        </div>
        <div class="metric-card bg-[#121824] border border-[#1e2638] rounded-xl p-2.5 shadow-sm col-span-2 sm:col-span-1">
          <div class="text-[10px] font-medium text-[#8b9bb4]">交易執行風格</div>
          <div class="text-lg sm:text-xl font-bold font-mono mt-0.5 text-amber-400">Futures</div>
          <div class="text-[10px] text-[#8b9bb4]">MGC / MNQ 微型期貨</div>
        </div>
      `;

      renderSessionChips(dayData.trades);
      renderSymbolFilters(dayData.trades);
      applyFilters(true);
    }

    function renderSessionChips(trades) {
      const sessions = [
        { key: 'asia', label: '亞盤' },
        { key: 'london', label: '歐盤' },
        { key: 'premarket', label: 'PreMarket' },
        { key: 'ny', label: '美盤' },
        { key: 'other', label: '其它' }
      ];

      const container = document.getElementById('session-chips-container');
      container.innerHTML = '';

      const allChip = document.createElement('div');
      allChip.id = 'chip-session-all';
      allChip.className = `session-chip px-2.5 py-1 rounded-lg text-xs font-semibold border ${selectedSession === 'all' ? 'active-session border-blue-500 bg-blue-500/10 text-white' : 'border-[#1e2638] bg-[#0b0e14] text-[#8b9bb4]'}`;
      allChip.onclick = () => setSessionFilter('all');
      allChip.innerHTML = `<span>全部時段 (${trades.length})</span>`;
      container.appendChild(allChip);

      sessions.forEach(s => {
        const sTrades = trades.filter(t => t.session_code === s.key);
        if (sTrades.length === 0) return;

        const pnl = sTrades.reduce((sum, t) => sum + t.pnl, 0);
        const pnlColor = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
        const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
        const wins = sTrades.filter(t => t.isWin).length;
        const wr = Math.round((wins / sTrades.length) * 100);

        const chip = document.createElement('div');
        chip.id = `chip-session-${s.key}`;
        const isActive = selectedSession === s.key;
        chip.className = `session-chip px-2.5 py-1 rounded-lg text-xs border flex items-center gap-1.5 ${isActive ? 'active-session border-blue-500 bg-blue-500/10' : 'border-[#1e2638] bg-[#0b0e14]'}`;
        chip.onclick = () => setSessionFilter(s.key);

        chip.innerHTML = `
          <span class="font-bold text-white">${s.label}</span>
          <span class="text-[10px] text-[#8b9bb4]">(${sTrades.length}單)</span>
          <span class="font-mono font-bold ${pnlColor}">${pnlStr}</span>
          <span class="text-[10px] px-1 rounded bg-[#121824] text-[#8b9bb4]">${wr}%</span>
        `;
        container.appendChild(chip);
      });
    }

    function renderSymbolFilters(trades) {
      const container = document.getElementById('symbol-filters-container');
      container.innerHTML = '';

      const symbols = [...new Set(trades.map(t => t.sym))];

      const allBtn = document.createElement('button');
      allBtn.className = `filter-pill px-2.5 py-1 rounded-lg text-xs font-semibold ${selectedSymbols.size === 0 ? 'bg-[#1e2638] text-white border border-blue-500/40' : 'bg-[#0b0e14] text-[#8b9bb4] border border-[#1e2638]'}`;
      allBtn.innerText = '全部品種';
      allBtn.onclick = () => {
        selectedSymbols.clear();
        renderSymbolFilters(trades);
        applyFilters(true);
      };
      container.appendChild(allBtn);

      symbols.forEach(sym => {
        const isSelected = selectedSymbols.has(sym);
        const symColor = sym === 'MGC' ? 'text-amber-400' : (sym === 'MNQ' ? 'text-indigo-400' : 'text-blue-400');
        const btn = document.createElement('button');
        btn.className = `filter-pill px-2.5 py-1 rounded-lg text-xs font-bold flex items-center gap-1 ${isSelected ? 'bg-blue-500/20 text-white border border-blue-500' : 'bg-[#0b0e14] border border-[#1e2638]'}`;
        btn.innerHTML = `<span class="${symColor}">${sym}</span>`;
        btn.onclick = () => {
          if (selectedSymbols.has(sym)) selectedSymbols.delete(sym);
          else selectedSymbols.add(sym);
          renderSymbolFilters(trades);
          applyFilters(true);
        };
        container.appendChild(btn);
      });
    }

    function setWinLossFilter(type) {
      selectedWinLoss = type;
      document.getElementById('btn-filter-all').className = `filter-pill px-2 py-1 rounded ${type === 'all' ? 'bg-[#1e2638] text-white' : 'text-[#8b9bb4]'}`;
      document.getElementById('btn-filter-win').className = `filter-pill px-2 py-1 rounded ${type === 'win' ? 'bg-emerald-500/20 text-emerald-400 font-bold' : 'text-[#8b9bb4]'}`;
      document.getElementById('btn-filter-loss').className = `filter-pill px-2 py-1 rounded ${type === 'loss' ? 'bg-rose-500/20 text-rose-400 font-bold' : 'text-[#8b9bb4]'}`;
      applyFilters(true);
    }

    function setSessionFilter(sessionKey) {
      selectedSession = selectedSession === sessionKey ? 'all' : sessionKey;
      const dayData = journalDb.days[currentSelectedDate];
      if (dayData) renderSessionChips(dayData.trades);
      applyFilters(true);
    }

    function applyFilters(reselect = true) {
      const dayData = journalDb.days[currentSelectedDate];
      if (!dayData) return;

      let filtered = dayData.trades;
      if (selectedSymbols.size > 0) filtered = filtered.filter(t => selectedSymbols.has(t.sym));
      if (selectedWinLoss === 'win') filtered = filtered.filter(t => t.isWin);
      else if (selectedWinLoss === 'loss') filtered = filtered.filter(t => !t.isWin);
      if (selectedSession !== 'all') filtered = filtered.filter(t => t.session_code === selectedSession);

      document.getElementById('filtered-count-badge').innerText = `${filtered.length}筆`;
      renderTable(filtered);

      if (reselect) {
        if (filtered.length > 0) {
          if (!filtered.some(t => t.id === currentTradeId)) {
            currentTradeId = filtered[0].id;
          }
          selectTrade(currentTradeId);
        } else {
          document.getElementById('chart-viewport').innerHTML = `<div class="p-8 text-center text-gray-400 text-xs">沒有符合目前篩選條件的交易</div>`;
        }
      }
    }

    function renderTable(trades) {
      const tbody = document.getElementById('trade-table-body');
      tbody.innerHTML = '';

      const reviewsDb = getReviewsDb();

      trades.forEach(t => {
        const tr = document.createElement('tr');
        tr.id = `row-${t.id}`;
        tr.className = `trade-row ${t.id === currentTradeId ? 'active-row' : ''}`;
        tr.onclick = () => selectTrade(t.id);

        const pnlFormatted = t.pnl >= 0 ? `+$${t.pnl.toFixed(2)}` : `-$${Math.abs(t.pnl).toFixed(2)}`;
        const pnlClass = t.pnl >= 0 ? 'text-emerald-400 font-extrabold text-sm' : 'text-rose-400 font-extrabold text-sm';
        const sideBadge = t.side === 'BUY'
          ? '<span class="px-2 py-0.5 rounded font-bold text-xs bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">BUY</span>'
          : '<span class="px-2 py-0.5 rounded font-bold text-xs bg-blue-500/10 text-blue-400 border border-blue-500/20">SELL</span>';
        const symColor = t.sym === 'MGC' ? 'text-amber-400' : (t.sym === 'MNQ' ? 'text-indigo-400' : 'text-blue-400');
        const volBadge = t.vol > 1 ? `<span class="text-[10px] text-[#8b9bb4] ml-0.5">x${t.vol}</span>` : '';

        const revKey = getTradeKey(currentSelectedDate, t.id);
        const tRev = reviewsDb[revKey];
        let reviewBadge = '';
        if (tRev) {
          if (tRev.rating > 0) {
            reviewBadge += `<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30 ml-1">★${tRev.rating}</span>`;
          }
          if (tRev.comment && tRev.comment.length > 0) {
            reviewBadge += `<span class="text-blue-400 text-xs ml-0.5" title="${tRev.comment.replace(/"/g, '&quot;')}">💬</span>`;
          }
        }

        tr.innerHTML = `
          <td class="py-3 px-3 text-[#8b9bb4] font-bold text-xs">
            <span class="inline-flex items-center">${t.id}${reviewBadge}</span>
          </td>
          <td class="py-3 px-2 font-sans text-white text-xs font-medium">${t.in_time.substr(0, 5)}</td>
          <td class="py-3 px-2 font-extrabold ${symColor} text-xs">${t.sym}${volBadge}</td>
          <td class="py-3 px-2 font-sans">${sideBadge}</td>
          <td class="py-3 px-2 text-right text-gray-200 text-xs">${t.in_price}</td>
          <td class="py-3 px-2 text-right text-gray-200 text-xs">${t.out_price}</td>
          <td class="py-3 px-3 text-right ${pnlClass}">${pnlFormatted}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    function selectTrade(tradeId) {
      currentTradeId = tradeId;
      const dayData = journalDb.days[currentSelectedDate];
      if (!dayData) return;
      const trade = dayData.trades.find(t => t.id === tradeId);
      if (!trade) return;

      document.querySelectorAll('.trade-row').forEach(r => r.classList.remove('active-row'));
      const row = document.getElementById(`row-${tradeId}`);
      if (row) row.classList.add('active-row');

      document.getElementById('viewer-trade-badge').innerText = `Trade #${trade.id} (${trade.contract || trade.sym} x${trade.vol})`;
      document.getElementById('viewer-trade-title').innerText = `${trade.sym} ${trade.side} 訂單多週期 K 線 (TradingView 真實行情)`;
      document.getElementById('viewer-trade-subtitle').innerText = `進場: ${trade.in_time} @ ${trade.in_price} ➔ 出場: ${trade.out_time} @ ${trade.out_price} ｜ 淨盈虧: ${trade.pnl >= 0 ? '+' : '-'}$${Math.abs(trade.pnl).toFixed(2)} (毛 ${trade.gross_pnl >= 0 ? '+' : '-'}$${Math.abs(trade.gross_pnl).toFixed(2)}, 佣 -$${trade.commission.toFixed(2)}) ｜ ${trade.session_name}`;

      const pnlColor = trade.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
      document.getElementById('viewer-stats-bar').innerHTML = `
        <div class="p-2 sm:p-2.5 rounded-lg bg-[#0b0e14] border border-[#1e2638]">
          <span class="text-[#8b9bb4] block text-[10px]">進場點位 / 時間</span>
          <span class="font-bold text-white text-xs sm:text-sm">${trade.in_price} (${trade.in_time})</span>
        </div>
        <div class="p-2 sm:p-2.5 rounded-lg bg-[#0b0e14] border border-[#1e2638]">
          <span class="text-[#8b9bb4] block text-[10px]">出場點位 / 時間</span>
          <span class="font-bold text-white text-xs sm:text-sm">${trade.out_price} (${trade.out_time})</span>
        </div>
        <div class="p-2 sm:p-2.5 rounded-lg bg-[#0b0e14] border border-[#1e2638]">
          <span class="text-[#8b9bb4] block text-[10px]">波幅差額 / 口數</span>
          <span class="font-bold text-white text-xs sm:text-sm">${trade.pts_diff} pts (${trade.vol} 口)</span>
        </div>
        <div class="p-2 sm:p-2.5 rounded-lg bg-[#0b0e14] border border-[#1e2638]">
          <span class="text-[#8b9bb4] block text-[10px]">淨收益 (已扣佣 -$${trade.commission.toFixed(2)})</span>
          <span class="font-bold font-mono text-xs sm:text-sm ${pnlColor}">${trade.pnl >= 0 ? '+' : '-'}$${Math.abs(trade.pnl).toFixed(2)} USD</span>
        </div>
      `;

      const matchedPlan = getPlanForTrade(currentSelectedDate, trade.sym);
      const planBtn = document.getElementById('btn-tf-plan');
      const lvBtn = document.getElementById('btn-toggle-levels');
      if (matchedPlan && matchedPlan.charts && matchedPlan.charts.length > 0) {
        currentPlanChartIdx = matchedPlan.charts.length - 1;
        if (planBtn) planBtn.innerHTML = `<span>📐 計劃 (${matchedPlan.charts.length}圖)</span>`;
        if (lvBtn) lvBtn.style.opacity = (matchedPlan.levels && matchedPlan.levels.length > 0) ? '1' : '0.45';
      } else {
        if (planBtn) planBtn.innerHTML = `<span class="opacity-50">📐 無計劃</span>`;
        if (lvBtn) lvBtn.style.opacity = '0.35';
      }

      renderViewport();
      loadReviewForCurrentTrade();
    }

    function switchActiveTf(tf) {
      currentTf = tf;
      document.querySelectorAll('.tf-btn').forEach(b => {
        b.className = "tf-btn px-2.5 py-1 rounded-lg text-[#8b9bb4] hover:text-white";
      });
      document.getElementById(`btn-tf-${tf}`).className = "tf-btn px-2.5 py-1 rounded-lg bg-[#1e2638] text-blue-400 font-bold shadow-xs";
      renderViewport();
    }

    // ==========================================
    // 5. SVG CHART ENGINE
    // ==========================================
    function renderTradeSvg(trade, tf) {
      const symbol = trade.sym;
      const tfKey = tf === '1m' ? 'M1' : (tf === '5m' ? 'M5' : 'M15');
      const allBars = journalDb.bars[symbol] ? journalDb.bars[symbol][tfKey] : [];

      if (!allBars || allBars.length === 0) {
        return `
          <div class="p-8 text-center text-gray-400 font-sans text-xs space-y-2">
            <div class="text-amber-400 font-bold">暫無 ${symbol} ${tf} 的 K 線數據</div>
            <div class="text-gray-500">可嘗試切換上方 5m 週期或 15m 週期查看</div>
          </div>`;
      }

      const earliestBarTs = allBars[0][0];
      const latestBarTs = allBars[allBars.length - 1][0];

      if (trade.out_ts < earliestBarTs - 1800000) {
        return `
          <div class="p-8 text-center text-gray-400 font-sans text-xs space-y-2">
            <div class="text-amber-400 font-bold">該訂單時間 (${currentSelectedDate} ${trade.in_time}) 超出 TradingView ${symbol} ${tf} 歷史深度</div>
            <div class="text-gray-400">目前 ${symbol} ${tf} 最早記錄為 ${new Date(earliestBarTs).toLocaleDateString('zh-HK', { timeZone: 'Asia/Hong_Kong' })}</div>
            <div class="text-blue-400 underline cursor-pointer" onclick="switchActiveTf('5m')">➔ 立即切換至 5m 週期查看完整 K 線</div>
          </div>`;
      }

      if (trade.in_ts > latestBarTs + 1800000) {
        return `
          <div class="p-8 text-center text-gray-400 font-sans text-xs space-y-2">
            <div class="text-amber-400 font-bold">該訂單時間 (${trade.in_time}) 晚於目前快取的 ${symbol} ${tf} 範圍</div>
          </div>`;
      }

      let exitIdx = allBars.findIndex(b => b[0] >= trade.out_ts);
      if (exitIdx === -1) exitIdx = allBars.length - 1;

      let endIdx = Math.min(allBars.length, exitIdx + 15);
      let startIdx = Math.max(0, endIdx - 120);
      const bars = allBars.slice(startIdx, endIdx);

      if (bars.length === 0) {
        return `<div class="p-8 text-center text-gray-400 font-sans text-xs">未能切片該訂單時段之 K 線數據</div>`;
      }

      const width = 900;
      const height = 480;
      const margin_top = 70;
      const margin_bottom = 50;
      const margin_left = 65;
      const margin_right = 85;
      const chart_w = width - margin_left - margin_right;
      const chart_h = height - margin_top - margin_bottom;

      let min_p = Math.min(...bars.map(b => b[3]), trade.in_price, trade.out_price);
      let max_p = Math.max(...bars.map(b => b[2]), trade.in_price, trade.out_price);
      const pad = Math.max((max_p - min_p) * 0.15, 0.5);
      min_p -= pad;
      max_p += pad;
      const p_range = max_p - min_p;

      function priceToY(p) {
        return margin_top + chart_h - ((p - min_p) / p_range * chart_h);
      }

      const bar_slot = chart_w / Math.max(bars.length, 1);
      const bar_w = Math.max(bar_slot * 0.70, 2.0);

      function tsToX(ts) {
        for (let i = 0; i < bars.length - 1; i++) {
          if (bars[i][0] <= ts && ts <= bars[i + 1][0]) {
            const ratio = (ts - bars[i][0]) / Math.max(bars[i + 1][0] - bars[i][0], 1);
            return margin_left + (i + ratio) * bar_slot + bar_slot / 2;
          }
        }
        if (ts <= bars[0][0]) return margin_left + bar_slot / 2;
        return margin_left + (bars.length - 1) * bar_slot + bar_slot / 2;
      }

      let svg = [];
      svg.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" height="100%" style="background:#131722; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; border-radius:10px;">`);

      for (let i = 0; i <= 5; i++) {
        const pr = min_p + (p_range / 5) * i;
        const y = priceToY(pr);
        svg.push(`<line x1="${margin_left}" y1="${y.toFixed(1)}" x2="${width - margin_right}" y2="${y.toFixed(1)}" stroke="#1e222d" stroke-width="1" stroke-dasharray="3 3"/>`);
        svg.push(`<text x="${width - margin_right + 8}" y="${(y + 4).toFixed(1)}" fill="#787b86" font-size="10" font-family="monospace">${pr.toFixed(2)}</text>`);
      }

      const label_step = Math.max(1, Math.floor(bars.length / 6));
      for (let i = 0; i < bars.length; i += label_step) {
        const x = margin_left + i * bar_slot + bar_slot / 2;
        const d = new Date(bars[i][0]);
        const time_str = d.toLocaleTimeString('en-GB', { timeZone: 'Asia/Hong_Kong', hour: '2-digit', minute: '2-digit', hour12: false });
        svg.push(`<line x1="${x.toFixed(1)}" y1="${margin_top}" x2="${x.toFixed(1)}" y2="${height - margin_bottom}" stroke="#1e222d" stroke-width="1" stroke-dasharray="3 3"/>`);
        svg.push(`<text x="${x.toFixed(1)}" y="${height - margin_bottom + 18}" fill="#787b86" font-size="10" text-anchor="middle" font-family="monospace">${time_str}</text>`);
      }

      bars.forEach((b, i) => {
        const [ts, o, h, l, c] = b;
        const x_center = margin_left + i * bar_slot + bar_slot / 2;
        const is_up = c >= o;
        const color = is_up ? '#089981' : '#f23645';

        const y_h = priceToY(h);
        const y_l = priceToY(l);
        const y_o = priceToY(o);
        const y_c = priceToY(c);

        const body_top = Math.min(y_o, y_c);
        const body_h = Math.max(Math.abs(y_c - y_o), 1.0);

        svg.push(`<line x1="${x_center.toFixed(1)}" y1="${y_h.toFixed(1)}" x2="${x_center.toFixed(1)}" y2="${y_l.toFixed(1)}" stroke="${color}" stroke-width="1.2"/>`);
        svg.push(`<rect x="${(x_center - bar_w / 2).toFixed(1)}" y="${body_top.toFixed(1)}" width="${bar_w.toFixed(1)}" height="${body_h.toFixed(1)}" fill="${color}" stroke="${color}" stroke-width="0.5"/>`);
      });

      if (showPlanLevelsOnChart) {
        const plan = getPlanForTrade(currentSelectedDate, trade.sym);
        if (plan && plan.levels) {
          plan.levels.forEach(lv => {
            if (lv.price >= min_p && lv.price <= max_p) {
              const ly = priceToY(lv.price);
              const dash = lv.style === 'dotted' ? '2 3' : (lv.style === 'dashed' ? '6 4' : '0');
              svg.push(`<line x1="${margin_left}" y1="${ly.toFixed(1)}" x2="${width - margin_right}" y2="${ly.toFixed(1)}" stroke="${lv.color}" stroke-width="1.3" stroke-dasharray="${dash}" opacity="0.85"/>`);
              const lblW = lv.label.length * 6.2 + 12;
              svg.push(`<rect x="${margin_left + 4}" y="${(ly - 14).toFixed(1)}" width="${lblW.toFixed(0)}" height="14" rx="3" fill="#0b0e14" fill-opacity="0.85" stroke="${lv.color}" stroke-width="0.6"/>`);
              svg.push(`<text x="${margin_left + 10}" y="${(ly - 4).toFixed(1)}" fill="${lv.color}" font-size="9.5" font-weight="bold" font-family="monospace">${lv.label}</text>`);
            }
          });
        }
      }

            const in_x = tsToX(trade.in_ts);
      const out_x = tsToX(trade.out_ts);
      const in_y = priceToY(trade.in_price);
      const out_y = priceToY(trade.out_price);

      const is_win = trade.pnl >= 0;
      const trade_color = is_win ? '#089981' : '#f23645';

      svg.push(`<line x1="${in_x.toFixed(1)}" y1="${margin_top}" x2="${in_x.toFixed(1)}" y2="${height - margin_bottom}" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4 3"/>`);
      svg.push(`<line x1="${out_x.toFixed(1)}" y1="${margin_top}" x2="${out_x.toFixed(1)}" y2="${height - margin_bottom}" stroke="${trade_color}" stroke-width="1.5" stroke-dasharray="4 3"/>`);

      svg.push(`<circle cx="${in_x.toFixed(1)}" cy="${in_y.toFixed(1)}" r="4.5" fill="#f59e0b" stroke="#ffffff" stroke-width="1.5"/>`);
      svg.push(`<circle cx="${out_x.toFixed(1)}" cy="${out_y.toFixed(1)}" r="4.5" fill="${trade_color}" stroke="#ffffff" stroke-width="1.5"/>`);

      svg.push(`<line x1="${margin_left}" y1="${in_y.toFixed(1)}" x2="${in_x.toFixed(1)}" y2="${in_y.toFixed(1)}" stroke="#f59e0b" stroke-width="1" stroke-dasharray="2 2" opacity="0.6"/>`);
      svg.push(`<line x1="${Math.min(in_x, out_x).toFixed(1)}" y1="${out_y.toFixed(1)}" x2="${Math.max(in_x, out_x).toFixed(1)}" y2="${out_y.toFixed(1)}" stroke="${trade_color}" stroke-width="1" stroke-dasharray="2 2" opacity="0.6"/>`);

      const in_badge_text = `${trade.side} @ ${trade.in_price}`;
      const in_badge_w = Math.max(in_badge_text.length * 7 + 16, 95);
      const in_badge_x = Math.max(margin_left + 5, Math.min(width - margin_right - in_badge_w - 5, in_x - in_badge_w / 2));
      svg.push(`<g>
        <rect x="${in_badge_x.toFixed(1)}" y="18" width="${in_badge_w}" height="22" rx="4" fill="#f59e0b" fill-opacity="0.95"/>
        <text x="${(in_badge_x + in_badge_w / 2).toFixed(1)}" y="33" fill="#000000" font-size="11" font-weight="bold" text-anchor="middle" font-family="monospace">${in_badge_text}</text>
      </g>`);

      const pnl_sign = trade.pnl >= 0 ? '+$' : '-$';
      const out_badge_text = `EXIT: ${pnl_sign}${Math.abs(trade.pnl).toFixed(2)}`;
      const out_badge_w = Math.max(out_badge_text.length * 7 + 16, 105);
      const out_badge_x = Math.max(margin_left + 5, Math.min(width - margin_right - out_badge_w - 5, out_x - out_badge_w / 2));
      svg.push(`<g>
        <rect x="${out_badge_x.toFixed(1)}" y="${height - 38}" width="${out_badge_w}" height="22" rx="4" fill="${trade_color}" fill-opacity="0.95"/>
        <text x="${(out_badge_x + out_badge_w / 2).toFixed(1)}" y="${height - 23}" fill="#ffffff" font-size="11" font-weight="bold" text-anchor="middle" font-family="monospace">${out_badge_text}</text>
      </g>`);

      svg.push(`<text x="${width - margin_right}" y="33" fill="#8b9bb4" font-size="11" font-weight="bold" text-anchor="end" font-family="monospace">${trade.contract || trade.sym} #${trade.id} [${tf}]</text>`);

      svg.push(`</svg>`);
      return svg.join('');
    }

    function renderViewport() {
      const dayData = journalDb.days[currentSelectedDate];
      if (!dayData) return;
      const trade = dayData.trades.find(t => t.id === currentTradeId);
      if (!trade) return;
      const vp = document.getElementById('chart-viewport');

      if (currentTf === 'plan') {
        vp.innerHTML = renderPlanViewer(trade);
      } else if (currentTf === 'all') {
        vp.innerHTML = `
          <div class="w-full h-full overflow-y-auto space-y-4 p-2">
            <div>${renderTradeSvg(trade, '15m')}</div>
            <div>${renderTradeSvg(trade, '5m')}</div>
            <div>${renderTradeSvg(trade, '1m')}</div>
          </div>
        `;
      } else {
        vp.innerHTML = renderTradeSvg(trade, currentTf);
      }
    }

    // Init App
    renderMonthSwitcher();
    renderMonthlyOverview();
    renderDateSelectorPills();
    if (currentSelectedDate) loadDate(currentSelectedDate);
  </script>

  <!-- Fullscreen Lightbox for Notion TV Plan Charts -->
  <div id="plan-lightbox" onclick="closePlanLightbox()" class="hidden fixed inset-0 bg-black/90 z-50 flex flex-col items-center justify-center p-4 cursor-zoom-out">
    <div class="w-full max-w-6xl flex items-center justify-between mb-2 text-xs text-gray-300">
      <span id="lightbox-caption" class="font-bold text-white"></span>
      <span class="px-2.5 py-1 rounded-lg bg-white/10 text-gray-200">點擊任意位置關閉 (ESC)</span>
    </div>
    <img id="lightbox-img" src="" class="max-w-full max-h-[88vh] rounded-xl border border-[#2b354d] shadow-2xl object-contain" />
  </div>
</body>
</html>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    orders = load_filled_orders()
    trades, remaining_queues = match_trades(orders)

    open_rem = {k: len(v) for k, v in remaining_queues.items() if v}
    if open_rem:
        print(f"Warning: remaining unmatched open lots: {open_rem}")

    with open(BARS_CACHE_PATH, "r", encoding="utf-8") as f:
        bars = json.load(f)

    journal_db = build_journal_db(trades, bars)

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(journal_db, f, ensure_ascii=False, indent=2)

    compact_json = json.dumps(journal_db, ensure_ascii=False, separators=(",", ":"))
    plans_db = {}
    if os.path.exists(PLANS_INDEX_PATH):
        with open(PLANS_INDEX_PATH, "r", encoding="utf-8") as pf:
            plans_db = json.load(pf)
    compact_plans = json.dumps(plans_db, ensure_ascii=False, separators=(",", ":"))
    html_out = HTML_TEMPLATE.replace("__JOURNAL_DB_JSON__", compact_json).replace("__DAILY_PLANS_JSON__", compact_plans)
    with open(HTML_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    s = journal_db["summary"]
    print("=== Tradovate Dashboard Build Summary ===")
    print(f"Trading Days  : {s['trading_days']} ({s['win_days']}W / {s['loss_days']}L)")
    print(f"Total Trades  : {s['total_trades']} ({s['win_trades']}W / {s['loss_trades']}L), Total Contracts: {s['total_contracts']}")
    print(f"Gross P&L     : ${s['total_gross_pnl']:+.2f}")
    print(f"Est. Comm     : -${s['total_commission']:.2f}")
    print(f"Net P&L       : ${s['total_pnl']:+.2f}")
    print(f"Months        : {', '.join(s['months'])}")
    print(f"Data JSON     : {DATA_JSON_PATH} ({os.path.getsize(DATA_JSON_PATH)/1024/1024:.2f} MB)")
    print(f"Dashboard HTML: {HTML_OUT_PATH} ({os.path.getsize(HTML_OUT_PATH)/1024/1024:.2f} MB)")


if __name__ == "__main__":
    main()
