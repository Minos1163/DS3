#!/usr/bin/env python3
"""Run only high-priority tasks from logs/oos_oos_tasks.csv."""

import csv
import subprocess
import sys
from pathlib import Path

CSV_PATH = Path("logs") / "oos_oos_tasks.csv"

if not CSV_PATH.exists():
    print("Missing:", CSV_PATH)
    sys.exit(2)

with CSV_PATH.open("r", encoding="utf-8", newline="") as fh:
    reader = csv.DictReader(fh)
    for row in reader:
        if row.get("priority", "").lower() != "high":
            continue
        cmd = row.get("cmd", "").strip()
        symbol = row.get("symbol")
        stop = row.get("stop_loss_pct")
        pos = row.get("position_percent")
        print(f"Running: {symbol} stop={stop} pos={pos}")
        # cmd may be quoted in CSV; remove surrounding quotes
        if cmd.startswith('"') and cmd.endswith('"'):
            cmd = cmd[1:-1]
        # Execute command via shell so Windows .venv\Scripts\python.exe works as-is
        rc = subprocess.call(cmd, shell=True)
        if rc != 0:
            print(f"Command failed ({rc}): {cmd}")
            sys.exit(rc)

print("All high-priority tasks completed.")
