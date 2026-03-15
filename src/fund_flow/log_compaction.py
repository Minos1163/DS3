from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Mapping


_METADATA_SCALAR_KEYS = (
    "trigger",
    "engine",
    "regime",
    "regime_reason",
    "decision_source",
    "side",
    "score",
    "long_score",
    "short_score",
    "direction_lock",
    "direction_lock_mode",
    "entry_mode",
    "entry_stage",
    "entry_size_mult",
    "close_threshold",
    "ds_confidence",
    "ds_source",
    "selected_pool_id",
    "signal_pool_id",
    "flow_confirm",
    "consistency_3bars",
    "allow_entry_window",
    "reverse_close_filter",
    "decision_confirm",
    "direction_neutral_trial_active",
    "direction_neutral_trial_mode",
    "direction_neutral_trial_reason",
    "pnl_pct",
    "regime_adx",
    "regime_atr_pct",
)

_METADATA_OBJECT_KEYS = ("score_15m", "score_5m", "final_score", "fusion_info")

_FLOW_SCALAR_KEYS = (
    "cvd_ratio",
    "cvd_momentum",
    "oi_delta_ratio",
    "funding_rate",
    "depth_ratio",
    "imbalance",
    "liquidity_delta_norm",
    "mid_price",
    "microprice",
    "micro_delta_norm",
    "spread_bps",
    "phantom",
    "trap_score",
    "signal_strength",
    "active_timeframe",
    "_ma10_macd_confluence",
)

_FLOW_TIMEFRAME_KEYS = (
    "cvd_ratio",
    "cvd_momentum",
    "oi_delta_ratio",
    "funding_rate",
    "depth_ratio",
    "imbalance",
    "liquidity_delta_norm",
    "micro_delta_norm",
    "spread_bps",
    "phantom",
    "trap_score",
    "signal_strength",
)

_EXECUTION_SCALAR_KEYS = (
    "status",
    "code",
    "msg",
    "message",
    "retry_index",
    "quantity",
    "filled_qty",
    "executedQty",
    "avg_price",
    "avgPrice",
    "reduce_only",
)


def compact_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def compact_decision_payload(decision: Any) -> Dict[str, Any]:
    raw = _as_mapping(decision)
    metadata = raw.get("metadata")
    payload: Dict[str, Any] = {
        "operation": raw.get("operation"),
        "symbol": raw.get("symbol"),
        "target_portion_of_balance": _compact_value(raw.get("target_portion_of_balance")),
        "leverage": _compact_value(raw.get("leverage")),
        "max_price": _compact_value(raw.get("max_price")),
        "min_price": _compact_value(raw.get("min_price")),
        "time_in_force": raw.get("time_in_force"),
        "take_profit_price": _compact_value(raw.get("take_profit_price")),
        "stop_loss_price": _compact_value(raw.get("stop_loss_price")),
        "tp_execution": raw.get("tp_execution"),
        "sl_execution": raw.get("sl_execution"),
        "reason": _compact_string(raw.get("reason"), 200),
    }
    compact_metadata = compact_decision_metadata(metadata)
    if compact_metadata:
        payload["metadata"] = compact_metadata
    return _drop_empty(payload)


def compact_decision_metadata(metadata: Any) -> Dict[str, Any]:
    raw = _as_mapping(metadata)
    if not raw:
        return {}
    compact = _pick_scalars(raw, _METADATA_SCALAR_KEYS)
    for key in _METADATA_OBJECT_KEYS:
        value = raw.get(key)
        if isinstance(value, Mapping):
            compact[key] = _compact_dict(
                value,
                preferred_keys=("long", "short", "score", "signal_strength", "confidence", "direction"),
                max_items=8,
            )
    omitted = len(raw) - len(compact)
    if omitted > 0:
        compact["_omitted_keys"] = omitted
    return _drop_empty(compact)


def compact_flow_context_payload(flow_context: Any) -> Dict[str, Any]:
    raw = _as_mapping(flow_context)
    if not raw:
        return {}
    compact = _pick_scalars(raw, _FLOW_SCALAR_KEYS)
    timeframes = raw.get("timeframes")
    if isinstance(timeframes, Mapping):
        active = str(raw.get("active_timeframe") or "").strip().lower()
        wanted: List[str] = []
        for candidate in (active, "5m", "15m"):
            if candidate and candidate not in wanted and candidate in timeframes:
                wanted.append(candidate)
        tf_payload: Dict[str, Any] = {}
        for tf in wanted:
            value = timeframes.get(tf)
            if isinstance(value, Mapping):
                tf_payload[tf] = _pick_scalars(value, _FLOW_TIMEFRAME_KEYS)
        if tf_payload:
            compact["timeframes"] = tf_payload
        omitted = len(timeframes) - len(tf_payload)
        if omitted > 0:
            compact["timeframes_omitted"] = omitted
    return _drop_empty(compact)


def compact_portfolio_payload(portfolio: Any) -> Dict[str, Any]:
    raw = _as_mapping(portfolio)
    if not raw:
        return {}
    positions = raw.get("positions")
    active_symbols: List[str] = []
    if isinstance(positions, Mapping):
        for symbol, value in positions.items():
            if len(active_symbols) >= 6:
                break
            if _position_is_active(value):
                active_symbols.append(str(symbol))
    compact = {
        "cash": _compact_value(raw.get("cash")),
        "total_assets": _compact_value(raw.get("total_assets")),
        "position_count": len(positions) if isinstance(positions, Mapping) else 0,
        "active_symbols": active_symbols,
    }
    return _drop_empty(compact)


def compact_trigger_context_payload(trigger_context: Any) -> Dict[str, Any]:
    return _compact_dict(trigger_context, max_items=8)


def compact_execution_result_payload(result: Any) -> Dict[str, Any]:
    raw = _as_mapping(result)
    if not raw:
        return {}
    compact = _pick_scalars(raw, _EXECUTION_SCALAR_KEYS)
    order = raw.get("order")
    if isinstance(order, Mapping):
        compact["order"] = _pick_scalars(
            order,
            ("orderId", "status", "executedQty", "origQty", "price", "avgPrice", "side", "type", "timeInForce"),
        )
    protection = raw.get("protection")
    if isinstance(protection, Mapping):
        orders = protection.get("orders")
        compact_orders: List[Dict[str, Any]] = []
        if isinstance(orders, list):
            for item in orders[:4]:
                compact_orders.append(
                    _pick_scalars(item, ("type", "orderId", "status", "stopPrice", "price"))
                )
        compact["protection"] = _drop_empty(
            {
                "status": protection.get("status"),
                "orders": compact_orders,
            }
        )
    quantity_info = raw.get("quantity_info")
    if isinstance(quantity_info, Mapping):
        compact["quantity_info"] = _pick_scalars(
            quantity_info,
            ("requested_qty", "formatted_qty", "promoted_to_full_close", "promotion_reason"),
        )
    return _drop_empty(compact)


def _pick_scalars(data: Mapping[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in keys:
        if key not in data:
            continue
        value = _compact_value(data.get(key))
        if value is None or value == "":
            continue
        result[key] = value
    return result


def _compact_dict(
    value: Any,
    *,
    preferred_keys: Iterable[str] = (),
    max_items: int = 12,
) -> Dict[str, Any]:
    raw = _as_mapping(value)
    if not raw:
        return {}
    result: Dict[str, Any] = {}
    seen = set()
    for key in preferred_keys:
        if key in raw and len(result) < max_items:
            result[key] = _compact_value(raw.get(key))
            seen.add(key)
    for key, item in raw.items():
        if key in seen or len(result) >= max_items:
            continue
        compact = _compact_value(item)
        if compact is None or compact == "":
            continue
        result[str(key)] = compact
    omitted = len(raw) - len(result)
    if omitted > 0:
        result["_omitted_keys"] = omitted
    return _drop_empty(result)


def _as_mapping(value: Any) -> Dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            value = value.to_dict()
        except Exception:
            return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _compact_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 6)
    if isinstance(value, str):
        return _compact_string(value, 200)
    if isinstance(value, Mapping):
        return _compact_dict(value)
    if isinstance(value, list):
        items = []
        for item in value[:6]:
            compact = _compact_value(item)
            if compact is None or compact == "":
                continue
            items.append(compact)
        if len(value) > 6:
            items.append({"_omitted_items": len(value) - 6})
        return items
    return _compact_string(value, 120)


def _compact_string(value: Any, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 12]}...[{len(text)}]"


def _drop_empty(payload: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if value == "":
            continue
        if value == {}:
            continue
        if value == []:
            continue
        cleaned[key] = value
    return cleaned


def _position_is_active(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key in ("amount", "positionAmt", "size", "qty"):
            try:
                if abs(float(value.get(key) or 0.0)) > 0.0:
                    return True
            except Exception:
                continue
        return bool(value)
    try:
        return abs(float(value or 0.0)) > 0.0
    except Exception:
        return bool(value)
