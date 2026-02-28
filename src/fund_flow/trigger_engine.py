from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class TriggerState:
    last_seen_at: datetime
    last_trigger_id: Optional[str] = None
    seen_count: int = 0


@dataclass
class ConditionEdgeState:
    active: bool = False
    last_changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_triggered_at: Optional[datetime] = None
    seen_count: int = 0


class TriggerEngine:
    """
    双触发引擎（scheduled + signal）：
    - 对同一 symbol + trigger_type 去重
    - 支持 trigger_id（signal pool id）幂等
    """

    def __init__(
        self,
        dedupe_window_seconds: int = 10,
        signal_pool_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dedupe_window_seconds = max(1, int(dedupe_window_seconds))
        self._state: Dict[str, TriggerState] = {}
        self._condition_state: Dict[str, ConditionEdgeState] = {}
        self.signal_pool_config: Dict[str, Any] = (
            signal_pool_config if isinstance(signal_pool_config, dict) else {}
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def should_trigger(
        self,
        symbol: str,
        trigger_type: str,
        trigger_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        now = now or self._now()
        key = f"{symbol.upper()}:{trigger_type.lower()}"
        state = self._state.get(key)
        if state is None:
            self._state[key] = TriggerState(
                last_seen_at=now,
                last_trigger_id=trigger_id,
                seen_count=1,
            )
            return True

        elapsed = (now - state.last_seen_at).total_seconds()
        if trigger_id and state.last_trigger_id == trigger_id:
            state.last_seen_at = now
            state.seen_count += 1
            return False

        if elapsed < self.dedupe_window_seconds:
            state.last_seen_at = now
            state.last_trigger_id = trigger_id
            state.seen_count += 1
            return False

        state.last_seen_at = now
        state.last_trigger_id = trigger_id
        state.seen_count += 1
        return True

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return default

    @staticmethod
    def _normalize_side(value: Any) -> str:
        raw = getattr(value, "value", value)
        side = str(raw or "").strip().upper()
        if side in ("BUY", "LONG"):
            return "LONG"
        if side in ("SELL", "SHORT"):
            return "SHORT"
        return side

    @staticmethod
    def _extract_scores(decision: Any) -> Dict[str, float]:
        md = getattr(decision, "metadata", None)
        md_dict = md if isinstance(md, dict) else {}
        long_score = 0.0
        short_score = 0.0
        try:
            long_score = float(md_dict.get("long_score", 0.0) or 0.0)
        except Exception:
            long_score = 0.0
        try:
            short_score = float(md_dict.get("short_score", 0.0) or 0.0)
        except Exception:
            short_score = 0.0
        return {"long_score": long_score, "short_score": short_score}

    def set_signal_pool_config(self, signal_pool_config: Optional[Dict[str, Any]]) -> None:
        self.signal_pool_config = signal_pool_config if isinstance(signal_pool_config, dict) else {}
        # 配置变化后重置条件边沿状态，避免旧条件状态影响新规则
        self._condition_state = {}

    def _resolve_metric_value(
        self,
        metric: str,
        market_flow_context: Dict[str, Any],
        decision: Any,
        timeframe: Optional[str] = None,
    ) -> float:
        metric_key = str(metric or "").strip()
        tf = str(timeframe or "").strip()
        if metric_key.lower().startswith("tf:"):
            parts = metric_key.split(":", 2)
            if len(parts) == 3:
                tf = parts[1].strip()
                metric_key = parts[2].strip()
        aliases = {
            "cvd": "cvd_ratio",
            "oi_delta": "oi_delta_ratio",
            "depth": "depth_ratio",
            "liq_norm": "liquidity_delta_norm",
        }
        metric_key = aliases.get(metric_key, metric_key)
        if metric_key in ("long_score", "short_score"):
            return self._extract_scores(decision).get(metric_key, 0.0)
        if tf:
            timeframes = (market_flow_context or {}).get("timeframes")
            if isinstance(timeframes, dict):
                tf_ctx = timeframes.get(tf)
                if tf_ctx is None:
                    tf_ctx = timeframes.get(tf.lower()) or timeframes.get(tf.upper())
                if isinstance(tf_ctx, dict):
                    return self._to_float(tf_ctx.get(metric_key), 0.0)
        return self._to_float((market_flow_context or {}).get(metric_key), 0.0)

    @staticmethod
    def _compare(
        value: float,
        operator: str,
        threshold: float,
        threshold_max: Optional[float] = None,
    ) -> bool:
        op = str(operator or ">=").strip().lower()
        if op == ">":
            return value > threshold
        if op == ">=":
            return value >= threshold
        if op == "<":
            return value < threshold
        if op == "<=":
            return value <= threshold
        if op in ("==", "="):
            return value == threshold
        if op in ("!=", "<>"):
            return value != threshold
        if op in ("between", "range"):
            upper = threshold if threshold_max is None else threshold_max
            lower = min(threshold, upper)
            upper2 = max(threshold, upper)
            return lower <= value <= upper2
        return value >= threshold

    def _edge_trigger(
        self,
        *,
        key: str,
        condition_met: bool,
        now: Optional[datetime] = None,
        cooldown_seconds: int = 0,
    ) -> Dict[str, Any]:
        now = now or self._now()
        state = self._condition_state.get(key)
        if state is None:
            state = ConditionEdgeState(active=bool(condition_met), last_changed_at=now, seen_count=1)
            self._condition_state[key] = state
            if condition_met:
                state.last_triggered_at = now
                return {"triggered": True, "reason": "initial_true", "active": True}
            return {"triggered": False, "reason": "initial_false", "active": False}

        state.seen_count += 1
        if bool(condition_met) != bool(state.active):
            state.active = bool(condition_met)
            state.last_changed_at = now
            if state.active:
                if cooldown_seconds > 0 and state.last_triggered_at is not None:
                    elapsed = (now - state.last_triggered_at).total_seconds()
                    if elapsed < cooldown_seconds:
                        return {
                            "triggered": False,
                            "reason": "rising_edge_in_cooldown",
                            "active": True,
                            "cooldown_remaining": max(0, int(cooldown_seconds - elapsed)),
                        }
                state.last_triggered_at = now
                return {"triggered": True, "reason": "rising_edge", "active": True}
            return {"triggered": False, "reason": "falling_edge", "active": False}

        if state.active:
            return {"triggered": False, "reason": "steady_true", "active": True}
        return {"triggered": False, "reason": "steady_false", "active": False}

    def evaluate_signal_pool(
        self,
        *,
        symbol: str,
        trigger_type: str,
        market_flow_context: Dict[str, Any],
        decision: Any,
        has_position: bool,
        signal_pool_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        可配置 signal_pool 过滤器（不替代 should_trigger 去重）。
        仅对 BUY/SELL 生效；默认不阻断已有仓位的加仓，除非配置显式开启。
        """
        cfg = signal_pool_config if isinstance(signal_pool_config, dict) else self.signal_pool_config
        cfg = cfg if isinstance(cfg, dict) else {}
        if not cfg or not self._to_bool(cfg.get("enabled", False), False):
            return {"passed": True, "reason": "signal_pool_disabled"}

        if str(trigger_type).lower() == "scheduled" and self._to_bool(cfg.get("scheduled_trigger_bypass", True), True):
            return {"passed": True, "reason": "scheduled_bypass"}

        scoped_symbols = cfg.get("symbols")
        if isinstance(scoped_symbols, list) and scoped_symbols:
            symbol_set = {str(s).upper() for s in scoped_symbols if str(s).strip()}
            if symbol_set and symbol.upper() not in symbol_set:
                return {"passed": False, "reason": "symbol_not_in_pool_scope"}

        operation = self._normalize_side(getattr(decision, "operation", ""))
        if operation not in ("LONG", "SHORT"):
            return {"passed": True, "reason": "non_entry_operation"}

        apply_when_position_exists = self._to_bool(cfg.get("apply_when_position_exists", False), False)
        if has_position and not apply_when_position_exists:
            return {"passed": True, "reason": "position_exists_bypass"}

        scores = self._extract_scores(decision)
        side_score = scores["long_score"] if operation == "LONG" else scores["short_score"]
        min_score_key = "min_long_score" if operation == "LONG" else "min_short_score"
        min_score_required = self._to_float(cfg.get(min_score_key), 0.0)
        if side_score < min_score_required:
            return {
                "passed": False,
                "reason": f"{min_score_key}_not_met",
                "score": side_score,
                "required": min_score_required,
            }

        raw_rules = cfg.get("rules")
        rules = raw_rules if isinstance(raw_rules, list) else []
        active_rules: List[Dict[str, Any]] = []
        for item in rules:
            if not isinstance(item, dict):
                continue
            rule_side = self._normalize_side(item.get("side", "BOTH"))
            if rule_side in ("", "BOTH", operation):
                active_rules.append(item)

        if not active_rules:
            return {
                "passed": True,
                "reason": "score_gate_only",
                "score": side_score,
                "required": min_score_required,
            }

        evaluations: List[Dict[str, Any]] = []
        pass_count = 0
        for idx, rule in enumerate(active_rules, start=1):
            metric = str(rule.get("metric", "")).strip()
            if not metric:
                continue
            timeframe = str(rule.get("timeframe", "")).strip()
            value = self._resolve_metric_value(
                metric,
                market_flow_context or {},
                decision,
                timeframe=timeframe or None,
            )
            operator = str(rule.get("operator", ">=")).strip()
            threshold = self._to_float(rule.get("threshold"), 0.0)
            threshold_max = rule.get("threshold_max")
            if isinstance(rule.get("threshold"), list):
                threshold_list = rule.get("threshold") or []
                if len(threshold_list) >= 2:
                    threshold = self._to_float(threshold_list[0], threshold)
                    threshold_max = self._to_float(threshold_list[1], threshold)
            th_max_val = self._to_float(threshold_max, threshold) if threshold_max is not None else None
            passed = self._compare(value, operator, threshold, th_max_val)
            if passed:
                pass_count += 1
            evaluations.append(
                {
                    "index": idx,
                    "name": str(rule.get("name", f"rule_{idx}")),
                    "metric": metric,
                    "operator": operator,
                    "threshold": threshold,
                    "threshold_max": th_max_val,
                    "value": value,
                    "passed": passed,
                }
            )

        total_rules = len(evaluations)
        if total_rules <= 0:
            return {"passed": True, "reason": "empty_rules_after_filter", "score": side_score}

        min_pass_count = int(self._to_float(cfg.get("min_pass_count"), 0))
        logic = str(cfg.get("logic", "AND")).strip().upper()
        if min_pass_count > 0:
            required = min(total_rules, min_pass_count)
            passed = pass_count >= required
            reason = f"min_pass_count({pass_count}/{required})"
        elif logic == "OR":
            passed = pass_count > 0
            reason = f"logic_or({pass_count}/{total_rules})"
        else:
            passed = pass_count == total_rules
            reason = f"logic_and({pass_count}/{total_rules})"

        edge_enabled = self._to_bool(cfg.get("edge_trigger_enabled", True), True)
        edge_cd = max(0, int(self._to_float(cfg.get("edge_cooldown_seconds", 0), 0.0)))
        edge_info: Dict[str, Any] = {"triggered": passed, "reason": "edge_disabled", "active": bool(passed)}
        final_passed = passed
        if edge_enabled:
            pool_id = str(cfg.get("pool_id", cfg.get("id", "default")) or "default")
            edge_key = f"{symbol.upper()}:{pool_id}:{operation}"
            edge_info = self._edge_trigger(
                key=edge_key,
                condition_met=passed,
                cooldown_seconds=edge_cd,
            )
            final_passed = bool(edge_info.get("triggered", False))

        return {
            "passed": final_passed,
            "reason": reason,
            "score": side_score,
            "evaluations": evaluations,
            "pass_count": pass_count,
            "total_rules": total_rules,
            "side": operation,
            "edge": edge_info,
            "condition_met": bool(passed),
        }
