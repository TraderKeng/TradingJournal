#!/usr/bin/env python3
"""
Safe Unified Trading Journal Sync & Publish Helper
1. Updates cTrader `index.html` embedded `journalDb` and `dailyPlansDb` in-place
   (STRICTLY preserving all custom UI: arrow steppers, cumulative P&L curve,
   mini calendar popover, Notion Plan viewer, Follow System & ICT tags).
2. Rebuilds Tradovate `tradovate/index.html` via `scripts/build_tradovate_dashboard.py`.
3. Commits and pushes any changes to GitHub Pages (`origin main`).
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HKT = timezone(timedelta(hours=8))
REPO_DIR = "/Users/lamkenneth/Claude/Trading Journal"
CTRADER_DATA_PATH = os.path.join(REPO_DIR, "data_journal.json")
CTRADER_HTML_PATH = os.path.join(REPO_DIR, "index.html")
PLANS_INDEX_PATH = os.path.join(REPO_DIR, "plans", "plans_index.json")
TRADOVATE_BUILDER = os.path.join(REPO_DIR, "scripts", "build_tradovate_dashboard.py")


def update_ctrader_html_in_place():
    if not os.path.exists(CTRADER_HTML_PATH) or not os.path.exists(CTRADER_DATA_PATH):
        print("Skipping cTrader HTML update (missing files).")
        return

    with open(CTRADER_DATA_PATH, "r", encoding="utf-8") as f:
        journal_db = json.load(f)
    compact_journal = json.dumps(journal_db, ensure_ascii=False, separators=(",", ":"))

    plans_db = {}
    if os.path.exists(PLANS_INDEX_PATH):
        with open(PLANS_INDEX_PATH, "r", encoding="utf-8") as f:
            plans_db = json.load(f)
    compact_plans = json.dumps(plans_db, ensure_ascii=False, separators=(",", ":"))

    with open(CTRADER_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # Replace ONLY the two embedded JSON lines, never touching HTML/CSS/JS UI
    new_html, n1 = re.subn(
        r"^(    const journalDb = )\{.*?\};$",
        lambda m: m.group(1) + compact_journal + ";",
        html,
        count=1,
        flags=re.MULTILINE,
    )
    if n1 != 1:
        raise RuntimeError(f"Expected 1 journalDb line in index.html, found {n1}")

    new_html, n2 = re.subn(
        r"^(    const dailyPlansDb = )\{.*?\};$",
        lambda m: m.group(1) + compact_plans + ";",
        new_html,
        count=1,
        flags=re.MULTILINE,
    )
    if n2 != 1:
        raise RuntimeError(f"Expected 1 dailyPlansDb line in index.html, found {n2}")

    if new_html != html:
        with open(CTRADER_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(new_html)
        print("Updated cTrader index.html data in-place (UI preserved).")
    else:
        print("cTrader index.html already up to date.")


def rebuild_tradovate():
    if not os.path.exists(TRADOVATE_BUILDER):
        print("Skipping Tradovate rebuild (builder not found).")
        return
    res = subprocess.run(
        [sys.executable, TRADOVATE_BUILDER],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Tradovate builder failed:\n{res.stderr}")
    print(res.stdout.strip())


def git_publish():
    def run_git(cmd):
        return subprocess.run(cmd, cwd=REPO_DIR, capture_output=True, text=True)

    run_git(["git", "add", "data_journal.json", "index.html", "tradovate/", "plans/", "Tradovate Report/", "scripts/"])
    st = run_git(["git", "status", "--porcelain"]).stdout.strip()
    if not st:
        print("No changes to commit. GitHub Pages is up to date.")
        return

    now_str = datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")
    msg = f"chore: automated daily journal sync ({now_str})"
    c = run_git(["git", "commit", "-m", msg])
    if c.returncode != 0:
        raise RuntimeError(f"git commit failed: {c.stderr}")
    p = run_git(["git", "push", "origin", "main"])
    if p.returncode != 0:
        raise RuntimeError(f"git push failed: {p.stderr}")
    print(f"Committed & pushed to GitHub Pages: {msg}")


def main():
    print(f"[{datetime.now(HKT).isoformat()}] Running unified safe dashboard sync...")
    update_ctrader_html_in_place()
    rebuild_tradovate()
    if "--no-push" not in sys.argv:
        git_publish()


if __name__ == "__main__":
    main()
