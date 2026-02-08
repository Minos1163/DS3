#!/usr/bin/env python3
import csv
from pathlib import Path

f = Path("logs/oos_validation_summary.csv")
if not f.exists():
    print("missing summary CSV")
    raise SystemExit(2)

bad = []
total = 0
with f.open(encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    for r in reader:
        total += 1
        bf = r.get("best_final", "").strip()
        if bf == "":
            bad.append((r.get("json_file"), "missing best_final"))
            continue
        try:
            float(bf)
        except Exception:
            bad.append((r.get("json_file"), f"non-numeric best_final:{bf}"))

print("total rows:", total)
print("bad rows:", len(bad))
for b in bad[:20]:
    print(*b)
