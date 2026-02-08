#!/usr/bin/env python3
"""Aggregate logs/oos_validation_*.json into a CSV summary and a short Markdown report."""

import json
from pathlib import Path
import csv
import datetime

LOGS = Path("logs")
OUT_CSV = LOGS / "oos_validation_summary.csv"
OUT_MD = Path("docs") / "oos_validation_summary.md"

files = sorted(LOGS.glob("oos_validation_*.json"))
if not files:
    print("no oos_validation_*.json files found in logs/")
    raise SystemExit(1)

rows = []
all_keys = set()
for p in files:
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print("failed parse", p, e)
        continue
    # flatten simple top-level keys
    row = {k: j.get(k) for k in j.keys()}
    row["json_file"] = str(p.name)
    rows.append(row)
    all_keys.update(row.keys())

all_keys = [k for k in sorted(all_keys) if k != "json_file"]

# write CSV
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
    writer = csv.writer(fh)
    header = ["json_file"] + all_keys
    writer.writerow(header)
    for r in rows:
        writer.writerow([r.get(c, "") for c in header])

# write short Markdown summary
OUT_MD.parent.mkdir(parents=True, exist_ok=True)
now = datetime.datetime.now().isoformat()
top_rows = sorted(rows, key=lambda x: float(x.get("best_final") or 0), reverse=True)[:10]
with OUT_MD.open("w", encoding="utf-8") as fh:
    fh.write(f"# OOS 验证汇总\n\n生成时间: {now}\n\n")
    fh.write("## Top 10 (by best_final)\n\n")
    fh.write(
        "| json_file | symbol | best_final | best_pnl | best_drawdown_pct | baseline_final | baseline_pnl | baseline_drawdown_pct |\n"
    )
    fh.write("|---|---|---:|---:|---:|---:|---:|---:|\n")
    for r in top_rows:
        fh.write(
            "| {} | {} | {} | {} | {} | {} | {} | {} |\n".format(
                r.get("json_file", ""),
                r.get("symbol", ""),
                r.get("best_final", ""),
                r.get("best_pnl", ""),
                r.get("best_drawdown_pct", ""),
                r.get("baseline_final", ""),
                r.get("baseline_pnl", ""),
                r.get("baseline_drawdown_pct", ""),
            )
        )

    fh.write("\n")
    fh.write("生成的验证文件数量: {}\n".format(len(rows)))

print("WROTE:", OUT_CSV)
print("WROTE:", OUT_MD)
