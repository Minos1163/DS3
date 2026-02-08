import sys

from datetime import datetime
import csv
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, TypeAlias
import argparse


def parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            raise argparse.ArgumentTypeError(f"无法解析时间: {s}")


RowsByKey: TypeAlias = Dict[Tuple[str, str], List[Dict[str, Any]]]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="计算每个方向的最严重浮亏时间段")
    parser.add_argument(
        "--in",
        dest="infile",
        default=r"d:\\AIDCA\\AIBOT\\logs\\dca_dashboard.csv",
        help="输入 dca_dashboard CSV 文件路径",
    )
    parser.add_argument(
        "--out", dest="outfile", default=r"d:\\AIDCA\\AIBOT\\logs\\worst_periods_by_side.csv", help="输出 CSV 路径"
    )
    parser.add_argument(
        "--start",
        dest="start",
        type=parse_dt,
        default=datetime(2026, 2, 5, 18, 0, 0),
        help="开始时间（ISO 或 yyyy-mm-dd HH:MM:SS）",
    )
    parser.add_argument(
        "--end",
        dest="end",
        type=parse_dt,
        default=datetime(2026, 2, 5, 21, 24, 0),
        help="结束时间（ISO 或 yyyy-mm-dd HH:MM:SS）",
    )
    parser.add_argument(
        "--float-places", dest="float_places", type=int, default=6, help="输出浮点数固定小数位数（默认 6）"
    )
    parser.add_argument(
        "--missing-num", dest="missing_num", type=float, default=-1.0, help="数值字段缺失时的占位符（默认 -1）"
    )
    args = parser.parse_args(argv)

    path: str = args.infile
    start: datetime = args.start
    end: datetime = args.end
    out_path: str = args.outfile

    # load rows per (symbol, side)
    rows_by_key: RowsByKey = defaultdict(list)
    rows: int = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_raw = row.get("timestamp") or row.get("time")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except Exception:
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
            if not (start <= ts <= end):
                continue
            rows += 1
            symbol = row.get("symbol") or "UNKNOWN"
            side = (row.get("side") or "UNKNOWN").upper()
            pnl_raw = row.get("pnl_percent") or row.get("pnl_pct")
            # 跳过空值以避免 Pylance 报错（None 不能传给 float）
            if pnl_raw is None or pnl_raw == "":
                continue
            try:
                pnl = float(pnl_raw)
            except Exception:
                continue
            # safe parse entry_price, mark_price, dca_count
            entry_price_raw = row.get("entry_price")
            mark_price_raw = row.get("mark_price")
            dca_count_raw = row.get("dca_count")

            def to_float_safe(v: Optional[Any]) -> Optional[float]:
                if v is None or v == "":
                    return None
                try:
                    return float(v)
                except Exception:
                    return None

            def to_int_safe(v: Optional[Any]) -> Optional[int]:
                if v is None or v == "":
                    return None
                try:
                    return int(float(v))
                except Exception:
                    return None

            entry_price = to_float_safe(entry_price_raw)
            mark_price = to_float_safe(mark_price_raw)
            dca_count = to_int_safe(dca_count_raw)

            key = (symbol, side)
            rows_by_key[key].append(
                {
                    "ts": ts,
                    "pnl": pnl,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "dca_count": dca_count,
                    "entry_time": row.get("entry_time"),
                }
            )

    # compute for each (symbol,side): min pnl and its index, then expand contiguous negative region
    results_by_side = defaultdict(list)
    for (symbol, side), recs in rows_by_key.items():
        recs.sort(key=lambda x: x["ts"])
        # find min pnl and its index
        min_idx = None
        min_pnl = 0
        for i, r in enumerate(recs):
            if min_idx is None or r["pnl"] < min_pnl:
                min_idx = i
                min_pnl = r["pnl"]
        if min_idx is None:
            continue
        # expand left
        i = min_idx
        while i - 1 >= 0 and recs[i - 1]["pnl"] < 0:
            i -= 1
        start_ts = recs[i]["ts"]
        # expand right
        j = min_idx
        while j + 1 < len(recs) and recs[j + 1]["pnl"] < 0:
            j += 1
        end_ts = recs[j]["ts"]
        duration = end_ts - start_ts
        min_rec = recs[min_idx]
        results_by_side[side].append(
            {
                "symbol": symbol,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "min_pnl": min_pnl,
                "min_ts": min_rec["ts"],
                "duration_s": int(duration.total_seconds()),
                "entry_price": min_rec["entry_price"],
                "mark_price": min_rec["mark_price"],
                "dca_count": min_rec["dca_count"],
            }
        )

    # pick worst per side by min_pnl (most negative)
    final = []
    for side, items in results_by_side.items():
        if not items:
            continue
        worst = min(items, key=lambda x: x["min_pnl"])
        final.append((side, worst))

    # write CSV
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "side",
                "symbol",
                "start_ts",
                "end_ts",
                "min_pnl",
                "min_ts",
                "duration_s",
                "entry_price",
                "mark_price",
                "dca_count",
            ]
        )
        float_places: int = args.float_places
        missing_num: float = args.missing_num
        fmt = "{:.%df}" % (float_places,)
        for side, wobj in sorted(final):
            # 将 None 标准化为 missing_num（数值字段），并格式化浮点数为固定小数位
            entry_price_val = missing_num if wobj["entry_price"] is None else wobj["entry_price"]
            mark_price_val = missing_num if wobj["mark_price"] is None else wobj["mark_price"]
            dca_count_out = int(missing_num) if wobj["dca_count"] is None else int(wobj["dca_count"])
            entry_price_out = (
                fmt.format(entry_price_val) if isinstance(entry_price_val, (int, float)) else str(entry_price_val)
            )
            mark_price_out = (
                fmt.format(mark_price_val) if isinstance(mark_price_val, (int, float)) else str(mark_price_val)
            )
            min_pnl_out = (
                fmt.format(wobj["min_pnl"]) if isinstance(wobj["min_pnl"], (int, float)) else str(wobj["min_pnl"])
            )
            w.writerow(
                [
                    side,
                    wobj["symbol"],
                    wobj["start_ts"].isoformat(),
                    wobj["end_ts"].isoformat(),
                    min_pnl_out,
                    wobj["min_ts"].isoformat(),
                    wobj["duration_s"],
                    entry_price_out,
                    mark_price_out,
                    dca_count_out,
                ]
            )

    # print summary
    print("ROWS_IN_WINDOW", rows)
    for side, wobj in sorted(final):
        print("SIDE", side)
        print("  SYMBOL", wobj["symbol"])
        print(
            "  PERIOD",
            wobj["start_ts"].isoformat(),
            "->",
            wobj["end_ts"].isoformat(),
            "duration_s=",
            wobj["duration_s"],
        )
        print("  MIN_PNL", wobj["min_pnl"], "at", wobj["min_ts"].isoformat())
        print("  ENTRY_PRICE", wobj["entry_price"], "MARK_PRICE", wobj["mark_price"], "DCA", wobj["dca_count"])

    print("\nCSV saved to", out_path)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("脚本执行出错:", e)
        raise
