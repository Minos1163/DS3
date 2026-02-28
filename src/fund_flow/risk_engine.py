from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from src.fund_flow.models import FundFlowDecision, Operation


class FundFlowRiskEngine:
    """
    资金流执行前统一风控校验：
    - operation/symbol 合法性
    - 仓位比例、杠杆边界
    - Hyper 风格价格边界钳制（默认 ±1%）
    """

    def __init__(
        self,
        config: Dict[str, Any],
        symbol_whitelist: Optional[Iterable[str]] = None,
    ) -> None:
        self.config = config or {}
        trading = self.config.get("trading", {}) or {}
        fund_flow_cfg = self.config.get("fund_flow", {}) or {}

        self.min_leverage = int(
            fund_flow_cfg.get("min_leverage", trading.get("min_leverage", 2))
        )
        max_lev_raw = int(
            fund_flow_cfg.get("max_leverage", trading.get("max_leverage", 20))
        )
        self.max_leverage = max(self.min_leverage, min(20, max_lev_raw))
        default_lev_raw = int(
            fund_flow_cfg.get("default_leverage", trading.get("default_leverage", self.min_leverage))
        )
        self.default_leverage = min(self.max_leverage, max(self.min_leverage, default_lev_raw))
        self.min_open_portion = float(fund_flow_cfg.get("min_open_portion", 0.08))
        self.max_open_portion = float(fund_flow_cfg.get("max_open_portion", 1.0))
        self.price_deviation_limit_percent = float(
            fund_flow_cfg.get("price_deviation_limit_percent", 1.0)
        )
        self.symbol_whitelist = {s.upper() for s in symbol_whitelist or []}

    def validate_symbol(self, symbol: str) -> None:
        if not symbol or not isinstance(symbol, str):
            raise ValueError("symbol 为空或非法")
        if self.symbol_whitelist and symbol.upper() not in self.symbol_whitelist:
            raise ValueError(f"symbol 不在白名单: {symbol}")

    def validate_operation(self, operation: Operation) -> None:
        if operation not in (
            Operation.BUY,
            Operation.SELL,
            Operation.HOLD,
            Operation.CLOSE,
        ):
            raise ValueError(f"operation 非法: {operation}")

    def clamp_leverage(self, leverage: Any) -> int:
        try:
            lev = int(leverage)
        except Exception:
            lev = self.default_leverage
        if lev < self.min_leverage:
            return self.min_leverage
        if lev > self.max_leverage:
            return self.max_leverage
        return int(lev)

    def validate_target_portion(self, portion: Any, operation: Operation) -> float:
        if operation == Operation.HOLD:
            return 0.0
        try:
            val = float(portion)
        except Exception:
            val = 0.0
        if operation == Operation.CLOSE:
            if val <= 0:
                return 1.0
            if val > 1.0:
                return 1.0
            return val
        if not (self.min_open_portion <= val <= self.max_open_portion):
            raise ValueError(
                f"target_portion_of_balance 越界: {val:.4f}, 要求 [{self.min_open_portion}, {self.max_open_portion}]"
            )
        return val

    def enforce_price_bounds(self, price: float, oracle_price: float) -> float:
        if price <= 0 or oracle_price <= 0:
            raise ValueError("price/oracle_price 必须大于 0")
        deviation = self.price_deviation_limit_percent / 100.0
        lower = oracle_price * (1.0 - deviation)
        upper = oracle_price * (1.0 + deviation)
        return min(max(price, lower), upper)

    def pick_entry_price(self, decision: FundFlowDecision, current_price: float) -> float:
        if decision.operation == Operation.BUY:
            return float(decision.max_price or current_price)
        if decision.operation == Operation.SELL:
            return float(decision.min_price or current_price)
        return float(current_price)

    def pick_close_price(self, decision: FundFlowDecision, current_price: float, position_side: str) -> float:
        side = str(position_side or "").upper()
        if side == "LONG":
            return float(decision.min_price or (current_price * 0.995))
        if side == "SHORT":
            return float(decision.max_price or (current_price * 1.005))
        return float(current_price)

    def align_close_price(self, close_price: float, current_price: float, position_side: str) -> float:
        side = str(position_side or "").upper()
        if side == "LONG" and close_price > current_price:
            return current_price * 0.9995
        if side == "SHORT" and close_price < current_price:
            return current_price * 1.0005
        return close_price

    def validate_decision(self, decision: FundFlowDecision) -> FundFlowDecision:
        self.validate_operation(decision.operation)
        self.validate_symbol(decision.symbol)
        decision.leverage = self.clamp_leverage(decision.leverage)
        decision.target_portion_of_balance = self.validate_target_portion(
            decision.target_portion_of_balance,
            decision.operation,
        )
        return decision
