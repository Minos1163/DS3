#!/usr/bin/env python3
"""
自动小时报告：
1) 评分低于阈值平仓占比
2) 平均持仓 bars
3) TREND 开仓占比
4) 净收益（close pnl 求和）
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Set


SCORE_CLOSE_KEY = "评分低于阈值"
ENTRY_ENGINE_RE = re.compile(r"entry_engine=([A-Z_]+)")
FALLBACK_ENGINE_RE = re.compile(r"(?:^|[^\w])engine=([A-Z_]+)")


@dataclass
class OpenEvent:
    timestamp: datetime
    symbol: str
    side: str
    engine: str


@dataclass
class CloseEvent:
    timestamp: datetime
    symbol: str
    side: str
    reason: str
    pnl: float
    hold_bars: Optional[float]


@dataclass
class LogSource:
    kind: str  # trade_log | dashboard
    trade_log_path: Optional[Path] = None
    dashboard_files: Optional[List[Path]] = None


@dataclass
class HourStat:
    hour: datetime
    open_count: int = 0
    trend_open_count: int = 0
    close_count: int = 0
    score_close_count: int = 0
    net_pnl: float = 0.0
    hold_bars_sum: float = 0.0
    hold_bars_count: int = 0

    @property
    def score_close_ratio(self) -> Optional[float]:
        if self.close_count <= 0:
            return None
        return self.score_close_count / self.close_count

    @property
    def avg_hold_bars(self) -> Optional[float]:
        if self.hold_bars_count <= 0:
            return None
        return self.hold_bars_sum / self.hold_bars_count

    @property
    def trend_open_share(self) -> Optional[float]:
        if self.open_count <= 0:
            return None
        return self.trend_open_count / self.open_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按小时输出：评分平仓占比 / 平均持仓bars / TREND占比 / 净收益"
    )
    parser.add_argument(
        "--log-dir",
        default="LOGS",
        help="日志目录（可直接传 YYYY-MM 目录或其上级目录，默认 LOGS）",
    )
    parser.add_argument(
        "--trade-log",
        default=None,
        help="trade_log.csv 文件路径（优先级高于 --log-dir）",
    )
    parser.add_argument(
        "--bar-minutes",
        type=int,
        default=5,
        help="K线周期分钟数，用于换算平均持仓 bars（默认 5）",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="仅输出最近 N 小时，默认 24",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="常驻运行，每小时自动输出上一完整小时",
    )
    parser.add_argument(
        "--output-csv",
        default=None,
        help="可选：将聚合结果写入 CSV（每次覆盖）",
    )
    return parser.parse_args()


def parse_timestamp(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def extract_entry_engine(reason: str) -> str:
    text = str(reason or "")
    m = ENTRY_ENGINE_RE.search(text)
    if m:
        return m.group(1).upper()
    m = FALLBACK_ENGINE_RE.search(text)
    if m:
        return m.group(1).upper()
    return "UNKNOWN"


def resolve_trade_log_path(log_dir: str, trade_log: Optional[str]) -> Path:
    if trade_log:
        path = Path(trade_log)
        if not path.exists():
            raise FileNotFoundError(f"trade_log 不存在: {path}")
        return path

    root = Path(log_dir)
    if root.is_file():
        return root

    candidates: List[Path] = []
    direct = root / "trade_log.csv"
    if direct.exists():
        candidates.append(direct)

    month_dir = root / datetime.now().strftime("%Y-%m") / "trade_log.csv"
    if month_dir.exists():
        candidates.append(month_dir)

    candidates.extend(root.glob("**/trade_log.csv"))
    if not candidates:
        raise FileNotFoundError(f"未找到 trade_log.csv: {root}")

    # 取最近修改的 trade_log.csv（兼容 logs-bak / 月目录）
    return max(set(candidates), key=lambda p: p.stat().st_mtime)


def resolve_dashboard_files(log_dir: str) -> List[Path]:
    root = Path(log_dir)
    if root.is_file() or not root.exists():
        return []
    files = list(root.glob("DCA_dashboard_*.csv"))
    files.extend(root.glob("**/DCA_dashboard_*.csv"))
    unique = sorted(set(files), key=lambda p: p.stat().st_mtime)
    return unique


def resolve_log_source(log_dir: str, trade_log: Optional[str]) -> LogSource:
    if trade_log:
        path = resolve_trade_log_path(log_dir=log_dir, trade_log=trade_log)
        return LogSource(kind="trade_log", trade_log_path=path)

    try:
        path = resolve_trade_log_path(log_dir=log_dir, trade_log=None)
        return LogSource(kind="trade_log", trade_log_path=path)
    except FileNotFoundError as trade_err:
        dashboard_files = resolve_dashboard_files(log_dir=log_dir)
        if dashboard_files:
            return LogSource(kind="dashboard", dashboard_files=dashboard_files)
        raise trade_err


def load_rows(trade_log_path: Path) -> List[dict]:
    with trade_log_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader if row]


def _queue_key(symbol: str, side: str) -> str:
    return f"{symbol}|{side}"


def _pop_open_event(
    queues: Dict[str, Deque[OpenEvent]], symbol: str, side: Optional[str] = None
) -> Optional[OpenEvent]:
    symbol = str(symbol or "").strip().upper()
    side_norm = str(side or "").strip().upper()
    if not symbol:
        return None

    if side_norm:
        key = _queue_key(symbol, side_norm)
        q = queues.get(key)
        if q:
            return q.popleft()

    prefix = f"{symbol}|"
    best_key: Optional[str] = None
    best_ts: Optional[datetime] = None
    for key, q in queues.items():
        if not key.startswith(prefix) or not q:
            continue
        ts = q[0].timestamp
        if best_ts is None or ts < best_ts:
            best_ts = ts
            best_key = key
    if best_key is None:
        return None
    return queues[best_key].popleft()


def build_events(rows: Iterable[dict], bar_minutes: int) -> tuple[List[OpenEvent], List[CloseEvent]]:
    bar_minutes = max(1, int(bar_minutes))
    parsed_rows: List[tuple[datetime, dict]] = []
    for row in rows:
        ts = parse_timestamp(row.get("timestamp", ""))
        if ts is None:
            continue
        parsed_rows.append((ts, row))
    parsed_rows.sort(key=lambda x: x[0])

    open_queues: Dict[str, Deque[OpenEvent]] = defaultdict(deque)
    opens: List[OpenEvent] = []
    closes: List[CloseEvent] = []

    for ts, row in parsed_rows:
        action = str(row.get("action", "")).strip().upper()
        symbol = str(row.get("symbol", "")).strip().upper()
        reason = str(row.get("reason", "") or "")

        if action in ("BUY_OPEN", "SELL_OPEN"):
            result = str(row.get("result", "")).strip().lower()
            if result and result not in ("success", "ok"):
                continue
            side = "LONG" if action == "BUY_OPEN" else "SHORT"
            ev = OpenEvent(
                timestamp=ts,
                symbol=symbol,
                side=side,
                engine=extract_entry_engine(reason),
            )
            opens.append(ev)
            if symbol and side:
                open_queues[_queue_key(symbol, side)].append(ev)
            continue

        if action != "CLOSE":
            continue

        close_side = str(row.get("position_side", "")).strip().upper()
        hold_bars: Optional[float] = None
        open_ev = _pop_open_event(open_queues, symbol=symbol, side=close_side)
        if open_ev is not None:
            hold_minutes = max(0.0, (ts - open_ev.timestamp).total_seconds() / 60.0)
            hold_bars = hold_minutes / bar_minutes

        closes.append(
            CloseEvent(
                timestamp=ts,
                symbol=symbol,
                side=close_side,
                reason=reason,
                pnl=to_float(row.get("pnl", 0.0), default=0.0),
                hold_bars=hold_bars,
            )
        )

    return opens, closes


def build_events_from_dashboard(
    dashboard_files: Iterable[Path], bar_minutes: int
) -> tuple[List[OpenEvent], List[CloseEvent]]:
    bar_minutes = max(1, int(bar_minutes))
    opens: List[OpenEvent] = []
    closes: List[CloseEvent] = []
    open_queues: Dict[str, Deque[OpenEvent]] = defaultdict(deque)
    seen: Set[tuple] = set()

    for file_path in sorted(dashboard_files):
        with file_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = parse_timestamp(row.get("timestamp", ""))
                if ts is None:
                    continue
                event_type = str(row.get("event_type", "")).strip().upper()
                if not event_type:
                    continue

                symbol = str(row.get("event_symbol", "") or row.get("symbol", "")).strip().upper()
                side = str(row.get("event_side", "")).strip().upper()
                if not side:
                    if event_type.endswith("_LONG"):
                        side = "LONG"
                    elif event_type.endswith("_SHORT"):
                        side = "SHORT"

                reason = str(row.get("event_reason", "") or "")
                status = str(row.get("event_status", "")).strip().lower()
                event_price = str(row.get("event_price", "") or "")
                event_pnl = str(row.get("event_pnl", "") or "")

                dedupe_key = (
                    ts.isoformat(),
                    event_type,
                    symbol,
                    side,
                    status,
                    event_price,
                    event_pnl,
                    reason,
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                if event_type in ("OPEN_LONG", "OPEN_SHORT"):
                    if status and status not in ("success", "ok"):
                        continue
                    engine = str(row.get("engine", "")).strip().upper() or extract_entry_engine(reason)
                    ev = OpenEvent(
                        timestamp=ts,
                        symbol=symbol,
                        side=side or ("LONG" if event_type == "OPEN_LONG" else "SHORT"),
                        engine=engine,
                    )
                    opens.append(ev)
                    if ev.symbol and ev.side:
                        open_queues[_queue_key(ev.symbol, ev.side)].append(ev)
                    continue

                if event_type not in ("CLOSE", "CLOSE_EXTERNAL"):
                    continue

                hold_bars: Optional[float] = None
                open_ev = _pop_open_event(open_queues, symbol=symbol, side=side)
                if open_ev is not None:
                    hold_minutes = max(0.0, (ts - open_ev.timestamp).total_seconds() / 60.0)
                    hold_bars = hold_minutes / bar_minutes

                closes.append(
                    CloseEvent(
                        timestamp=ts,
                        symbol=symbol,
                        side=side,
                        reason=reason,
                        pnl=to_float(event_pnl, default=0.0),
                        hold_bars=hold_bars,
                    )
                )

    return opens, closes


def aggregate_hourly(opens: Iterable[OpenEvent], closes: Iterable[CloseEvent]) -> List[HourStat]:
    stats: Dict[datetime, HourStat] = {}

    def get_hour_stat(hour: datetime) -> HourStat:
        if hour not in stats:
            stats[hour] = HourStat(hour=hour)
        return stats[hour]

    for ev in opens:
        s = get_hour_stat(floor_hour(ev.timestamp))
        s.open_count += 1
        if ev.engine == "TREND":
            s.trend_open_count += 1

    for ev in closes:
        s = get_hour_stat(floor_hour(ev.timestamp))
        s.close_count += 1
        if SCORE_CLOSE_KEY in ev.reason:
            s.score_close_count += 1
        s.net_pnl += ev.pnl
        if ev.hold_bars is not None:
            s.hold_bars_sum += ev.hold_bars
            s.hold_bars_count += 1

    return [stats[h] for h in sorted(stats.keys())]


def filter_recent(hours: List[HourStat], since_hours: int) -> List[HourStat]:
    if since_hours <= 0:
        return hours
    cutoff = datetime.now() - timedelta(hours=since_hours)
    return [h for h in hours if h.hour >= floor_hour(cutoff)]


def fmt_ratio(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def fmt_float(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def print_table(hours: List[HourStat]) -> None:
    if not hours:
        print("暂无可输出的小时数据。")
        return

    header = (
        "hour                | score_close_ratio | avg_hold_bars | "
        "trend_open_share | net_pnl   | closes | opens"
    )
    print(header)
    print("-" * len(header))
    for h in hours:
        print(
            f"{h.hour.strftime('%Y-%m-%d %H:00')} | "
            f"{fmt_ratio(h.score_close_ratio):>16} | "
            f"{fmt_float(h.avg_hold_bars, 2):>13} | "
            f"{fmt_ratio(h.trend_open_share):>16} | "
            f"{fmt_float(h.net_pnl, 5):>9} | "
            f"{h.close_count:>6} | "
            f"{h.open_count:>5}"
        )


def write_csv(path: Path, hours: List[HourStat]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "hour",
                "score_close_ratio",
                "avg_hold_bars",
                "trend_open_share",
                "net_pnl",
                "close_count",
                "open_count",
            ]
        )
        for h in hours:
            writer.writerow(
                [
                    h.hour.isoformat(),
                    "" if h.score_close_ratio is None else round(h.score_close_ratio, 6),
                    "" if h.avg_hold_bars is None else round(h.avg_hold_bars, 6),
                    "" if h.trend_open_share is None else round(h.trend_open_share, 6),
                    round(h.net_pnl, 10),
                    h.close_count,
                    h.open_count,
                ]
            )


def run_once(
    log_source: LogSource,
    bar_minutes: int,
    since_hours: int,
    output_csv: Optional[Path],
) -> List[HourStat]:
    hourly = compute_hourly(
        log_source=log_source,
        bar_minutes=bar_minutes,
        since_hours=since_hours,
    )
    print_table(hourly)
    if output_csv is not None:
        write_csv(output_csv, hourly)
    return hourly


def compute_hourly(log_source: LogSource, bar_minutes: int, since_hours: int) -> List[HourStat]:
    if log_source.kind == "trade_log":
        assert log_source.trade_log_path is not None
        rows = load_rows(log_source.trade_log_path)
        opens, closes = build_events(rows, bar_minutes=bar_minutes)
    else:
        dashboard_files = log_source.dashboard_files or []
        opens, closes = build_events_from_dashboard(dashboard_files, bar_minutes=bar_minutes)
    hourly = aggregate_hourly(opens, closes)
    return filter_recent(hourly, since_hours=since_hours)


def sleep_to_next_hour() -> None:
    now = datetime.now()
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    wait_seconds = max(3, int((next_hour - now).total_seconds()) + 1)
    time.sleep(wait_seconds)


def run_watch(
    log_source: LogSource,
    bar_minutes: int,
    since_hours: int,
    output_csv: Optional[Path],
) -> None:
    last_emitted: Optional[datetime] = None
    if log_source.kind == "trade_log":
        print(f"watch 模式启动，trade_log: {log_source.trade_log_path}")
    else:
        file_count = len(log_source.dashboard_files or [])
        print(f"watch 模式启动，dashboard 文件数: {file_count}")
    while True:
        hourly = compute_hourly(
            log_source=log_source,
            bar_minutes=bar_minutes,
            since_hours=since_hours,
        )
        if output_csv is not None:
            write_csv(output_csv, hourly)
        completed_hour = floor_hour(datetime.now()) - timedelta(hours=1)
        latest = [h for h in hourly if h.hour == completed_hour]
        if latest and (last_emitted is None or completed_hour > last_emitted):
            item = latest[0]
            print(
                "hourly> "
                f"{item.hour.strftime('%Y-%m-%d %H:00')} "
                f"score_close_ratio={fmt_ratio(item.score_close_ratio)} "
                f"avg_hold_bars={fmt_float(item.avg_hold_bars, 2)} "
                f"trend_open_share={fmt_ratio(item.trend_open_share)} "
                f"net_pnl={fmt_float(item.net_pnl, 5)}"
            )
            last_emitted = completed_hour
        sleep_to_next_hour()


def main() -> None:
    args = parse_args()
    log_source = resolve_log_source(args.log_dir, args.trade_log)
    output_csv = Path(args.output_csv) if args.output_csv else None

    if args.watch:
        run_watch(
            log_source=log_source,
            bar_minutes=args.bar_minutes,
            since_hours=args.since_hours,
            output_csv=output_csv,
        )
        return

    run_once(
        log_source=log_source,
        bar_minutes=args.bar_minutes,
        since_hours=args.since_hours,
        output_csv=output_csv,
    )


if __name__ == "__main__":
    main()
