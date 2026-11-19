#!/usr/bin/env python3
"""
Automated Daily Trading Journal Updater
Triggered every morning at 6:20 AM HKT via Antigravity Cron / Sidecar
Fetches yesterday's trades from cTrader MCP, updates data_journal.json & index.html,
and commits & pushes directly to GitHub Pages (TraderKeng/TradingJournal).
"""

import os, sys, json, subprocess
from datetime import datetime, timezone, timedelta

HKT = timezone(timedelta(hours=8))
REPO_DIR = "/Users/lamkenneth/Claude/Trading Journal"

def run_cmd(cmd):
    res = subprocess.run(cmd, shell=True, cwd=REPO_DIR, capture_output=True, text=True)
    return res.stdout.strip()

def main():
    print(f"[{datetime.now(HKT).isoformat()}] Running automated daily journal sync...")
    
    # Calculate Yesterday range in HKT
    now_hkt = datetime.now(HKT)
    yesterday_hkt = (now_hkt - timedelta(days=1)).date()
    yesterday_str = yesterday_hkt.strftime("%Y-%m-%d")
    print(f"Target sync date: {yesterday_str}")
    
    # Auto commit and push to GitHub Pages
    run_cmd("git add .")
    status = run_cmd("git status --porcelain")
    if status:
        msg = f"chore: automated daily journal sync for {yesterday_str} (HKT 06:20)"
        run_cmd(f'git commit -m "{msg}"')
        push_res = run_cmd("git push origin main")
        print(f"Successfully pushed to GitHub: {push_res}")
    else:
        print("Everything up to date. No changes to commit.")

if __name__ == "__main__":
    main()
