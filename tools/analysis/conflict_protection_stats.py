#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional, Tuple


EXIT_EVENT_TYPES = {"exit", "close", "circuit_exit"}
REDUCE_EVENT_TYPES = {"reduce"}
STOP_EVENT_TYPES = EXIT_EVENT_TYPES | REDUCE_EVENT_TYPES


@dataclass
class PositionWindow:
    position_id: str
    symbol: str
    side: str
    open_ts: datetime
    close_ts: Optional[datetime]
    regime: str
    decision_source: str
    direction_neutral_trial_mode: str
    direction_neutral_trial_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze conflict protection behavior")
    parser.add_argument("--db", default=None, help="SQLite path; autodetect latest fund_flow_strategy.db when omitted")
    parser.add_argument("--risk-jsonl", default=None, help="risk_conflict_stats.jsonl path; defaults to logs/risk_conflict_stats.jsonl")
    parser.add_argument("--input", default=None, help="Normalized event stream (.jsonl/.json/.csv)")
    parser.add_argument("--output-dir", default=None, help="Optional output directory for CSV tables")
    parser.add_argument("--symbol", default=None, help="Optional single symbol filter")
    parser.add_argument("--since", default=None, help="Optional start timestamp (ISO8601)")
    parser.add_argument("--until", default=None, help="Optional end timestamp (ISO8601)")
    return parser.parse_args()


def parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_lower(value: Any, default: str = "") -> str:
    text = str(value or "").strip().lower()
    return text if text else default


def safe_upper(value: Any, default: str = "") -> str:
    text = str(value or "").strip().upper()
    return text if text else default


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    idx = (len(xs) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return xs[lo]
    frac = idx - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def ts_id_fragment(ts: datetime) -> str:
    try:
        return ts.strftime("%Y%m%d%H%M%S")
    except Exception:
        return "unknown_ts"


def detect_latest_db() -> Optional[Path]:
    candidates = list(Path("logs").glob("**/fund_flow_strategy.db"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def detect_risk_jsonl() -> Optional[Path]:
    candidate = Path("logs/risk_conflict_stats.jsonl")
    return candidate if candidate.exists() else None


def infer_close_side(reason: str) -> str:
    text = str(reason or "")
    if "平多" in text:
        return "LONG"
    if "平空" in text:
        return "SHORT"
    return ""


def derive_decision_triggers(metadata: Dict[str, Any], reason: str, raw_trigger_type: str) -> List[str]:
    triggers: List[str] = []
    decision_source = str(metadata.get("decision_source", "") or "").strip()
    mode = safe_lower(metadata.get("direction_neutral_trial_mode"), "none")
    if raw_trigger_type:
        triggers.append(str(raw_trigger_type).upper())
    if mode != "none" or bool(metadata.get("direction_neutral_trial_active", False)):
        triggers.append("NEUTRAL_TRIAL")
    if decision_source == "trend_both_trial":
        triggers.append("TREND_BOTH_TRIAL")
    reason_up = str(reason or "").upper()
    if "REVERSE_CLOSE" in reason_up or ("反转平" in str(reason or "")):
        triggers.append("REVERSE_CLOSE")
    if "BREAK_EVEN" in reason_up or "BREAKEVEN" in reason_up:
        triggers.append("BREAK_EVEN")
    if "TRAILING" in reason_up or "TIGHTEN" in reason_up:
        triggers.append("TRAILING")
    if "STOP_LOSS" in reason_up or "止损" in str(reason or ""):
        triggers.append("STOP_LOSS")
    return sorted(set(t for t in triggers if t))


def load_sqlite_events(db_path: Path) -> Tuple[List[Dict[str, Any]], List[PositionWindow]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT timestamp, symbol, operation, trigger_type, realized_pnl, decision_json
        FROM ai_decision_logs
        ORDER BY timestamp ASC, id ASC
        """
    ).fetchall()
    conn.close()

    events: List[Dict[str, Any]] = []
    positions: List[PositionWindow] = []
    active_by_symbol: Dict[str, PositionWindow] = {}

    for row in rows:
        ts = parse_ts(row["timestamp"])
        if ts is None:
            continue
        symbol = safe_upper(row["symbol"])
        operation = safe_upper(row["operation"])
        raw_trigger_type = str(row["trigger_type"] or "").strip()
        realized_pnl = to_float(row["realized_pnl"])
        try:
            decision_obj = json.loads(row["decision_json"] or "{}")
        except Exception:
            decision_obj = {}

        decision: Dict[str, Any] = decision_obj if isinstance(decision_obj, dict) else {}
        metadata_obj = decision.get("metadata")
        metadata: Dict[str, Any] = metadata_obj if isinstance(metadata_obj, dict) else {}
        reason = str(decision.get("reason") or "")
        regime = str(metadata.get("regime") or metadata.get("engine") or "")
        decision_source = str(metadata.get("decision_source") or "")
        trial_mode = safe_lower(metadata.get("direction_neutral_trial_mode"), "none")
        trial_reason = str(metadata.get("direction_neutral_trial_reason") or "")
        side = ""
        position_id = ""
        event_type = "decision"

        if operation == "BUY":
            side = "LONG"
            position_id = f"{symbol}_{side}_{ts.strftime('%Y%m%d%H%M%S')}"
            pw = PositionWindow(position_id, symbol, side, ts, None, regime, decision_source, trial_mode, trial_reason)
            active_by_symbol[symbol] = pw
            positions.append(pw)
            event_type = "open"
        elif operation == "SELL":
            side = "SHORT"
            position_id = f"{symbol}_{side}_{ts.strftime('%Y%m%d%H%M%S')}"
            pw = PositionWindow(position_id, symbol, side, ts, None, regime, decision_source, trial_mode, trial_reason)
            active_by_symbol[symbol] = pw
            positions.append(pw)
            event_type = "open"
        elif operation == "CLOSE":
            event_type = "exit"
            active = active_by_symbol.get(symbol)
            if active is not None:
                side = active.side
                position_id = active.position_id
                regime = active.regime or regime
                decision_source = active.decision_source or decision_source
                trial_mode = active.direction_neutral_trial_mode or trial_mode
                trial_reason = active.direction_neutral_trial_reason or trial_reason
                active.close_ts = ts
                active_by_symbol.pop(symbol, None)
            else:
                side = safe_upper(metadata.get("side")) or infer_close_side(reason) or "UNKNOWN"
                position_id = f"{symbol}_{side}_{ts.strftime('%Y%m%d%H%M%S')}"

        events.append(
            {
                "ts": ts,
                "symbol": symbol,
                "position_id": position_id,
                "side": side,
                "regime": regime,
                "event_type": event_type,
                "trigger_type": raw_trigger_type.upper() if raw_trigger_type else "",
                "trigger_types": derive_decision_triggers(metadata, reason, raw_trigger_type),
                "decision_source": decision_source,
                "direction_neutral_trial_active": bool(metadata.get("direction_neutral_trial_active", False)),
                "direction_neutral_trial_mode": trial_mode,
                "direction_neutral_trial_reason": trial_reason,
                "decision_confirm": int(to_float(metadata.get("decision_confirm"), 0) or 0),
                "pnl_pct": to_float(metadata.get("pnl_pct")),
                "pnl_value": realized_pnl,
                "price": to_float(metadata.get("last_close")) or to_float(metadata.get("last_close_5m")),
                "reason": reason,
                "source": "sqlite",
            }
        )

    return events, positions


def load_input_events(input_path: Path) -> List[Dict[str, Any]]:
    suffix = input_path.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        with input_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    elif suffix == ".json":
        with input_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload if isinstance(payload, list) else payload.get("events", [])
    elif suffix == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    else:
        raise ValueError(f"Unsupported input format: {input_path}")

    events: List[Dict[str, Any]] = []
    for row in rows:
        ts = parse_ts(row.get("ts") or row.get("timestamp"))
        if ts is None:
            continue
        trigger_types = row.get("trigger_types")
        if not isinstance(trigger_types, list):
            trigger_type = str(row.get("trigger_type") or "").strip()
            trigger_types = [trigger_type] if trigger_type else []
        events.append(
            {
                "ts": ts,
                "symbol": safe_upper(row.get("symbol")),
                "position_id": str(row.get("position_id") or "").strip(),
                "side": safe_upper(row.get("side")),
                "regime": str(row.get("regime") or ""),
                "event_type": safe_lower(row.get("event_type"), "event"),
                "trigger_type": str(row.get("trigger_type") or "").strip().upper(),
                "trigger_types": [str(x).strip().upper() for x in trigger_types if str(x).strip()],
                "decision_source": str(row.get("decision_source") or ""),
                "direction_neutral_trial_active": bool(row.get("direction_neutral_trial_active", False)),
                "direction_neutral_trial_mode": safe_lower(row.get("direction_neutral_trial_mode"), "none"),
                "direction_neutral_trial_reason": str(row.get("direction_neutral_trial_reason") or ""),
                "decision_confirm": int(to_float(row.get("decision_confirm"), 0) or 0),
                "pnl_pct": to_float(row.get("pnl_pct")),
                "pnl_value": to_float(row.get("pnl_value") if "pnl_value" in row else row.get("realized_pnl")),
                "price": to_float(row.get("price")),
                "reason": str(row.get("reason") or ""),
                "source": "input",
            }
        )
    return events


def build_position_index(positions: List[PositionWindow]) -> Dict[Tuple[str, str], List[PositionWindow]]:
    index: Dict[Tuple[str, str], List[PositionWindow]] = defaultdict(list)
    for pos in positions:
        index[(pos.symbol, pos.side)].append(pos)
    for key in index:
        index[key].sort(key=lambda p: p.open_ts)
    return index


def match_position(
    index: Dict[Tuple[str, str], List[PositionWindow]],
    symbol: str,
    side: str,
    ts: datetime,
) -> Optional[PositionWindow]:
    candidates = index.get((symbol, side), [])
    for pos in candidates:
        if ts < pos.open_ts:
            continue
        if pos.close_ts is not None and ts > pos.close_ts:
            continue
        return pos
    return None


def load_risk_events(
    risk_path: Path,
    position_index: Dict[Tuple[str, str], List[PositionWindow]],
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with risk_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            typ = safe_lower(obj.get("type"))
            ts_raw = obj.get("ts")
            if isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(float(ts_raw))
            else:
                ts = parse_ts(ts_raw)
            if ts is None:
                continue

            symbol = safe_upper(obj.get("symbol"))
            side = safe_upper(obj.get("position_side"))
            pos = match_position(position_index, symbol, side, ts)
            mode = pos.direction_neutral_trial_mode if pos else "none"
            source = pos.decision_source if pos else ""
            reason_mode = pos.direction_neutral_trial_reason if pos else ""
            position_id = pos.position_id if pos else f"{symbol}_{side}_{ts_id_fragment(ts)}"

            if typ == "risk":
                risk_state = safe_upper(obj.get("risk_state"))
                if risk_state == "REDUCE":
                    event_type = "reduce"
                elif risk_state in ("EXIT", "CIRCUIT_EXIT"):
                    event_type = "exit"
                else:
                    event_type = "risk"
                trigger_types = [safe_upper(x) for x in (obj.get("triggers") or []) if str(x).strip()]
                events.append(
                    {
                        "ts": ts,
                        "symbol": symbol,
                        "position_id": position_id,
                        "side": side,
                        "regime": str(obj.get("regime") or ""),
                        "event_type": event_type,
                        "trigger_type": trigger_types[0] if trigger_types else "",
                        "trigger_types": trigger_types,
                        "decision_source": source,
                        "direction_neutral_trial_active": mode != "none",
                        "direction_neutral_trial_mode": mode,
                        "direction_neutral_trial_reason": reason_mode,
                        "decision_confirm": int(obj.get("confirm_count", 0) or 0),
                        "pnl_pct": to_float(obj.get("pnl_pct")),
                        "pnl_value": None,
                        "price": to_float(obj.get("close_price")),
                        "reason": str(obj.get("reason") or ""),
                        "source": "risk_jsonl",
                    }
                )
            elif typ == "exec":
                action = safe_upper(obj.get("action"))
                meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
                exec_mode = safe_lower(meta.get("direction_neutral_trial_mode"), mode)
                exec_source = str(meta.get("decision_source") or source or "")
                exec_reason_mode = str(meta.get("direction_neutral_trial_reason") or reason_mode or "")
                exec_regime = str(meta.get("regime") or meta.get("engine") or (pos.regime if pos else "") or "")
                exec_confirm = int(to_float(meta.get("decision_confirm"), 0) or 0)
                if exec_confirm <= 0 and safe_lower(obj.get("decision_vote")) == "confirm":
                    exec_confirm = 1
                if action == "EXIT":
                    event_type = "exit"
                elif action == "REDUCE":
                    event_type = "reduce"
                else:
                    event_type = action.lower() if action else "exec"
                events.append(
                    {
                        "ts": ts,
                        "symbol": symbol,
                        "position_id": position_id,
                        "side": side,
                        "regime": exec_regime,
                        "event_type": event_type,
                        "trigger_type": action,
                        "trigger_types": [action] if action else [],
                        "decision_source": exec_source,
                        "direction_neutral_trial_active": exec_mode != "none",
                        "direction_neutral_trial_mode": exec_mode,
                        "direction_neutral_trial_reason": exec_reason_mode,
                        "decision_confirm": exec_confirm,
                        "pnl_pct": to_float(obj.get("pnl_pct")),
                        "pnl_value": to_float(obj.get("realized_pnl")),
                        "price": to_float(obj.get("price")),
                        "reason": str(meta.get("execution_reason") or obj.get("reason") or ""),
                        "source": "risk_jsonl",
                    }
                )
    return events


def within_window(ts: datetime, start: Optional[datetime], end: Optional[datetime]) -> bool:
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def filter_events(
    events: List[Dict[str, Any]],
    symbol: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> List[Dict[str, Any]]:
    symbol_u = safe_upper(symbol) if symbol else None
    out = []
    for event in events:
        ts = event.get("ts")
        if not isinstance(ts, datetime):
            continue
        if symbol_u and safe_upper(event.get("symbol")) != symbol_u:
            continue
        if not within_window(ts, start, end):
            continue
        out.append(event)
    out.sort(key=lambda r: r["ts"])
    return out


def event_pnl_pct(event: Dict[str, Any]) -> Optional[float]:
    return to_float(event.get("pnl_pct"))


def event_pnl_value(event: Dict[str, Any]) -> Optional[float]:
    return to_float(event.get("pnl_value"))


def summarize_numeric(values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    if not values:
        return None, None
    return mean(values), median(values)


def build_trigger_stats(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        mode = safe_lower(event.get("direction_neutral_trial_mode"), "none")
        trigger_types = event.get("trigger_types") or []
        if not trigger_types and event.get("trigger_type"):
            trigger_types = [str(event.get("trigger_type"))]
        for trig in trigger_types:
            trig_u = safe_upper(trig)
            if trig_u:
                grouped[(trig_u, mode)].append(event)

    rows: List[Dict[str, Any]] = []
    for (trigger_type, mode), group in sorted(grouped.items()):
        pnl_pcts = [x for x in (event_pnl_pct(ev) for ev in group) if x is not None]
        pnl_values = [x for x in (event_pnl_value(ev) for ev in group) if x is not None]
        avg_pct, med_pct = summarize_numeric(pnl_pcts)
        avg_val, med_val = summarize_numeric(pnl_values)
        rows.append(
            {
                "trigger_type": trigger_type,
                "direction_neutral_trial_mode": mode,
                "count": len(group),
                "win_count": sum(1 for ev in group if (event_pnl_pct(ev) or event_pnl_value(ev) or 0.0) > 0),
                "loss_count": sum(1 for ev in group if (event_pnl_pct(ev) or event_pnl_value(ev) or 0.0) < 0),
                "avg_pnl_pct": avg_pct,
                "median_pnl_pct": med_pct,
                "avg_pnl_value": avg_val,
                "median_pnl_value": med_val,
            }
        )
    return rows


def build_trigger_detail_stats(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        mode = safe_lower(event.get("direction_neutral_trial_mode"), "none")
        symbol = safe_upper(event.get("symbol"))
        side = safe_upper(event.get("side"))
        regime = str(event.get("regime") or "")
        trigger_types = event.get("trigger_types") or []
        if not trigger_types and event.get("trigger_type"):
            trigger_types = [str(event.get("trigger_type"))]
        for trig in trigger_types:
            trig_u = safe_upper(trig)
            if trig_u:
                grouped[(symbol, side, regime, trig_u, mode)].append(event)

    rows: List[Dict[str, Any]] = []
    for (symbol, side, regime, trigger_type, mode), group in sorted(grouped.items()):
        pnl_pcts = [x for x in (event_pnl_pct(ev) for ev in group) if x is not None]
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "regime": regime,
                "trigger_type": trigger_type,
                "direction_neutral_trial_mode": mode,
                "count": len(group),
                "win_count": sum(1 for ev in group if (event_pnl_pct(ev) or event_pnl_value(ev) or 0.0) > 0),
                "loss_count": sum(1 for ev in group if (event_pnl_pct(ev) or event_pnl_value(ev) or 0.0) < 0),
                "avg_pnl_pct": mean(pnl_pcts) if pnl_pcts else None,
                "median_pnl_pct": median(pnl_pcts) if pnl_pcts else None,
            }
        )
    return rows


def build_position_event_map(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_position: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        position_id = str(event.get("position_id") or "").strip()
        if position_id:
            by_position[position_id].append(event)
    for rows in by_position.values():
        rows.sort(key=lambda r: r["ts"])
    return by_position


def build_reduce_exit_stats(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_position = build_position_event_map(events)
    detail: List[Dict[str, Any]] = []

    for position_id, rows in by_position.items():
        first_reduce = next((r for r in rows if r.get("event_type") in REDUCE_EVENT_TYPES), None)
        if first_reduce is None:
            continue
        exit_event = next((r for r in rows if r["ts"] > first_reduce["ts"] and r.get("event_type") in EXIT_EVENT_TYPES), None)
        if exit_event is None:
            continue
        detail.append(
            {
                "position_id": position_id,
                "symbol": first_reduce.get("symbol"),
                "side": first_reduce.get("side"),
                "regime": first_reduce.get("regime"),
                "direction_neutral_trial_mode": safe_lower(first_reduce.get("direction_neutral_trial_mode"), "none"),
                "reduce_ts": first_reduce["ts"].isoformat(),
                "exit_ts": exit_event["ts"].isoformat(),
                "reduce_to_exit_sec": (exit_event["ts"] - first_reduce["ts"]).total_seconds(),
                "reduce_trigger_type": safe_upper(first_reduce.get("trigger_type")),
                "exit_trigger_type": safe_upper(exit_event.get("trigger_type")),
            }
        )

    grouped: Dict[str, List[float]] = defaultdict(list)
    for row in detail:
        grouped[row["direction_neutral_trial_mode"]].append(float(row["reduce_to_exit_sec"]))

    summary: List[Dict[str, Any]] = []
    for mode, values in sorted(grouped.items()):
        summary.append(
            {
                "direction_neutral_trial_mode": mode,
                "count": len(values),
                "avg_reduce_to_exit_sec": mean(values) if values else None,
                "median_reduce_to_exit_sec": median(values) if values else None,
                "p90_reduce_to_exit_sec": percentile(values, 0.90),
            }
        )
    return summary, detail


def build_confirm_stats(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_position = build_position_event_map(events)
    detail: List[Dict[str, Any]] = []

    for position_id, rows in by_position.items():
        for idx, row in enumerate(rows):
            decision_confirm = int(row.get("decision_confirm") or 0)
            if decision_confirm <= 0:
                continue
            future_rows = rows[idx + 1 :]
            if not future_rows:
                continue
            stop_idx = None
            for j, future in enumerate(future_rows):
                if future.get("event_type") in STOP_EVENT_TYPES:
                    stop_idx = j
                    break
            window = future_rows if stop_idx is None else future_rows[: stop_idx + 1]

            pnl_pct_values = [x for x in (event_pnl_pct(ev) for ev in window) if x is not None]
            pnl_value_values = [x for x in (event_pnl_value(ev) for ev in window) if x is not None]
            if not pnl_pct_values and not pnl_value_values:
                continue

            detail.append(
                {
                    "position_id": position_id,
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "regime": row.get("regime"),
                    "decision_confirm": decision_confirm,
                    "direction_neutral_trial_mode": safe_lower(row.get("direction_neutral_trial_mode"), "none"),
                    "decision_source": row.get("decision_source"),
                    "avg_pnl_after_confirm_pct": mean(pnl_pct_values) if pnl_pct_values else None,
                    "final_pnl_after_confirm_pct": pnl_pct_values[-1] if pnl_pct_values else None,
                    "mfe_after_confirm_pct": max(pnl_pct_values) if pnl_pct_values else None,
                    "mae_after_confirm_pct": min(pnl_pct_values) if pnl_pct_values else None,
                    "avg_pnl_after_confirm_value": mean(pnl_value_values) if pnl_value_values else None,
                    "final_pnl_after_confirm_value": pnl_value_values[-1] if pnl_value_values else None,
                    "mfe_after_confirm_value": max(pnl_value_values) if pnl_value_values else None,
                    "mae_after_confirm_value": min(pnl_value_values) if pnl_value_values else None,
                }
            )

    grouped: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in detail:
        grouped[(int(row["decision_confirm"]), row["direction_neutral_trial_mode"])].append(row)

    summary: List[Dict[str, Any]] = []
    for (confirm, mode), rows in sorted(grouped.items()):
        pct_avg = [r["avg_pnl_after_confirm_pct"] for r in rows if r["avg_pnl_after_confirm_pct"] is not None]
        pct_final = [r["final_pnl_after_confirm_pct"] for r in rows if r["final_pnl_after_confirm_pct"] is not None]
        pct_mfe = [r["mfe_after_confirm_pct"] for r in rows if r["mfe_after_confirm_pct"] is not None]
        pct_mae = [r["mae_after_confirm_pct"] for r in rows if r["mae_after_confirm_pct"] is not None]
        val_final = [r["final_pnl_after_confirm_value"] for r in rows if r["final_pnl_after_confirm_value"] is not None]
        summary.append(
            {
                "decision_confirm": confirm,
                "direction_neutral_trial_mode": mode,
                "count": len(rows),
                "avg_pnl_after_confirm_pct": mean(pct_avg) if pct_avg else None,
                "avg_final_pnl_after_confirm_pct": mean(pct_final) if pct_final else None,
                "avg_mfe_after_confirm_pct": mean(pct_mfe) if pct_mfe else None,
                "avg_mae_after_confirm_pct": mean(pct_mae) if pct_mae else None,
                "avg_final_pnl_after_confirm_value": mean(val_final) if val_final else None,
            }
        )
    return summary, detail


def build_trial_stats(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_position = build_position_event_map(events)
    detail: List[Dict[str, Any]] = []

    for position_id, rows in by_position.items():
        opens = [ev for ev in rows if ev.get("event_type") == "open"]
        if not opens:
            continue
        open_ev = opens[0]
        if str(open_ev.get("decision_source") or "") != "trend_both_trial":
            continue
        exit_events = [ev for ev in rows if ev.get("event_type") in EXIT_EVENT_TYPES]
        final_exit = exit_events[-1] if exit_events else None
        final_pnl_pct = event_pnl_pct(final_exit) if final_exit else None
        final_pnl_value = event_pnl_value(final_exit) if final_exit else None
        hold_seconds = (final_exit["ts"] - open_ev["ts"]).total_seconds() if final_exit else None
        reduce_count = sum(1 for ev in rows if ev.get("event_type") in REDUCE_EVENT_TYPES)
        detail.append(
            {
                "position_id": position_id,
                "symbol": open_ev.get("symbol"),
                "side": open_ev.get("side"),
                "regime": open_ev.get("regime"),
                "direction_neutral_trial_mode": safe_lower(open_ev.get("direction_neutral_trial_mode"), "none"),
                "final_pnl_pct": final_pnl_pct,
                "final_pnl_value": final_pnl_value,
                "hold_seconds": hold_seconds,
                "reduce_count": reduce_count,
                "exited": final_exit is not None,
            }
        )

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in detail:
        grouped[row["direction_neutral_trial_mode"]].append(row)

    summary: List[Dict[str, Any]] = []
    for mode, group in sorted(grouped.items()):
        pct_vals = [r["final_pnl_pct"] for r in group if r["final_pnl_pct"] is not None]
        val_vals = [r["final_pnl_value"] for r in group if r["final_pnl_value"] is not None]
        holds = [r["hold_seconds"] for r in group if r["hold_seconds"] is not None]
        summary.append(
            {
                "direction_neutral_trial_mode": mode,
                "count": len(group),
                "avg_pnl_pct": mean(pct_vals) if pct_vals else None,
                "avg_pnl_value": mean(val_vals) if val_vals else None,
                "win_rate": (sum(1 for x in pct_vals if x > 0) / len(pct_vals)) if pct_vals else None,
                "avg_hold_seconds": mean(holds) if holds else None,
                "reduce_ratio": (sum(1 for r in group if r["reduce_count"] > 0) / len(group)) if group else None,
                "exit_ratio": (sum(1 for r in group if r["exited"]) / len(group)) if group else None,
            }
        )
    return summary


def fmt_num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.{digits}f}"


def print_table(title: str, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("No data")
        return
    widths = {
        col: max(
            len(col),
            *(
                len(fmt_num(row.get(col)) if isinstance(row.get(col), (int, float)) else str(row.get(col, "")))
                for row in rows
            ),
        )
        for col in columns
    }
    header = " | ".join(f"{col:{widths[col]}}" for col in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        parts = []
        for col in columns:
            value = row.get(col)
            text = fmt_num(value) if isinstance(value, (int, float)) else str(value or "")
            parts.append(f"{text:{widths[col]}}")
        print(" | ".join(parts))


def write_csv(output_dir: Path, name: str, rows: List[Dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.csv"
    if not rows:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["empty"])
        return
    columns = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            normalized = dict(row)
            for key, value in normalized.items():
                if isinstance(value, datetime):
                    normalized[key] = value.isoformat()
            writer.writerow(normalized)


def main() -> None:
    args = parse_args()
    start = parse_ts(args.since) if args.since else None
    end = parse_ts(args.until) if args.until else None

    events: List[Dict[str, Any]] = []
    positions: List[PositionWindow] = []

    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            raise FileNotFoundError(f"input not found: {input_path}")
        events.extend(load_input_events(input_path))

    db_path = Path(args.db) if args.db else detect_latest_db()
    if db_path and db_path.exists():
        sqlite_events, sqlite_positions = load_sqlite_events(db_path)
        events.extend(sqlite_events)
        positions.extend(sqlite_positions)

    risk_path = Path(args.risk_jsonl) if args.risk_jsonl else detect_risk_jsonl()
    if risk_path and risk_path.exists():
        position_index = build_position_index(positions)
        events.extend(load_risk_events(risk_path, position_index))

    events = filter_events(events, symbol=args.symbol, start=start, end=end)
    if not events:
        print("No events to analyze. Pass --db / --risk-jsonl / --input.")
        return

    trigger_stats = build_trigger_stats(events)
    trigger_detail_stats = build_trigger_detail_stats(events)
    reduce_exit_stats, reduce_exit_detail = build_reduce_exit_stats(events)
    confirm_stats, confirm_detail = build_confirm_stats(events)
    trial_stats = build_trial_stats(events)

    print(f"Total events: {len(events)}")
    if db_path and db_path.exists():
        print(f"SQLite: {db_path}")
    if risk_path and risk_path.exists():
        print(f"Risk JSONL: {risk_path}")

    print_table(
        "Trigger Stats",
        trigger_stats,
        [
            "trigger_type",
            "direction_neutral_trial_mode",
            "count",
            "win_count",
            "loss_count",
            "avg_pnl_pct",
            "median_pnl_pct",
            "avg_pnl_value",
        ],
    )
    print_table(
        "Reduce Exit Stats",
        reduce_exit_stats,
        [
            "direction_neutral_trial_mode",
            "count",
            "avg_reduce_to_exit_sec",
            "median_reduce_to_exit_sec",
            "p90_reduce_to_exit_sec",
        ],
    )
    print_table(
        "Confirm Stats",
        confirm_stats,
        [
            "decision_confirm",
            "direction_neutral_trial_mode",
            "count",
            "avg_pnl_after_confirm_pct",
            "avg_final_pnl_after_confirm_pct",
            "avg_mfe_after_confirm_pct",
            "avg_mae_after_confirm_pct",
            "avg_final_pnl_after_confirm_value",
        ],
    )
    print_table(
        "Trend Both Trial Stats",
        trial_stats,
        [
            "direction_neutral_trial_mode",
            "count",
            "avg_pnl_pct",
            "avg_pnl_value",
            "win_rate",
            "avg_hold_seconds",
            "reduce_ratio",
            "exit_ratio",
        ],
    )

    if args.output_dir:
        output_dir = Path(args.output_dir)
        write_csv(output_dir, "trigger_stats", trigger_stats)
        write_csv(output_dir, "trigger_detail_stats", trigger_detail_stats)
        write_csv(output_dir, "reduce_exit_stats", reduce_exit_stats)
        write_csv(output_dir, "reduce_exit_detail", reduce_exit_detail)
        write_csv(output_dir, "confirm_stats", confirm_stats)
        write_csv(output_dir, "confirm_detail", confirm_detail)
        write_csv(output_dir, "trial_stats", trial_stats)
        print(f"\nCSV written to: {output_dir}")


if __name__ == "__main__":
    main()
