#!/usr/bin/env python
# pyright: reportMissingImports=false
# -*- coding: utf-8 -*-
"""
Fund flow rule backtest.

Strategy:
- 1H EMA30 direction filter
- 15m EMA+MACD or EMA+BB entry
- EMA30-based initial stop
- TP1 partial reduce, then EMA10/MACD runner exit

Modes:
- loose: single-model allowed, but with cooldown and stronger trend thresholds
- strict: requires dual-model resonance and lower per-trade risk
- compare: run both and output side-by-side comparison
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.data.klines_downloader import download_public_klines, load_or_download


@dataclass(frozen=True)
class ModeConfig:
    name: str
    margin_pct_per_trade: float
    cooldown_bars: int
    min_1h_slope_pct: float
    require_macd_hist_expand: bool
    min_macd_hist_pct: float
    min_bb_width_delta: float
    min_model_count: int
    require_dual_models: bool
    entry_signal_carry_bars: int


@dataclass
class Position:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    quantity: float
    margin: float
    leverage: float
    stop_price: float
    tp1_price: float
    tp1_hit: bool = False
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    entry_models: str = "-"
    resonance_count: int = 0


@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    leverage: float
    pnl: float
    pnl_pct_on_margin: float
    hold_bars: int
    exit_reason: str
    entry_models: str
    tp1_hit: bool


DEFAULT_MODES: Dict[str, ModeConfig] = {
    "loose": ModeConfig(
        name="loose",
        margin_pct_per_trade=0.20,
        cooldown_bars=8,
        min_1h_slope_pct=0.00025,
        require_macd_hist_expand=False,
        min_macd_hist_pct=0.00008,
        min_bb_width_delta=0.0004,
        min_model_count=1,
        require_dual_models=False,
        entry_signal_carry_bars=1,
    ),
    "strict": ModeConfig(
        name="strict",
        margin_pct_per_trade=0.15,
        cooldown_bars=12,
        min_1h_slope_pct=0.00035,
        require_macd_hist_expand=False,
        min_macd_hist_pct=0.00012,
        min_bb_width_delta=0.0008,
        min_model_count=1,
        require_dual_models=False,
        entry_signal_carry_bars=1,
    ),
}


def _normalize_int_list(values: object) -> List[int]:
    if not isinstance(values, list):
        return []
    result: List[int] = []
    for value in values:
        try:
            hour = int(value)
        except Exception:
            continue
        if 0 <= hour <= 23:
            result.append(hour)
    return sorted(set(result))


def _resolve_backtest_profile(config: dict, profile_name: str) -> Tuple[str, dict]:
    fund_flow_cfg = config.get("fund_flow") or {}
    backtest_cfg = fund_flow_cfg.get("backtest") if isinstance(fund_flow_cfg.get("backtest"), dict) else {}
    selected = str(profile_name or backtest_cfg.get("default_profile") or "").strip()
    profiles = backtest_cfg.get("profiles") if isinstance(backtest_cfg.get("profiles"), dict) else {}
    if not selected:
        return "", {}
    profile = profiles.get(selected)
    if not isinstance(profile, dict):
        raise KeyError(f"未知回测 profile: {selected}")
    return selected, profile


def _merge_mode_config(base_mode: ModeConfig, overrides: dict) -> ModeConfig:
    if not overrides:
        return base_mode
    data = asdict(base_mode)
    for key, value in overrides.items():
        if key in data and value is not None:
            data[key] = value
    # Strategy rule: either EMA+MACD or EMA+BB is enough for entry.
    data["require_macd_hist_expand"] = False
    data["min_model_count"] = 1
    data["require_dual_models"] = False
    data["entry_signal_carry_bars"] = max(0, int(data.get("entry_signal_carry_bars", 1)))
    return ModeConfig(**data)


def _apply_profile_to_args(args: argparse.Namespace, profile_name: str, profile: dict) -> argparse.Namespace:
    merged = argparse.Namespace(**vars(args))
    merged.profile_name = profile_name
    merged.allowed_entry_hours = _normalize_int_list(profile.get("allowed_entry_hours"))
    merged.profile_rule_overrides = profile.get("rule_overrides") if isinstance(profile.get("rule_overrides"), dict) else {}
    profile_symbols = profile.get("symbols") if isinstance(profile.get("symbols"), list) else []
    if (not merged.symbols) and profile_symbols:
        merged.symbols = ",".join(str(symbol).upper() for symbol in profile_symbols)
    if profile.get("max_same_side_positions") is not None:
        merged.max_same_side_positions = int(profile.get("max_same_side_positions"))
    return merged


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _row_at(df: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
    if ts not in df.index:
        return None
    row = df.loc[ts]
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return None
        return row.iloc[-1]
    return row


def _prev_row(df: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
    if ts not in df.index:
        return None
    loc = df.index.get_loc(ts)
    if isinstance(loc, slice):
        pos = int(loc.stop) - 1
    elif isinstance(loc, (list, tuple)):
        pos = int(loc[-1])
    elif hasattr(loc, "tolist") and not isinstance(loc, (int, str, bytes)):
        values = loc.tolist()
        if isinstance(values, list):
            truthy = [idx for idx, flag in enumerate(values) if flag]
            if not truthy:
                return None
            pos = truthy[-1]
        else:
            pos = int(values)
    else:
        pos = int(loc)
    if pos <= 0:
        return None
    return df.iloc[pos - 1]


def _compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = _ema(df["close"], fast)
    ema_slow = _ema(df["close"], slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    close_safe = df["close"].abs().replace(0.0, pd.NA)
    hist_pct = hist.abs() / close_safe
    cross = pd.Series("NONE", index=df.index, dtype="object")
    cross[(macd_line.shift(1) <= signal_line.shift(1)) & (macd_line > signal_line)] = "GOLDEN"
    cross[(macd_line.shift(1) >= signal_line.shift(1)) & (macd_line < signal_line)] = "DEAD"
    zone = pd.Series("NEAR_ZERO", index=df.index, dtype="object")
    zone[macd_line > 0] = "ABOVE_ZERO"
    zone[macd_line < 0] = "BELOW_ZERO"
    return pd.DataFrame(
        {
            "macd_line": macd_line,
            "macd_signal": signal_line,
            "macd_hist": hist,
            "macd_hist_pct": hist_pct.fillna(0.0),
            "macd_cross": cross,
            "macd_zone": zone,
            "macd_hist_delta": hist.diff().fillna(0.0),
            "macd_hist_expand_up": (hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2)),
            "macd_hist_expand_down": (hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2)),
        }
    )


def _compute_bollinger(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    middle = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = middle + std * num_std
    lower = middle - std * num_std
    width = (upper - lower) / middle.replace(0.0, pd.NA)
    bb_break = pd.Series("NONE", index=df.index, dtype="object")
    bb_break[df["close"] > upper] = "UPPER"
    bb_break[df["close"] < lower] = "LOWER"
    width_delta = width.diff().fillna(0.0)
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width.fillna(0.0),
            "bb_break": bb_break,
            "bb_width_expand": width > width.shift(1),
            "bb_width_delta": width_delta,
            "bb_width_delta_pos": width_delta.clip(lower=0.0),
        }
    )


def _prepare_15m(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_index()
    out["ema10"] = _ema(out["close"], 10)
    out["ema30"] = _ema(out["close"], 30)
    out["ema_cross"] = "NONE"
    out.loc[(out["ema10"].shift(1) <= out["ema30"].shift(1)) & (out["ema10"] > out["ema30"]), "ema_cross"] = "GOLDEN"
    out.loc[(out["ema10"].shift(1) >= out["ema30"].shift(1)) & (out["ema10"] < out["ema30"]), "ema_cross"] = "DEAD"
    out = out.join(_compute_macd(out))
    out = out.join(_compute_bollinger(out))
    return out


def _prepare_1h(df_15m: pd.DataFrame) -> pd.DataFrame:
    agg = (
        df_15m[["open", "high", "low", "close", "volume"]]
        .resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    agg["ema30_1h"] = _ema(agg["close"], 30)
    agg["ema30_slope"] = agg["ema30_1h"] - agg["ema30_1h"].shift(3)
    agg["ema30_slope_pct"] = agg["ema30_slope"] / agg["ema30_1h"].shift(3).replace(0.0, pd.NA)
    agg["regime_close"] = agg["close"]
    safe = agg[["regime_close", "ema30_1h", "ema30_slope_pct"]].shift(1)
    return safe.reindex(df_15m.index, method="ffill")


def _regime(row: pd.Series, ema_band_pct: float, flat_slope_threshold: float, min_1h_slope_pct: float) -> Tuple[str, str]:
    close_price = _as_float(row.get("regime_close"))
    ema30 = _as_float(row.get("ema30_1h"))
    slope_pct = _as_float(row.get("ema30_slope_pct"))
    if close_price <= 0 or ema30 <= 0:
        return "NO_TRADE", "BOTH"
    band = abs(ema30) * ema_band_pct
    slope_gate = max(flat_slope_threshold, min_1h_slope_pct)
    if abs(slope_pct) <= slope_gate:
        return "NO_TRADE", "BOTH"
    if close_price > ema30 + band and slope_pct > 0:
        return "TREND", "LONG_ONLY"
    if close_price < ema30 - band and slope_pct < 0:
        return "TREND", "SHORT_ONLY"
    return "NO_TRADE", "BOTH"


def _entry_models(row: pd.Series, prev_row: Optional[pd.Series], direction: str, mode: ModeConfig) -> Tuple[List[str], Dict[str, int]]:
    close_price = _as_float(row.get("close"))
    ema10 = _as_float(row.get("ema10"))
    ema30 = _as_float(row.get("ema30"))
    if not all(math.isfinite(v) and v > 0 for v in (close_price, ema10, ema30)):
        return [], {}

    long_ema_bias = ema10 > ema30 and close_price >= ema30
    short_ema_bias = ema10 < ema30 and close_price <= ema30
    carry_bars = max(0, int(getattr(mode, "entry_signal_carry_bars", 0)))

    def _signal_flags(signal_row: Optional[pd.Series]) -> Dict[str, bool]:
        if signal_row is None:
            return {}
        signal_close = _as_float(signal_row.get("close"))
        signal_ema10 = _as_float(signal_row.get("ema10"))
        signal_ema30 = _as_float(signal_row.get("ema30"))
        if not all(math.isfinite(v) and v > 0 for v in (signal_close, signal_ema10, signal_ema30)):
            return {}

        signal_long_bias = signal_ema10 > signal_ema30 and signal_close >= signal_ema30
        signal_short_bias = signal_ema10 < signal_ema30 and signal_close <= signal_ema30
        ema_cross = str(signal_row.get("ema_cross", "NONE")).upper()
        macd_cross = str(signal_row.get("macd_cross", "NONE")).upper()
        macd_zone = str(signal_row.get("macd_zone", "NEAR_ZERO")).upper()
        macd_hist = _as_float(signal_row.get("macd_hist"))
        macd_hist_pct = _as_float(signal_row.get("macd_hist_pct"))
        hist_expand_up = bool(signal_row.get("macd_hist_expand_up", False))
        hist_expand_down = bool(signal_row.get("macd_hist_expand_down", False))
        bb_break = str(signal_row.get("bb_break", "NONE")).upper()
        bb_width_expand = bool(signal_row.get("bb_width_expand", False))
        bb_width_delta_pos = _as_float(signal_row.get("bb_width_delta_pos"))

        long_macd_core = signal_long_bias and ema_cross == "GOLDEN" and (
            macd_cross == "GOLDEN" or (macd_zone == "ABOVE_ZERO" and (macd_hist > 0 or hist_expand_up))
        )
        short_macd_core = signal_short_bias and ema_cross == "DEAD" and (
            macd_cross == "DEAD" or (macd_zone == "BELOW_ZERO" and (macd_hist < 0 or hist_expand_down))
        )
        long_macd_ok = long_macd_core and macd_hist_pct >= mode.min_macd_hist_pct
        short_macd_ok = short_macd_core and macd_hist_pct >= mode.min_macd_hist_pct

        long_bb_ok = signal_long_bias and bb_break == "UPPER" and bb_width_expand and bb_width_delta_pos >= mode.min_bb_width_delta
        short_bb_ok = signal_short_bias and bb_break == "LOWER" and bb_width_expand and bb_width_delta_pos >= mode.min_bb_width_delta
        return {
            "LONG_EMA_MACD": long_macd_ok,
            "LONG_EMA_BB": long_bb_ok,
            "SHORT_EMA_MACD": short_macd_ok,
            "SHORT_EMA_BB": short_bb_ok,
        }

    current_flags = _signal_flags(row)
    prev_flags = _signal_flags(prev_row) if carry_bars >= 1 else {}

    models: List[str] = []
    signal_bars_ago: Dict[str, int] = {}
    if direction == "LONG_ONLY" and long_ema_bias:
        if current_flags.get("LONG_EMA_MACD"):
            models.append("EMA_MACD")
            signal_bars_ago["EMA_MACD"] = 0
        elif prev_flags.get("LONG_EMA_MACD"):
            models.append("EMA_MACD")
            signal_bars_ago["EMA_MACD"] = 1
        if current_flags.get("LONG_EMA_BB"):
            models.append("EMA_BB")
            signal_bars_ago["EMA_BB"] = 0
        elif prev_flags.get("LONG_EMA_BB"):
            models.append("EMA_BB")
            signal_bars_ago["EMA_BB"] = 1
    elif direction == "SHORT_ONLY" and short_ema_bias:
        if current_flags.get("SHORT_EMA_MACD"):
            models.append("EMA_MACD")
            signal_bars_ago["EMA_MACD"] = 0
        elif prev_flags.get("SHORT_EMA_MACD"):
            models.append("EMA_MACD")
            signal_bars_ago["EMA_MACD"] = 1
        if current_flags.get("SHORT_EMA_BB"):
            models.append("EMA_BB")
            signal_bars_ago["EMA_BB"] = 0
        elif prev_flags.get("SHORT_EMA_BB"):
            models.append("EMA_BB")
            signal_bars_ago["EMA_BB"] = 1
    return models, signal_bars_ago

def _risk_plan(
    side: str,
    entry_price: float,
    stop_anchor: float,
    entry_models: List[str],
    min_stop_pct: float,
    max_stop_pct: float,
    stop_buffer_pct: float,
    stop_break_buffer_pct: float,
    tp1_min_pct: float,
    tp1_max_pct: float,
) -> Optional[Dict[str, float]]:
    if entry_price <= 0 or stop_anchor <= 0:
        return None
    raw_stop_pct = abs(entry_price - stop_anchor) / entry_price
    if raw_stop_pct <= 0 or raw_stop_pct > max_stop_pct:
        return None
    effective_stop_pct = min(max_stop_pct, max(min_stop_pct, raw_stop_pct + stop_buffer_pct))
    if side == "LONG":
        stop_trigger_price = min(
            stop_anchor * (1.0 - stop_break_buffer_pct),
            entry_price * (1.0 - effective_stop_pct),
        )
    else:
        stop_trigger_price = max(
            stop_anchor * (1.0 + stop_break_buffer_pct),
            entry_price * (1.0 + effective_stop_pct),
        )

    model_set = set(entry_models)
    tp1_pct = max(tp1_min_pct, min(tp1_max_pct, effective_stop_pct * 4.0))
    if "EMA_MACD" in model_set and "EMA_BB" in model_set:
        tp1_pct = max(tp1_pct, 0.08)
    elif "EMA_BB" in model_set:
        tp1_pct = max(tp1_pct, 0.07)
    else:
        tp1_pct = max(tp1_pct, 0.06)
    tp1_pct = min(tp1_max_pct, tp1_pct)
    tp1_price = entry_price * (1.0 + tp1_pct) if side == "LONG" else entry_price * (1.0 - tp1_pct)
    return {
        "raw_stop_pct": raw_stop_pct,
        "effective_stop_pct": effective_stop_pct,
        "stop_trigger_price": stop_trigger_price,
        "tp1_pct": tp1_pct,
        "tp1_price": tp1_price,
    }


def _candidate_rank(row: pd.Series, side: str, models: List[str], risk_plan: Dict[str, float]) -> Tuple[float, float, float, float, float]:
    model_score = float(len(models))
    slope = _as_float(row.get("ema30_slope_pct"))
    slope_score = slope if side == "LONG" else -slope
    hist = _as_float(row.get("macd_hist"))
    hist_delta = _as_float(row.get("macd_hist_delta"))
    macd_score = (hist if side == "LONG" else -hist) + (hist_delta if side == "LONG" else -hist_delta)
    bb_score = _as_float(row.get("bb_width_delta_pos")) + (_as_float(row.get("bb_width")) * 0.5)
    effective_stop_pct = float(risk_plan["effective_stop_pct"])
    ideal_stop = (0.02 + 0.05) / 2.0
    stop_quality = -abs(effective_stop_pct - ideal_stop)
    return (model_score, slope_score, macd_score, bb_score, stop_quality)


def _pnl(side: str, entry_price: float, exit_price: float, quantity: float) -> float:
    if side == "LONG":
        return (exit_price - entry_price) * quantity
    return (entry_price - exit_price) * quantity


def _analyze_trades(trades_df: pd.DataFrame) -> Dict[str, object]:
    if trades_df.empty:
        return {"worst_symbols": [], "worst_entry_hours": []}

    temp = trades_df.copy()
    temp["entry_dt"] = pd.to_datetime(temp["entry_time"], errors="coerce")
    temp["entry_hour"] = temp["entry_dt"].dt.hour.fillna(-1).astype(int)

    symbol_stats = (
        temp.groupby("symbol", dropna=False)
        .agg(
            trade_count=("pnl", "count"),
            total_pnl=("pnl", "sum"),
            avg_pnl=("pnl", "mean"),
            win_rate=("pnl", lambda s: float((s > 0).mean() * 100.0)),
        )
        .reset_index()
        .sort_values(["total_pnl", "avg_pnl"], ascending=[True, True])
    )

    hour_stats = (
        temp[temp["entry_hour"] >= 0]
        .groupby("entry_hour", dropna=False)
        .agg(
            trade_count=("pnl", "count"),
            total_pnl=("pnl", "sum"),
            avg_pnl=("pnl", "mean"),
            win_rate=("pnl", lambda s: float((s > 0).mean() * 100.0)),
        )
        .reset_index()
        .sort_values(["total_pnl", "avg_pnl"], ascending=[True, True])
    )

    return {
        "worst_symbols": symbol_stats.head(5).to_dict(orient="records"),
        "worst_entry_hours": hour_stats.head(6).to_dict(orient="records"),
    }


class RuleBacktester:
    def __init__(self, args: argparse.Namespace, mode: ModeConfig):
        self.args = args
        self.mode = mode
        self.profile_name = str(getattr(args, "profile_name", "") or "")
        profile_hours = _normalize_int_list(getattr(args, "allowed_entry_hours", []))
        self.allowed_entry_hours = set(profile_hours) if profile_hours else None
        self.config = _read_json(args.config)
        self.rule_cfg = dict(((self.config.get("fund_flow") or {}).get("rule_strategy") or {}))
        self.fund_flow_cfg = self.config.get("fund_flow") or {}
        self.trading_cfg = self.config.get("trading") or {}
        self.risk_cfg = self.config.get("risk") or {}
        profile_rule_overrides = getattr(args, "profile_rule_overrides", None)
        if isinstance(profile_rule_overrides, dict) and profile_rule_overrides:
            self.rule_cfg.update(profile_rule_overrides)

        cfg_symbols = list(self.trading_cfg.get("symbols") or [])
        requested = args.symbols.split(",") if args.symbols else cfg_symbols
        self.symbols = [s.strip().upper() for s in requested if s and s.strip()]
        self.max_active_symbols = int(self.fund_flow_cfg.get("max_active_symbols", 2))
        self.max_same_side_positions = int(args.max_same_side_positions or 1)
        self.leverage = float(args.leverage or self.trading_cfg.get("default_leverage", 4))
        self.entry_slippage = float(args.slippage if args.slippage is not None else self.fund_flow_cfg.get("entry_slippage", 0.0015))
        self.fee_rate = float(args.fee_rate)
        self.data_dir = args.data_dir
        self.report_dir = args.report_dir
        self.initial_capital = float(args.initial_capital)
        self.cash = float(args.initial_capital)
        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeRecord] = []
        self.equity_curve: List[Dict[str, object]] = []
        self.market: Dict[str, pd.DataFrame] = {}
        self.eval_start: Optional[pd.Timestamp] = None
        self.eval_end: Optional[pd.Timestamp] = None
        self.cooldown_until: Dict[str, pd.Timestamp] = {}
        self.daily_realized_pnl: Dict[str, float] = {}
        self.daily_blocked_dates: set[str] = set()
        self.consecutive_losses = 0
        self.account_cooldown_until: Optional[pd.Timestamp] = None
        self.max_daily_loss_pct = float(self.risk_cfg.get("max_daily_loss_percent", 5)) / 100.0
        self.max_consecutive_losses = int(self.risk_cfg.get("max_consecutive_losses", 2))
        self.consecutive_loss_cooldown_seconds = int(self.risk_cfg.get("consecutive_loss_cooldown_seconds", 1800))
        self.account_risk_events: List[Dict[str, object]] = []
        self.bar_logs: List[Dict[str, object]] = []

    @staticmethod
    def _row_indicator_snapshot(row: Optional[pd.Series]) -> Dict[str, object]:
        if row is None:
            return {}
        return {
            "close": _as_float(row.get("close")),
            "high": _as_float(row.get("high")),
            "low": _as_float(row.get("low")),
            "ema10": _as_float(row.get("ema10")),
            "ema30": _as_float(row.get("ema30")),
            "ema30_1h": _as_float(row.get("ema30_1h")),
            "ema_cross": str(row.get("ema_cross", "NONE")).upper(),
            "macd_cross": str(row.get("macd_cross", "NONE")).upper(),
            "macd_zone": str(row.get("macd_zone", "NEAR_ZERO")).upper(),
            "macd_hist": _as_float(row.get("macd_hist")),
            "bb_break": str(row.get("bb_break", "NONE")).upper(),
            "bb_width_expand": bool(row.get("bb_width_expand", False)),
        }

    def _position_snapshot(self, symbol: str, row: Optional[pd.Series]) -> Dict[str, object]:
        pos = self.positions.get(symbol)
        snapshot: Dict[str, object] = {
            "position_side": "FLAT",
            "entry_price": None,
            "quantity": 0.0,
            "stop_price": None,
            "tp1_price": None,
            "tp1_hit": False,
            "pnl_ratio": None,
        }
        if pos is None:
            return snapshot
        current_close = _as_float(row.get("close")) if row is not None else 0.0
        pnl_ratio = None
        if current_close > 0 and pos.entry_price > 0:
            pnl_ratio = (
                (current_close - pos.entry_price) / pos.entry_price if pos.side == "LONG" else (pos.entry_price - current_close) / pos.entry_price
            )
        snapshot.update(
            {
                "position_side": pos.side,
                "entry_price": round(pos.entry_price, 8),
                "quantity": round(pos.quantity, 8),
                "stop_price": round(pos.stop_price, 8) if pos.stop_price > 0 else None,
                "tp1_price": round(pos.tp1_price, 8) if pos.tp1_price > 0 else None,
                "tp1_hit": bool(pos.tp1_hit),
                "pnl_ratio": round(pnl_ratio, 8) if pnl_ratio is not None else None,
            }
        )
        return snapshot

    def _append_bar_log(
        self,
        *,
        ts: pd.Timestamp,
        symbol: str,
        row: Optional[pd.Series],
        position_before: Dict[str, object],
        position_after: Dict[str, object],
        action: str,
        reason: str,
        event: Optional[Dict[str, object]] = None,
    ) -> None:
        payload: Dict[str, object] = {
            "timestamp": ts.isoformat(),
            "symbol": symbol,
            "action": action,
            "reason": reason,
            "position_before_side": position_before.get("position_side"),
            "position_after_side": position_after.get("position_side"),
            "entry_price_before": position_before.get("entry_price"),
            "entry_price_after": position_after.get("entry_price"),
            "quantity_before": position_before.get("quantity"),
            "quantity_after": position_after.get("quantity"),
            "stop_price_before": position_before.get("stop_price"),
            "stop_price_after": position_after.get("stop_price"),
            "tp1_price_before": position_before.get("tp1_price"),
            "tp1_price_after": position_after.get("tp1_price"),
            "tp1_hit_before": position_before.get("tp1_hit"),
            "tp1_hit_after": position_after.get("tp1_hit"),
            "pnl_ratio_before": position_before.get("pnl_ratio"),
            "pnl_ratio_after": position_after.get("pnl_ratio"),
        }
        payload.update(self._row_indicator_snapshot(row))
        if isinstance(event, dict):
            for key, value in event.items():
                if key in {"action", "reason"}:
                    continue
                payload[f"event_{key}"] = value
        self.bar_logs.append(payload)

    def _download_symbol(self, symbol: str, total_days: int) -> pd.DataFrame:
        os.makedirs(self.data_dir, exist_ok=True)
        file_path = os.path.join(self.data_dir, f"{symbol}_15m_{total_days}d.csv")
        if self.args.refresh or (not os.path.exists(file_path)):
            df = download_public_klines(symbol, "15m", total_days, file_path)
        else:
            df, _ = load_or_download(symbol, "15m", total_days, self.data_dir)
        if df is None or df.empty:
            raise RuntimeError(f"无法下载或读取 {symbol} 的 15m 数据")
        return df

    def load_market(self) -> None:
        total_days = self.args.days + self.args.warmup_days
        for symbol in self.symbols:
            raw = self._download_symbol(symbol, total_days).sort_index()
            df15 = _prepare_15m(raw)
            df1h = _prepare_1h(raw)
            merged = df15.join(df1h, how="left")
            merged["symbol"] = symbol
            self.market[symbol] = merged

        timeline = sorted(set().union(*[set(df.index) for df in self.market.values()]))
        if not timeline:
            raise RuntimeError("没有可用的时间轴")
        self.eval_end = pd.Timestamp(timeline[-1])
        self.eval_start = self.eval_end - pd.Timedelta(days=self.args.days)

    def _mark_equity(self, ts: pd.Timestamp) -> float:
        equity = self.cash
        for symbol, pos in self.positions.items():
            row = _row_at(self.market[symbol], ts)
            if row is None:
                continue
            current_price = _as_float(row.get("close"))
            equity += pos.margin + pos.realized_pnl + _pnl(pos.side, pos.entry_price, current_price, pos.quantity)
        return equity

    def _record_risk_event(self, ts: pd.Timestamp, event: str, detail: str) -> None:
        self.account_risk_events.append({"timestamp": ts.isoformat(), "event": event, "detail": detail})

    def _register_closed_trade_risk(self, ts: pd.Timestamp, net_pnl: float) -> None:
        date_key = ts.date().isoformat()
        self.daily_realized_pnl[date_key] = self.daily_realized_pnl.get(date_key, 0.0) + net_pnl
        if net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        daily_limit = -self.initial_capital * self.max_daily_loss_pct
        if self.daily_realized_pnl[date_key] <= daily_limit and date_key not in self.daily_blocked_dates:
            self.daily_blocked_dates.add(date_key)
            self._record_risk_event(ts, "daily_loss_stop", f"date={date_key} realized={self.daily_realized_pnl[date_key]:.4f}")

        if self.consecutive_losses >= self.max_consecutive_losses:
            cooldown_until = ts + pd.Timedelta(seconds=self.consecutive_loss_cooldown_seconds)
            if self.account_cooldown_until is None or cooldown_until > self.account_cooldown_until:
                self.account_cooldown_until = cooldown_until
                self._record_risk_event(ts, "consecutive_loss_stop", f"losses={self.consecutive_losses} until={cooldown_until.isoformat()}")

    def _can_open_new_position(self, ts: pd.Timestamp, side: str) -> bool:
        date_key = ts.date().isoformat()
        if date_key in self.daily_blocked_dates:
            return False
        if self.account_cooldown_until is not None and ts < self.account_cooldown_until:
            return False
        side_count = sum(1 for pos in self.positions.values() if pos.side == side)
        if side_count >= self.max_same_side_positions:
            return False
        return True

    def _close_position(self, ts: pd.Timestamp, pos: Position, exit_price: float, reason: str) -> Dict[str, object]:
        gross_pnl = _pnl(pos.side, pos.entry_price, exit_price, pos.quantity)
        exit_fee = abs(exit_price * pos.quantity) * self.fee_rate
        net_pnl = pos.realized_pnl + gross_pnl - pos.fees_paid - exit_fee
        self.cash += pos.margin + net_pnl
        hold_bars = max(1, int((ts - pos.entry_time) / pd.Timedelta(minutes=15)))
        self.trades.append(
            TradeRecord(
                symbol=pos.symbol,
                side=pos.side,
                entry_time=pos.entry_time.isoformat(),
                exit_time=ts.isoformat(),
                entry_price=round(pos.entry_price, 8),
                exit_price=round(exit_price, 8),
                quantity=round(pos.quantity, 8),
                margin=round(pos.margin, 8),
                leverage=pos.leverage,
                pnl=round(net_pnl, 8),
                pnl_pct_on_margin=round(net_pnl / pos.margin if pos.margin > 0 else 0.0, 8),
                hold_bars=hold_bars,
                exit_reason=reason,
                entry_models=pos.entry_models,
                tp1_hit=pos.tp1_hit,
            )
        )
        self.positions.pop(pos.symbol, None)
        self.cooldown_until[pos.symbol] = ts + pd.Timedelta(minutes=15 * self.mode.cooldown_bars)
        self._register_closed_trade_risk(ts, net_pnl)
        return {
            "action": "CLOSE",
            "reason": reason,
            "exit_price": round(exit_price, 8),
            "net_pnl": round(net_pnl, 8),
            "hold_bars": hold_bars,
        }
    def _maybe_manage_position(self, ts: pd.Timestamp, symbol: str, row: pd.Series) -> Dict[str, object]:
        pos = self.positions.get(symbol)
        if pos is None:
            return {"action": "FLAT", "reason": "no_position"}
        high_price = _as_float(row.get("high"))
        low_price = _as_float(row.get("low"))
        current_close = _as_float(row.get("close"))
        ema10 = _as_float(row.get("ema10"))
        ema30 = _as_float(row.get("ema30"))
        stop_break_buffer_pct = float(self.rule_cfg.get("stop_break_buffer_pct", 0.0))
        runner_activate_pct = float(self.rule_cfg.get("runner_activate_pct", 0.05))
        macd_cross = str(row.get("macd_cross", "NONE")).upper()
        macd_zone = str(row.get("macd_zone", "NEAR_ZERO")).upper()
        current_pnl_ratio = (
            (current_close - pos.entry_price) / pos.entry_price if pos.side == "LONG" else (pos.entry_price - current_close) / pos.entry_price
        )
        tp1_reduced = False

        if pos.side == "LONG":
            if (not pos.tp1_hit) and high_price >= pos.tp1_price:
                qty_reduce = pos.quantity * float(self.rule_cfg.get("tp1_reduce_pct", 0.5))
                gross_pnl = _pnl(pos.side, pos.entry_price, pos.tp1_price, qty_reduce)
                fee = abs(pos.tp1_price * qty_reduce) * self.fee_rate
                pos.realized_pnl += gross_pnl - fee
                pos.quantity -= qty_reduce
                pos.tp1_hit = True
                tp1_reduced = True
        else:
            if (not pos.tp1_hit) and low_price <= pos.tp1_price:
                qty_reduce = pos.quantity * float(self.rule_cfg.get("tp1_reduce_pct", 0.5))
                gross_pnl = _pnl(pos.side, pos.entry_price, pos.tp1_price, qty_reduce)
                fee = abs(pos.tp1_price * qty_reduce) * self.fee_rate
                pos.realized_pnl += gross_pnl - fee
                pos.quantity -= qty_reduce
                pos.tp1_hit = True
                tp1_reduced = True

        if pos.quantity <= 0:
            self.positions.pop(symbol, None)
            self.cooldown_until[symbol] = ts + pd.Timedelta(minutes=15 * self.mode.cooldown_bars)
            return {"action": "TP1_FULL_EXIT", "reason": "tp1_consumed_position"}

        runner_active = pos.tp1_hit or current_pnl_ratio >= runner_activate_pct
        anchor = ema10 if runner_active and ema10 > 0 else ema30
        if anchor > 0:
            pos.stop_price = anchor * (1.0 - stop_break_buffer_pct) if pos.side == "LONG" else anchor * (1.0 + stop_break_buffer_pct)

        event: Dict[str, object] = {
            "action": "HOLD_POS",
            "reason": "position_hold",
            "runner_active": bool(runner_active),
            "tp1_reduced": bool(tp1_reduced),
            "current_pnl_ratio": round(current_pnl_ratio, 8),
        }

        if pos.side == "LONG":
            ema_trigger = current_close < pos.stop_price if pos.stop_price > 0 else False
            macd_trigger = macd_cross == "DEAD"
            if ema_trigger or macd_trigger:
                reason = "RUNNER_EMA10" if runner_active and ema_trigger else (
                    "RUNNER_MACD" if runner_active and macd_trigger else (
                        "STOP_EMA30" if ema_trigger else "STOP_MACD"
                    )
                )
                close_event = self._close_position(ts, pos, current_close, reason)
                close_event["tp1_reduced"] = bool(tp1_reduced)
                close_event["runner_active"] = bool(runner_active)
                close_event["current_pnl_ratio"] = round(current_pnl_ratio, 8)
                return close_event
        else:
            ema_trigger = current_close > pos.stop_price if pos.stop_price > 0 else False
            macd_trigger = macd_cross == "GOLDEN" and macd_zone == "BELOW_ZERO"
            if ema_trigger or macd_trigger:
                reason = "RUNNER_EMA10" if runner_active and ema_trigger else (
                    "RUNNER_MACD" if runner_active and macd_trigger else (
                        "STOP_EMA30" if ema_trigger else "STOP_MACD"
                    )
                )
                close_event = self._close_position(ts, pos, current_close, reason)
                close_event["tp1_reduced"] = bool(tp1_reduced)
                close_event["runner_active"] = bool(runner_active)
                close_event["current_pnl_ratio"] = round(current_pnl_ratio, 8)
                return close_event

        if tp1_reduced:
            event["action"] = "TP1_REDUCE"
            event["reason"] = "tp1_hit_reduce_half"
        return event
    def _build_candidates(self, ts: pd.Timestamp) -> List[dict]:
        if self.allowed_entry_hours is not None and ts.hour not in self.allowed_entry_hours:
            return []
        candidates: List[dict] = []
        min_stop_pct = float(self.rule_cfg.get("min_stop_pct", 0.02))
        max_stop_pct = float(self.rule_cfg.get("max_stop_pct", 0.05))
        stop_buffer_pct = float(self.rule_cfg.get("stop_buffer_pct", 0.003))
        stop_break_buffer_pct = float(self.rule_cfg.get("stop_break_buffer_pct", 0.0))
        tp1_min_pct = float(self.rule_cfg.get("tp1_min_pct", 0.05))
        tp1_max_pct = float(self.rule_cfg.get("tp1_max_pct", 0.1))
        ema_band_pct = float(self.rule_cfg.get("ema_band_pct", 0.001))
        flat_slope_threshold = float(self.rule_cfg.get("ema_flat_slope_pct_threshold", 0.00015))

        for symbol, df in self.market.items():
            if symbol in self.positions:
                continue
            cooldown_until = self.cooldown_until.get(symbol)
            if cooldown_until is not None and ts < cooldown_until:
                continue
            row = _row_at(df, ts)
            if row is None:
                continue
            prev_row = _prev_row(df, ts)
            if any(pd.isna(row.get(key)) for key in ("ema30_1h", "ema30", "ema10")):
                continue
            regime, direction = _regime(row, ema_band_pct, flat_slope_threshold, self.mode.min_1h_slope_pct)
            if regime != "TREND":
                continue
            models, signal_bars_ago = _entry_models(row, prev_row, direction, self.mode)
            if len(models) < 1:
                continue
            side = "LONG" if direction == "LONG_ONLY" else "SHORT"
            if not self._can_open_new_position(ts, side):
                continue
            risk = _risk_plan(
                side=side,
                entry_price=_as_float(row.get("close")),
                stop_anchor=_as_float(row.get("ema30")),
                entry_models=models,
                min_stop_pct=min_stop_pct,
                max_stop_pct=max_stop_pct,
                stop_buffer_pct=stop_buffer_pct,
                stop_break_buffer_pct=stop_break_buffer_pct,
                tp1_min_pct=tp1_min_pct,
                tp1_max_pct=tp1_max_pct,
            )
            if not risk:
                continue
            candidates.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "models": models,
                    "row": row,
                    "risk": risk,
                    "rank": _candidate_rank(row, side, models, risk),
                    "signal_bars_ago": signal_bars_ago,
                }
            )

        candidates.sort(key=lambda item: item["rank"], reverse=True)
        return candidates

    def _open_position(self, ts: pd.Timestamp, candidate: dict) -> Optional[Dict[str, object]]:
        row = candidate["row"]
        side = str(candidate["side"])
        ref_price = _as_float(row.get("close"))
        entry_price = ref_price * (1.0 + self.entry_slippage) if side == "LONG" else ref_price * (1.0 - self.entry_slippage)
        equity = self._mark_equity(ts)
        target_margin = min(self.cash, equity * self.mode.margin_pct_per_trade)
        if target_margin <= 0:
            return None
        notional = target_margin * self.leverage
        quantity = notional / entry_price if entry_price > 0 else 0.0
        entry_fee = abs(notional) * self.fee_rate
        if self.cash < target_margin + entry_fee:
            target_margin = max(0.0, self.cash - entry_fee)
            notional = target_margin * self.leverage
            quantity = notional / entry_price if entry_price > 0 else 0.0
        if target_margin <= 0 or quantity <= 0:
            return None

        self.cash -= target_margin + entry_fee
        self.positions[str(candidate["symbol"])] = Position(
            symbol=str(candidate["symbol"]),
            side=side,
            entry_time=ts,
            entry_price=entry_price,
            quantity=quantity,
            margin=target_margin,
            leverage=self.leverage,
            stop_price=float(candidate["risk"]["stop_trigger_price"]),
            tp1_price=float(candidate["risk"]["tp1_price"]),
            fees_paid=entry_fee,
            entry_models="+".join(list(candidate["models"])),
            resonance_count=len(list(candidate["models"])),
        )
        signal_bars_ago = candidate.get("signal_bars_ago") or {}
        return {
            "action": "OPEN",
            "reason": "+".join(list(candidate["models"])),
            "side": side,
            "entry_price": round(entry_price, 8),
            "target_margin": round(target_margin, 8),
            "quantity": round(quantity, 8),
            "stop_price": round(float(candidate["risk"]["stop_trigger_price"]), 8),
            "tp1_price": round(float(candidate["risk"]["tp1_price"]), 8),
            "ema_macd_signal_bars_ago": signal_bars_ago.get("EMA_MACD"),
            "ema_bb_signal_bars_ago": signal_bars_ago.get("EMA_BB"),
        }
    def run(self) -> Dict[str, object]:
        self.load_market()
        self.bar_logs = []
        timeline = sorted(set().union(*[set(df.index) for df in self.market.values()]))
        for ts_obj in timeline:
            ts = pd.Timestamp(ts_obj)
            if self.eval_start and ts < self.eval_start:
                continue

            manage_events: Dict[str, Dict[str, object]] = {}
            rows_at_ts: Dict[str, pd.Series] = {}
            position_before_map: Dict[str, Dict[str, object]] = {}
            for symbol, df in self.market.items():
                row = _row_at(df, ts)
                if row is None:
                    continue
                rows_at_ts[symbol] = row
                position_before_map[symbol] = self._position_snapshot(symbol, row)
                if symbol in self.positions:
                    manage_events[symbol] = self._maybe_manage_position(ts, symbol, row)

            open_events: Dict[str, Dict[str, object]] = {}
            candidates_all = self._build_candidates(ts)
            free_slots = self.max_active_symbols - len(self.positions)
            if free_slots > 0:
                for candidate in candidates_all[:free_slots]:
                    if len(self.positions) >= self.max_active_symbols:
                        break
                    if str(candidate["symbol"]) in self.positions:
                        continue
                    if not self._can_open_new_position(ts, str(candidate["side"])):
                        continue
                    open_event = self._open_position(ts, candidate)
                    if isinstance(open_event, dict):
                        open_events[str(candidate["symbol"])] = open_event

            candidate_map = {str(item["symbol"]): item for item in candidates_all}
            for symbol, row in rows_at_ts.items():
                position_before = position_before_map.get(symbol, self._position_snapshot(symbol, row))
                position_after = self._position_snapshot(symbol, row)
                event = None
                action = "BAR"
                reason = "no_signal"
                if symbol in manage_events:
                    event = manage_events[symbol]
                    action = str(event.get("action", "BAR"))
                    reason = str(event.get("reason", "manage"))
                elif symbol in open_events:
                    event = open_events[symbol]
                    action = str(event.get("action", "BAR"))
                    reason = str(event.get("reason", "open"))
                elif symbol in candidate_map:
                    candidate = candidate_map[symbol]
                    action = "CANDIDATE"
                    reason = "+".join(list(candidate.get("models") or [])) or "candidate"
                    signal_bars_ago = candidate.get("signal_bars_ago") or {}
                    event = {
                        "side": candidate.get("side"),
                        "rank": candidate.get("rank"),
                        "stop_price": round(float(candidate.get("risk", {}).get("stop_trigger_price", 0.0)), 8),
                        "tp1_price": round(float(candidate.get("risk", {}).get("tp1_price", 0.0)), 8),
                        "ema_macd_signal_bars_ago": signal_bars_ago.get("EMA_MACD"),
                        "ema_bb_signal_bars_ago": signal_bars_ago.get("EMA_BB"),
                    }
                elif position_after.get("position_side") != "FLAT":
                    action = "HOLD_POS"
                    reason = "position_carry"
                self._append_bar_log(
                    ts=ts,
                    symbol=symbol,
                    row=row,
                    position_before=position_before,
                    position_after=position_after,
                    action=action,
                    reason=reason,
                    event=event,
                )

            equity = self._mark_equity(ts)
            self.equity_curve.append({"timestamp": ts.isoformat(), "equity": equity, "cash": self.cash, "open_positions": len(self.positions)})

        if self.eval_end is not None:
            for symbol, pos in list(self.positions.items()):
                df = self.market[symbol]
                eligible = df.index[df.index <= self.eval_end]
                if len(eligible) == 0:
                    continue
                last_ts = pd.Timestamp(eligible[-1])
                last_row = _row_at(df, last_ts)
                if last_row is None:
                    continue
                position_before = self._position_snapshot(symbol, last_row)
                last_close = _as_float(last_row.get("close"))
                end_event = self._close_position(last_ts, pos, last_close, "END_OF_TEST")
                position_after = self._position_snapshot(symbol, last_row)
                self._append_bar_log(
                    ts=last_ts,
                    symbol=symbol,
                    row=last_row,
                    position_before=position_before,
                    position_after=position_after,
                    action=str(end_event.get("action", "CLOSE")),
                    reason=str(end_event.get("reason", "END_OF_TEST")),
                    event=end_event,
                )

        return self._summary()
    def _summary(self) -> Dict[str, object]:
        equity_df = pd.DataFrame(self.equity_curve)
        if equity_df.empty:
            raise RuntimeError("没有生成权益曲线")
        equity_df["peak"] = equity_df["equity"].cummax()
        equity_df["drawdown"] = equity_df["equity"] / equity_df["peak"] - 1.0
        trades_df = pd.DataFrame([asdict(t) for t in self.trades])
        if trades_df.empty:
            trades_df = pd.DataFrame(columns=list(TradeRecord.__annotations__.keys()))

        final_equity = _as_float(equity_df["equity"].iloc[-1])
        total_return = (final_equity / self.initial_capital) - 1.0
        max_drawdown = _as_float(equity_df["drawdown"].min())
        trade_count = int(len(trades_df))
        win_rate = _as_float((trades_df["pnl"] > 0).mean()) if trade_count else 0.0
        avg_pnl = _as_float(trades_df["pnl"].mean()) if trade_count else 0.0
        profit_factor = 0.0
        if trade_count:
            gross_profit = _as_float(trades_df.loc[trades_df["pnl"] > 0, "pnl"].sum())
            gross_loss = abs(_as_float(trades_df.loc[trades_df["pnl"] < 0, "pnl"].sum()))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

        analysis = _analyze_trades(trades_df)
        summary: Dict[str, object] = {
            "mode": self.mode.name,
            "profile_name": self.profile_name,
            "allowed_entry_hours": sorted(self.allowed_entry_hours) if self.allowed_entry_hours is not None else None,
            "symbols": self.symbols,
            "initial_capital": self.initial_capital,
            "final_equity": round(final_equity, 6),
            "total_return_pct": round(total_return * 100.0, 4),
            "max_drawdown_pct": round(max_drawdown * 100.0, 4),
            "trade_count": trade_count,
            "win_rate_pct": round(win_rate * 100.0, 4),
            "avg_trade_pnl": round(avg_pnl, 6),
            "profit_factor": round(profit_factor, 6),
            "max_active_symbols": self.max_active_symbols,
            "max_same_side_positions": self.max_same_side_positions,
            "leverage": self.leverage,
            "margin_pct_per_trade": round(self.mode.margin_pct_per_trade, 4),
            "cooldown_bars": self.mode.cooldown_bars,
            "min_1h_slope_pct": self.mode.min_1h_slope_pct,
            "min_macd_hist_pct": self.mode.min_macd_hist_pct,
            "min_bb_width_delta": self.mode.min_bb_width_delta,
            "require_dual_models": self.mode.require_dual_models,
            "entry_signal_carry_bars": self.mode.entry_signal_carry_bars,
            "entry_model_logic": "ANY_ONE_MODEL",
            "max_daily_loss_percent": round(self.max_daily_loss_pct * 100.0, 4),
            "max_consecutive_losses": self.max_consecutive_losses,
            "consecutive_loss_cooldown_seconds": self.consecutive_loss_cooldown_seconds,
            "risk_events": self.account_risk_events,
            "analysis": analysis,
            "days": self.args.days,
            "warmup_days": self.args.warmup_days,
        }

        os.makedirs(self.report_dir, exist_ok=True)
        run_tag = pd.Timestamp.utcnow().strftime("%Y%m%d_%H%M%S")
        file_tag = f"{self.mode.name}_{run_tag}"
        summary_path = os.path.join(self.report_dir, f"summary_{file_tag}.json")
        trades_path = os.path.join(self.report_dir, f"trades_{file_tag}.csv")
        equity_path = os.path.join(self.report_dir, f"equity_{file_tag}.csv")
        bar_log_path = os.path.join(self.report_dir, f"barlog_{file_tag}.csv")

        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")
        equity_df.drop(columns=["peak", "drawdown"]).to_csv(equity_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(self.bar_logs).to_csv(bar_log_path, index=False, encoding="utf-8-sig")

        summary["summary_path"] = summary_path
        summary["trades_path"] = trades_path
        summary["equity_path"] = equity_path
        summary["bar_log_path"] = bar_log_path
        return summary


def _compare_summaries(loose: Dict[str, object], strict: Dict[str, object]) -> Dict[str, object]:
    loose_ret = _as_float(loose.get("total_return_pct"))
    strict_ret = _as_float(strict.get("total_return_pct"))
    loose_dd = _as_float(loose.get("max_drawdown_pct"))
    strict_dd = _as_float(strict.get("max_drawdown_pct"))
    loose_pf = _as_float(loose.get("profit_factor"))
    strict_pf = _as_float(strict.get("profit_factor"))

    score_loose = loose_ret + loose_dd * 0.35 + loose_pf * 10.0
    score_strict = strict_ret + strict_dd * 0.35 + strict_pf * 10.0
    better_mode = "strict" if score_strict > score_loose else "loose"

    return {
        "better_mode": better_mode,
        "return_gap_pct": round(strict_ret - loose_ret, 4),
        "drawdown_gap_pct": round(strict_dd - loose_dd, 4),
        "profit_factor_gap": round(strict_pf - loose_pf, 6),
        "trade_gap": int(_as_float(strict.get("trade_count")) - _as_float(loose.get("trade_count"))),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1H EMA30 + 15M EMA/MACD/Bollinger rule backtest")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "config", "trading_config_fund_flow.json"))
    parser.add_argument("--symbols", default="", help="Comma separated symbols, default from config")
    parser.add_argument("--days", type=int, default=30, help="Evaluation days")
    parser.add_argument("--warmup-days", type=int, default=7, help="Extra days for indicator warmup")
    parser.add_argument("--initial-capital", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=0.0, help="0 means use config default leverage")
    parser.add_argument("--fee-rate", type=float, default=0.0004, help="One-way fee rate")
    parser.add_argument("--slippage", type=float, default=None, help="One-way slippage rate, default from config")
    parser.add_argument("--data-dir", default=os.path.join(PROJECT_ROOT, "data", "backtest_klines"))
    parser.add_argument("--report-dir", default=os.path.join(PROJECT_ROOT, "output", "backtest"))
    parser.add_argument("--refresh", action="store_true", help="Force re-download klines")
    parser.add_argument("--mode", choices=["loose", "strict", "compare"], default="compare")
    parser.add_argument("--profile", default="", help="Backtest profile name from config.fund_flow.backtest.profiles")
    parser.add_argument("--max-same-side-positions", type=int, default=1)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = _read_json(args.config)
    try:
        profile_name, profile = _resolve_backtest_profile(config, args.profile)
    except KeyError as exc:
        raise SystemExit(str(exc))
    args = _apply_profile_to_args(args, profile_name, profile)
    mode_overrides = profile.get("mode_overrides") if isinstance(profile.get("mode_overrides"), dict) else {}
    mode_map = {name: _merge_mode_config(mode_cfg, mode_overrides) for name, mode_cfg in DEFAULT_MODES.items()}
    if args.mode == "compare":
        loose_summary = RuleBacktester(args, mode_map["loose"]).run()
        strict_summary = RuleBacktester(args, mode_map["strict"]).run()
        result = {
            "mode": "compare",
            "profile_name": profile_name,
            "loose": loose_summary,
            "strict": strict_summary,
            "comparison": _compare_summaries(loose_summary, strict_summary),
        }
    else:
        result = RuleBacktester(args, mode_map[args.mode]).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())








