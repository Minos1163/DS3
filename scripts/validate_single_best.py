#!/usr/bin/env python3
"""Validate a single file with provided best config by reusing run_on_file from validate_best_config_oos.py
Usage: python scripts/validate_single_best.py --file data/...csv --stop_loss 0.015 --position 0.4
"""

import argparse
import json
from pathlib import Path
import os

sys_path = os.path.dirname(__file__)
try:
    from validate_best_config_oos import run_on_file
except Exception:
    # try package import
    from scripts.validate_best_config_oos import run_on_file


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    p.add_argument("--stop_loss", type=float, required=True)
    p.add_argument("--position", type=float, required=True)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    best_cfg = {"stop_loss_pct": args.stop_loss, "position_percent": args.position}
    res = run_on_file(args.file, best_cfg)
    if res is None:
        print("Validation failed or data load failed for", args.file)
        return 1
    out = args.out
    if out is None:
        stem = Path(args.file).stem.replace("data\\", "")
        out = f"logs/oos_validation_{stem}_{args.stop_loss:.3f}_{args.position:.3f}.json"
    Path("logs").mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)
    print("WROTE", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
