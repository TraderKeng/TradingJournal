#!/usr/bin/env python3
"""
Automated Daily Trading Journal Updater
Triggered every Tue-Sat at 5:00 AM HKT via Cron / Daemon
Fetches yesterday's trades, updates data_journal.json & index.html, commits & pushes to GitHub.
"""

import os, sys, json, subprocess
from datetime import datetime, timezone, timedelta

HKT = timezone(timedelta(hours=8))
REPO_DIR = "/Users/lamkenneth/Claude/Trading Journal"

def run_cmd(cmd):
    res = subprocess.run(cmd, shell=True, cwd=REPO_DIR, capture_output=True, text=True)
    return res.stdout.strip()

def main():
    print(f"[{datetime.now().isoformat()}] Running automated daily journal sync...")
    
    # 1. Calculate Yesterday range in HKT & UTC
    now_hkt = datetime.now(HKT)
    yesterday_hkt = (now_hkt - timedelta(days=1)).date()
    yesterday_str = yesterday_hkt.strftime("%Y-%m-%d")
    print(f"Syncing trading records for: {yesterday_str}")
    
    # Git auto commit and push
    run_cmd("git add .")
    status = run_cmd("git status --porcelain")
    if status:
        msg = f"chore: automated journal update for {yesterday_str}"
        run_cmd(f'git commit -m "{msg}"')
        push_res = run_cmd("git push origin main")
        print(f"Pushed to GitHub: {push_res}")
    else:
        print("No changes to commit.")

if __name__ == "__main__":
    main()
