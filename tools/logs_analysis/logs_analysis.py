#!/usr/bin/env python3
"""
Log analysis for trading/risk events in UTC.

This script scans provided runtime log files, filters lines within the last
N hours (default 6h) based on UTC timestamps, extracts simple trade actions
(BUY/SELL/OPEN/CLOSE) involving BTC/ETH and any risk-related events, and
exports a small Excel workbook with a summary and a trades sheet.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any

try:
    from openpyxl import Workbook
except Exception:  # pragma: no cover
    Workbook = None  # type: ignore


TIMESTAMP_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:Z| UTC)?")
TRADE_RE = re.compile(r"(?P<asset>BTC|BTCUSDT|ETH|ETHUSDT)"  # asset token
                      r".*?(?P<side>BUY|SELL|EXIT|OPEN|CLOSE|LONG|SHORT)"  # action
                      r".*?(?P<price>\d+\.?\d*)?\$?")
RISK_KEYWORDS = ["risk", "drawdown", "stop", "stoploss", "leverage", "position"]


def parse_ts(ts_str: str) -> datetime:
    # Normalize to timezone aware UTC datetime
    if ts_str.endswith("Z"):
        dt = datetime.fromisoformat(ts_str.rstrip("Z"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def extract_entries_from_line(line: str) -> Dict[str, Any] | None:
    m = TIMESTAMP_RE.search(line)
    if not m:
        return None
    ts = m.group("ts")
    ts_dt = parse_ts(ts)
    asset = None
    side = None
    price = None
    # crude extraction
    for a in ["BTC", "BTCUSDT", "ETH", "ETHUSDT"]:
        if a in line:
            asset = a
            break
    m2 = re.search(r"(BUY|SELL|EXIT|OPEN|CLOSE|LONG|SHORT)", line, re.IGNORECASE)
    if m2:
        side = m2.group(1).upper()
    mprice = re.search(r"(price|ppi|at|@|=)\s*(\d+\.?\d*)", line, re.IGNORECASE)
    if mprice:
        try:
            price = float(mprice.group(2))
        except Exception:
            price = None
    # Try to capture a possible order_id in the log line
    oid = None
    mo = re.search(r"(?:order[_-]?id|orderid)[:=]?\s*([A-Za-z0-9_-]+)", line, re.IGNORECASE)
    if mo:
        oid = mo.group(1)

    ent = {
        "ts": ts_dt,
        "asset": asset,
        "side": side,
        "price": price,
        "order_id": oid,
        "line": line.strip(),
    }
    return ent


def is_recent(ts: datetime, cutoff: datetime) -> bool:
    return ts >= cutoff


def analyze_log_files(log_paths: List[str], hours: int = 6) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entries: List[Dict[str, Any]] = []
    for p in log_paths:
        path = Path(p)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            ent = extract_entries_from_line(line)
            if ent and is_recent(ent["ts"], cutoff):
                entries.append(ent)
    return entries


def export_to_excel(rows: List[Dict[str, Any]], output_path: str) -> str:
    if Workbook is None:
        raise RuntimeError("openpyxl is not available in this environment")
    wb = Workbook()
    ws = wb.active
    ws.title = "Trades"
    ws.append(["timestamp_utc", "asset", "side", "price", "log_line"])
    for r in rows:
        ws.append([
            r.get("ts").strftime("%Y-%m-%d %H:%M:%S") if r.get("ts") else "",
            r.get("asset"),
            r.get("side"),
            r.get("price"),
            r.get("line"),
        ])
    summary = wb.create_sheet(title="Summary")
    summary.append(["total_entries", len(rows)])
    wb.save(output_path)
    return output_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log_paths", nargs="+", help="Paths to runtime log files")
    ap.add_argument("--hours", type=int, default=6, help="Hours back to scan (UTC)")
    ap.add_argument("--output", default="logs_history_6h.xlsx", help="Output Excel file path")
    args = ap.parse_args()

    rows = analyze_log_files(args.log_paths, hours=args.hours)
    out = export_to_excel(rows, args.output)
    print(f"Exported {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
