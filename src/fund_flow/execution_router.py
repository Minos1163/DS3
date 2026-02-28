from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from src.fund_flow.attribution_engine import FundFlowAttributionEngine
from src.fund_flow.models import FundFlowDecision, Operation, TimeInForce
from src.fund_flow.risk_engine import FundFlowRiskEngine
from src.trading.intents import PositionSide as IntentPositionSide


class FundFlowExecutionRouter:
    """
    统一执行器：
    - 开仓: IOC -> GTC 回退 + TP/SL
    - 平仓: IOC 多次重试 + GTC reduce-only 兜底
    """

    def __init__(
        self,
        client: Any,
        risk_engine: FundFlowRiskEngine,
        attribution_engine: FundFlowAttributionEngine,
        close_retry_times: int = 4,
    ) -> None:
        self.client = client
        self.risk = risk_engine
        self.attribution = attribution_engine
        ff_cfg = ((self.risk.config or {}).get("fund_flow", {}) or {})
        degrade_cfg = ff_cfg.get("execution_degradation", {}) or {}
        if not isinstance(degrade_cfg, dict):
            degrade_cfg = {}

        self.open_ioc_retry_times = max(1, int(degrade_cfg.get("open_ioc_retry_times", 1) or 1))
        self.open_ioc_retry_step_bps = max(0.0, self._to_float(degrade_cfg.get("open_ioc_retry_step_bps", 10.0), 10.0))
        self.open_gtc_fallback_enabled = self._to_bool(degrade_cfg.get("open_gtc_fallback_enabled", True), True)
        self.open_market_fallback_enabled = self._to_bool(degrade_cfg.get("open_market_fallback_enabled", False), False)

        close_retry_default = max(1, int(close_retry_times))
        self.close_retry_times = max(
            1,
            int(degrade_cfg.get("close_ioc_retry_times", close_retry_default) or close_retry_default),
        )
        self.close_ioc_retry_step_bps = max(0.0, self._to_float(degrade_cfg.get("close_ioc_retry_step_bps", 10.0), 10.0))
        self.close_gtc_fallback_enabled = self._to_bool(degrade_cfg.get("close_gtc_fallback_enabled", True), True)
        self.close_market_fallback_enabled = self._to_bool(degrade_cfg.get("close_market_fallback_enabled", False), False)

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
    def _is_success(result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("warning") == "order_failed_but_position_exists":
            return True
        if result.get("status") == "error":
            return False
        code = result.get("code")
        if isinstance(code, (int, float)) and code < 0:
            return False
        return True

    @staticmethod
    def _extract_message(result: Dict[str, Any]) -> str:
        return str(result.get("msg") or result.get("message") or result)

    def _ensure_open_quantity(
        self,
        *,
        symbol: str,
        raw_quantity: float,
        price: float,
        leverage: int,
        available_balance: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        兜底开仓数量，避免因精度/最小名义价值导致 quantity 被归零（尤其是 SELL）。
        """
        info: Dict[str, Any] = {
            "raw_quantity": raw_quantity,
            "formatted_quantity": 0.0,
            "used_min_qty_fallback": False,
        }
        qty = max(0.0, self._to_float(raw_quantity, 0.0))
        if qty > 0:
            try:
                qty = float(self.client.format_quantity(symbol, qty))
            except Exception:
                pass
        info["formatted_quantity"] = qty

        if qty <= 0 and price > 0:
            info["used_min_qty_fallback"] = True
            step_qty = 0.0
            min_qty = 0.0
            try:
                symbol_info = self.client.get_symbol_info(symbol) or {}
                step_size = self._to_float(symbol_info.get("step_size"), 0.0)
                if step_size > 0:
                    step_qty = float(self.client.format_quantity(symbol, step_size))
                min_qty = self._to_float(symbol_info.get("min_qty"), 0.0)
                if min_qty > 0:
                    min_qty = float(self.client.format_quantity(symbol, min_qty))
            except Exception:
                step_qty = 0.0
                min_qty = 0.0

            candidate = max(self._to_float(raw_quantity, 0.0), step_qty, min_qty)
            if candidate <= 0:
                candidate = max(step_qty, min_qty)

            if candidate > 0:
                try:
                    candidate = float(
                        self.client.ensure_min_notional_quantity(
                            symbol,
                            candidate,
                            price,
                        )
                    )
                except Exception:
                    pass
                if min_qty > 0 and candidate < min_qty:
                    candidate = min_qty
                try:
                    candidate = float(self.client.format_quantity(symbol, candidate))
                except Exception:
                    pass
            qty = max(0.0, candidate)
            info["fallback_quantity"] = qty

        if qty <= 0:
            info["reject_reason"] = "qty_zero_after_fallback"
            return 0.0, info

        safe_lev = max(1, int(leverage or 1))
        required_margin = (qty * price) / safe_lev if price > 0 else 0.0
        info["required_margin"] = required_margin
        info["available_balance"] = available_balance
        if required_margin > (available_balance + 1e-8):
            info["reject_reason"] = "insufficient_balance_for_min_qty"
            return 0.0, info

        return qty, info

    def _resolve_close_quantity(
        self,
        *,
        symbol: str,
        position_size: float,
        target_portion: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        计算平仓数量。
        当目标平仓量被交易所步长量化为 0 时，自动升级为可执行数量（优先全平当前微仓位），
        避免风控减仓持续 noop（"平仓数量为0"）而被动等待止损。
        """
        info: Dict[str, Any] = {
            "position_size": max(0.0, self._to_float(position_size, 0.0)),
            "target_portion": max(0.0, self._to_float(target_portion, 0.0)),
            "raw_close_quantity": 0.0,
            "formatted_close_quantity": 0.0,
            "promoted_to_full_close": False,
            "promotion_reason": "",
        }
        pos_size = info["position_size"]
        tgt = info["target_portion"]
        raw_close_qty = max(0.0, pos_size * tgt)
        info["raw_close_quantity"] = raw_close_qty
        close_qty = raw_close_qty
        try:
            close_qty = float(self.client.format_quantity(symbol, close_qty))
        except Exception:
            pass
        info["formatted_close_quantity"] = close_qty
        if close_qty > 0:
            return close_qty, info

        if raw_close_qty <= 0 or pos_size <= 0:
            info["reject_reason"] = "close_qty_zero_after_format"
            return 0.0, info

        # 微仓位兜底：若部分平仓量被格式化为 0，升级为全平当前可执行仓位。
        full_close_qty = pos_size
        try:
            full_close_qty = float(self.client.format_quantity(symbol, full_close_qty))
        except Exception:
            pass
        if full_close_qty > 0:
            info["promoted_to_full_close"] = True
            info["promotion_reason"] = "partial_qty_rounded_to_zero"
            info["formatted_close_quantity"] = full_close_qty
            return full_close_qty, info

        info["reject_reason"] = "full_close_qty_zero_after_format"
        return 0.0, info

    def _format_price(self, symbol: str, price: float) -> float:
        """Format price to exchange tick/precision to avoid -1111 precision errors."""
        if price <= 0:
            return price
        try:
            info = self.client.get_symbol_info(symbol)
        except Exception:
            info = None

        if isinstance(info, dict):
            try:
                tick = float(info.get("tick_size", 0) or 0)
            except Exception:
                tick = 0.0
            if tick > 0:
                try:
                    p_dec = Decimal(str(price))
                    t_dec = Decimal(str(tick))
                    q = (p_dec / t_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    out = float((q * t_dec).normalize())
                    if out > 0:
                        return out
                except Exception:
                    pass
            try:
                precision = int(info.get("price_precision", 4) or 4)
                if precision < 0:
                    precision = 4
                return round(float(price), precision)
            except Exception:
                pass
        return round(float(price), 4)

    def _is_no_liquidity(self, result: Dict[str, Any]) -> bool:
        msg = self._extract_message(result).lower()
        code = result.get("code")
        if code in (-2010, -5022):
            return True
        keys = (
            "immediately match and take",
            "insufficient liquidity",
            "no liquidity",
            "would immediately trigger",
        )
        return any(k in msg for k in keys)

    def _is_reduce_only_rejected(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        code = result.get("code")
        try:
            if code is not None and int(float(code)) == -2022:
                return True
        except Exception:
            pass
        msg = self._extract_message(result).lower()
        keys = (
            "reduceonly order is rejected",
            "reduceonly rejected",
            "reduce only order is rejected",
            "reduceonly",
            "-2022",
        )
        return any(k in msg for k in keys)

    def _sync_symbol_leverage(self, symbol: str, target_leverage: int) -> Dict[str, Any]:
        """
        将交易所该交易对杠杆同步到目标值，避免“策略显示杠杆”和“实盘实际杠杆”不一致。
        """
        requested = int(target_leverage)
        try:
            if not hasattr(self.client, "position_gateway"):
                return {"status": "skipped", "requested": requested, "message": "position_gateway unavailable"}
            raw = self.client.position_gateway.change_leverage(symbol, requested)
            applied = requested
            if isinstance(raw, dict):
                try:
                    code_field = raw.get("code")
                    if code_field is not None:
                        code_value = int(str(code_field))
                        if code_value < 0:
                            return {
                                "status": "error",
                                "requested": requested,
                                "message": str(raw.get("msg") or raw),
                                "raw": raw,
                            }
                except Exception:
                    pass
                try:
                    lev_field = raw.get("leverage")
                    if lev_field is not None:
                        applied = int(float(lev_field))
                except Exception:
                    applied = requested
            return {"status": "success", "requested": requested, "applied": applied, "raw": raw}
        except Exception as e:
            return {"status": "error", "requested": requested, "message": f"change_leverage exception: {e}"}

    @staticmethod
    def _position_from_snapshot(position: Optional[Dict[str, Any]]) -> Tuple[str, float]:
        if not position:
            return "", 0.0
        side = str(position.get("side", "")).upper()
        size = float(position.get("amount", 0.0) or position.get("positionAmt", 0.0) or 0.0)
        return side, abs(size)

    @staticmethod
    def _infer_position_side(raw_position: Optional[Dict[str, Any]]) -> str:
        if not isinstance(raw_position, dict):
            return ""
        side = str(raw_position.get("positionSide", "")).upper()
        if side in ("LONG", "SHORT"):
            return side
        amt = FundFlowExecutionRouter._to_float(raw_position.get("positionAmt"), 0.0)
        if amt > 0:
            return "LONG"
        if amt < 0:
            return "SHORT"
        return ""

    def _fetch_live_position_state(self, symbol: str, preferred_side: str = "") -> Dict[str, Any]:
        """
        从交易所实时拉取仓位，避免使用快照导致 reduce-only 方向/数量失配。
        """
        symbol_up = str(symbol or "").upper()
        side_pref = str(preferred_side or "").upper()
        try:
            if side_pref in ("LONG", "SHORT"):
                pos_side = self.client.get_position(symbol_up, side=side_pref)
                amt_side = abs(self._to_float((pos_side or {}).get("positionAmt"), 0.0)) if isinstance(pos_side, dict) else 0.0
                if amt_side > 0:
                    return {
                        "ok": True,
                        "symbol": symbol_up,
                        "side": side_pref,
                        "size": amt_side,
                        "source": "get_position(side)",
                        "raw": pos_side,
                    }

            pos_any = self.client.get_position(symbol_up)
            amt_any = abs(self._to_float((pos_any or {}).get("positionAmt"), 0.0)) if isinstance(pos_any, dict) else 0.0
            if amt_any > 0:
                side_any = self._infer_position_side(pos_any) or side_pref
                return {
                    "ok": True,
                    "symbol": symbol_up,
                    "side": side_any,
                    "size": amt_any,
                    "source": "get_position(any)",
                    "raw": pos_any,
                }

            all_positions = self.client.get_all_positions() if hasattr(self.client, "get_all_positions") else []
            candidates: List[Dict[str, Any]] = []
            for p in all_positions or []:
                if not isinstance(p, dict):
                    continue
                if str(p.get("symbol") or "").upper() != symbol_up:
                    continue
                amt = abs(self._to_float(p.get("positionAmt"), 0.0))
                if amt <= 0:
                    continue
                candidates.append(p)
            if candidates:
                primary = max(candidates, key=lambda x: abs(self._to_float(x.get("positionAmt"), 0.0)))
                amt_primary = abs(self._to_float(primary.get("positionAmt"), 0.0))
                side_primary = self._infer_position_side(primary) or side_pref
                return {
                    "ok": True,
                    "symbol": symbol_up,
                    "side": side_primary,
                    "size": amt_primary,
                    "source": "get_all_positions",
                    "raw": primary,
                }

            return {
                "ok": True,
                "symbol": symbol_up,
                "side": "",
                "size": 0.0,
                "source": "flat",
                "raw": {},
            }
        except Exception as e:
            return {
                "ok": False,
                "symbol": symbol_up,
                "side": "",
                "size": 0.0,
                "source": "error",
                "error": str(e),
            }

    def _place_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        price: float,
        tif: TimeInForce,
        reduce_only: bool,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": tif.value.upper(),
        }
        try:
            if bool(self.client.broker.get_hedge_mode()):
                params["positionSide"] = position_side
        except Exception:
            params["positionSide"] = position_side

        try:
            return self.client._execute_order_v2(
                params=params,
                side=side,
                reduce_only=reduce_only,
            )
        except Exception as e:
            msg = str(e)
            if "[OPEN BLOCKED]" in msg:
                return {
                    "status": "noop",
                    "code": 0,
                    "message": msg,
                    "open_blocked": True,
                }
            return {
                "status": "error",
                "code": -1,
                "message": f"place_limit_order exception: {msg}",
            }

    def _place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        reduce_only: bool,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "type": "MARKET",
            "quantity": quantity,
        }
        try:
            if bool(self.client.broker.get_hedge_mode()):
                params["positionSide"] = position_side
        except Exception:
            params["positionSide"] = position_side

        try:
            return self.client._execute_order_v2(
                params=params,
                side=side,
                reduce_only=reduce_only,
            )
        except Exception as e:
            msg = str(e)
            if "[OPEN BLOCKED]" in msg:
                return {
                    "status": "noop",
                    "code": 0,
                    "message": msg,
                    "open_blocked": True,
                }
            return {
                "status": "error",
                "code": -1,
                "message": f"place_market_order exception: {msg}",
            }

    @staticmethod
    def _with_degradation_path(result: Dict[str, Any], path: List[Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(result, dict):
            result["degradation_path"] = path
            return result
        return {"status": "error", "message": str(result), "degradation_path": path}

    def _try_place_with_fallback(
        self,
        *,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        price: float,
        tif: TimeInForce,
        reduce_only: bool,
    ) -> Dict[str, Any]:
        path: List[Dict[str, Any]] = []

        current = self._place_limit_order(
            symbol=symbol,
            side=side,
            position_side=position_side,
            quantity=quantity,
            price=price,
            tif=tif,
            reduce_only=reduce_only,
        )
        path.append(
            {
                "step": "limit_initial",
                "tif": tif.value,
                "price": price,
                "success": self._is_success(current),
            }
        )
        if self._is_success(current) or (isinstance(current, dict) and current.get("open_blocked")):
            return self._with_degradation_path(current, path)

        # 非 IOC 订单不做 IOC 退化链
        if tif != TimeInForce.IOC:
            return self._with_degradation_path(current, path)

        # IOC 多次重试（仅在流动性不足类错误时触发）
        for i in range(1, self.open_ioc_retry_times):
            if not self._is_no_liquidity(current):
                break
            step = (self.open_ioc_retry_step_bps / 10000.0) * i
            retry_price = price * (1.0 + step) if side == "BUY" else price * (1.0 - step)
            retry_price = self._format_price(symbol, retry_price)
            current = self._place_limit_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                quantity=quantity,
                price=retry_price,
                tif=TimeInForce.IOC,
                reduce_only=reduce_only,
            )
            path.append(
                {
                    "step": "limit_ioc_retry",
                    "retry_index": i,
                    "tif": TimeInForce.IOC.value,
                    "price": retry_price,
                    "success": self._is_success(current),
                }
            )
            if self._is_success(current) or (isinstance(current, dict) and current.get("open_blocked")):
                return self._with_degradation_path(current, path)

        # IOC -> GTC 退化
        if self.open_gtc_fallback_enabled and self._is_no_liquidity(current):
            gtc_result = self._place_limit_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                quantity=quantity,
                price=self._format_price(symbol, price),
                tif=TimeInForce.GTC,
                reduce_only=reduce_only,
            )
            path.append(
                {
                    "step": "limit_gtc_fallback",
                    "tif": TimeInForce.GTC.value,
                    "price": self._format_price(symbol, price),
                    "success": self._is_success(gtc_result),
                }
            )
            current = gtc_result
            if self._is_success(current) or (isinstance(current, dict) and current.get("open_blocked")):
                return self._with_degradation_path(current, path)

        # 最后兜底：市价开仓（默认关闭）
        if self.open_market_fallback_enabled and self._is_no_liquidity(current):
            market_result = self._place_market_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                quantity=quantity,
                reduce_only=reduce_only,
            )
            path.append(
                {
                    "step": "market_fallback",
                    "success": self._is_success(market_result),
                }
            )
            current = market_result

        return self._with_degradation_path(current, path)

    def _place_tp_sl(self, decision: FundFlowDecision, position_side: str) -> Dict[str, Any]:
        tp = decision.take_profit_price
        sl = decision.stop_loss_price
        if tp is None and sl is None:
            return {"status": "noop", "message": "no tp/sl"}
        side = IntentPositionSide.LONG if position_side == "LONG" else IntentPositionSide.SHORT
        qty: Optional[float] = None
        try:
            pos = self.client.get_position(decision.symbol, side=position_side)
            if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                qty = abs(float(pos.get("positionAmt", 0)))
                qty = float(self.client.format_quantity(decision.symbol, qty))
        except Exception:
            qty = None
        try:
            return self.client._execute_protection_v2(
                symbol=decision.symbol,
                side=side,
                tp=tp,
                sl=sl,
                quantity=qty,
            )
        except Exception as e:
            return {"status": "error", "code": -1, "message": f"place_tp_sl exception: {e}"}

    @staticmethod
    def _is_exchange_error_item(item: Any) -> bool:
        if not isinstance(item, dict):
            return True
        code = item.get("code")
        return isinstance(code, (int, float)) and code < 0

    def _check_protection_completeness(
        self,
        decision: FundFlowDecision,
        protection: Dict[str, Any],
    ) -> Dict[str, Any]:
        orders = protection.get("orders") if isinstance(protection, dict) else []
        order_list = orders if isinstance(orders, list) else []
        success_orders: List[Dict[str, Any]] = []
        for item in order_list:
            if not isinstance(item, dict):
                continue
            if self._is_exchange_error_item(item):
                continue
            if item.get("orderId") is None:
                continue
            success_orders.append(item)

        has_tp = False
        has_sl = False
        for item in success_orders:
            order_type = str(item.get("type") or item.get("strategyType") or "").upper()
            if "TAKE_PROFIT" in order_type:
                has_tp = True
            if "STOP" in order_type:
                has_sl = True

        need_tp = decision.take_profit_price is not None
        need_sl = decision.stop_loss_price is not None
        status = str(protection.get("status", "")).lower()
        status_ok = status in ("success", "noop")

        if not status_ok:
            return {
                "ok": False,
                "reason": f"protection_status={status or 'unknown'}",
                "has_tp": has_tp,
                "has_sl": has_sl,
            }
        if need_tp and not has_tp:
            return {"ok": False, "reason": "missing_take_profit_order", "has_tp": has_tp, "has_sl": has_sl}
        if need_sl and not has_sl:
            return {"ok": False, "reason": "missing_stop_loss_order", "has_tp": has_tp, "has_sl": has_sl}
        return {"ok": True, "reason": "ok", "has_tp": has_tp, "has_sl": has_sl}

    def _force_flatten_position(self, symbol: str, position_side: str) -> Dict[str, Any]:
        side_up = str(position_side or "").upper()
        if side_up not in ("LONG", "SHORT"):
            return {"status": "error", "message": f"invalid position side: {position_side}"}
        try:
            pos = self.client.get_position(symbol, side=side_up)
            if not pos or abs(float(pos.get("positionAmt", 0))) <= 0:
                return {"status": "noop", "message": "no position to flatten"}
            qty = abs(float(pos.get("positionAmt", 0)))
            qty = float(self.client.format_quantity(symbol, qty))
            close_side = "SELL" if side_up == "LONG" else "BUY"
            params: Dict[str, Any] = {
                "symbol": symbol,
                "type": "MARKET",
                "quantity": qty,
                "closePosition": True,
            }
            try:
                if bool(self.client.broker.get_hedge_mode()):
                    params["positionSide"] = side_up
            except Exception:
                pass
            result = self.client._execute_order_v2(
                params=params,
                side=close_side,
                reduce_only=True,
            )
            if not self._is_success(result):
                return {
                    "status": "error",
                    "message": f"force_flatten order rejected: {self._extract_message(result)}",
                    "order": result,
                }
            if self._is_filled(result):
                return {"status": "success", "order": result}

            # 回滚路径必须尽量确认仓位是否已实际消失，避免“假成功”。
            latest = self.client.get_position(symbol, side=side_up)
            latest_amt = abs(self._to_float((latest or {}).get("positionAmt"), 0.0)) if isinstance(latest, dict) else 0.0
            if latest_amt <= 0:
                return {
                    "status": "success",
                    "order": result,
                    "message": "position closed after flatten request",
                }
            return {
                "status": "error",
                "message": "force_flatten not confirmed; position still open",
                "order": result,
                "remaining_qty": latest_amt,
            }
        except Exception as e:
            return {"status": "error", "message": f"force_flatten exception: {e}"}

    def _is_filled(self, result: Dict[str, Any]) -> bool:
        if not self._is_success(result):
            return False
        status = str(result.get("status", "")).upper()
        if status in ("FILLED", "PARTIALLY_FILLED"):
            return True
        executed_qty = self._to_float(result.get("executedQty"), 0.0)
        return executed_qty > 0

    def _is_fully_filled(self, result: Dict[str, Any]) -> bool:
        if not self._is_success(result):
            return False
        status = str(result.get("status", "")).upper()
        if status == "FILLED":
            return True
        executed_qty = self._to_float(result.get("executedQty"), 0.0)
        orig_qty = self._to_float(result.get("origQty"), 0.0)
        if orig_qty > 0:
            return executed_qty >= max(orig_qty - 1e-12, 0.0)
        return False

    def execute_decision(
        self,
        decision: FundFlowDecision,
        account_state: Dict[str, Any],
        current_price: float,
        position: Optional[Dict[str, Any]] = None,
        trigger_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        trigger_context = trigger_context or {}
        try:
            decision = self.risk.validate_decision(decision)
        except Exception as e:
            result = {"status": "error", "message": f"decision 校验失败: {e}"}
            self.attribution.log_execution(decision, result)
            return result

        try:
            if decision.operation == Operation.HOLD:
                result = {"status": "noop", "message": "hold"}
                self.attribution.log_execution(decision, result)
                return result

            if decision.operation in (Operation.BUY, Operation.SELL):
                available_balance = self._to_float(account_state.get("available_balance"), 0.0)
                if available_balance <= 0:
                    result = {"status": "error", "message": "可用余额为0，禁止开仓"}
                    self.attribution.log_execution(decision, result)
                    return result

                leverage = self.risk.clamp_leverage(decision.leverage)
                leverage_sync = self._sync_symbol_leverage(decision.symbol, leverage)
                strict_sync = bool(
                    ((self.risk.config or {}).get("fund_flow", {}) or {}).get("strict_leverage_sync", True)
                )
                if leverage_sync.get("status") == "error" and strict_sync:
                    result = {
                        "status": "error",
                        "message": "杠杆同步失败，已阻止开仓",
                        "leverage_sync": leverage_sync,
                        "trigger_context": trigger_context,
                    }
                    self.attribution.log_execution(decision, result)
                    return result
                if leverage_sync.get("status") == "success":
                    try:
                        leverage = int(leverage_sync.get("applied", leverage))
                    except Exception:
                        pass

                margin = available_balance * decision.target_portion_of_balance
                position_value = margin * leverage
                qty_raw = round(position_value / current_price, 10) if current_price > 0 else 0.0
                qty, qty_info = self._ensure_open_quantity(
                    symbol=decision.symbol,
                    raw_quantity=qty_raw,
                    price=current_price,
                    leverage=leverage,
                    available_balance=available_balance,
                )
                if qty <= 0:
                    result = {
                        "status": "error",
                        "message": "开仓数量无效（最小下单量/余额约束）",
                        "quantity_info": qty_info,
                    }
                    self.attribution.log_execution(decision, result)
                    return result

                raw_price = self.risk.pick_entry_price(decision, current_price)
                order_price = self.risk.enforce_price_bounds(raw_price, current_price)
                order_price = self._format_price(decision.symbol, order_price)
                side = "BUY" if decision.operation == Operation.BUY else "SELL"
                position_side = "LONG" if decision.operation == Operation.BUY else "SHORT"

                order_result = self._try_place_with_fallback(
                    symbol=decision.symbol,
                    side=side,
                    position_side=position_side,
                    quantity=qty,
                    price=order_price,
                    tif=decision.time_in_force,
                    reduce_only=False,
                )
                if isinstance(order_result, dict) and order_result.get("open_blocked"):
                    result = {
                        "status": "noop",
                        "message": "已有持仓，跳过重复开仓",
                        "order": order_result,
                        "leverage_sync": leverage_sync,
                        "trigger_context": trigger_context,
                    }
                    self.attribution.log_execution(decision, result)
                    return result
                if not self._is_success(order_result):
                    order_error_msg = self._extract_message(order_result) if isinstance(order_result, dict) else str(order_result)
                    order_error_code = order_result.get("code") if isinstance(order_result, dict) else None
                    result = {
                        "status": "error",
                        "message": "开仓失败",
                        "error_detail": order_error_msg,
                        "error_code": order_error_code,
                        "order": order_result,
                        "leverage_sync": leverage_sync,
                        "quantity_info": qty_info,
                        "trigger_context": trigger_context,
                    }
                    self.attribution.log_execution(decision, result)
                    return result

                entry_filled = self._is_filled(order_result)
                if entry_filled:
                    protection = self._place_tp_sl(decision, position_side)
                    protection_guard = self._check_protection_completeness(decision, protection)
                    if not protection_guard.get("ok", False):
                        ff_cfg = (self.risk.config or {}).get("fund_flow", {}) or {}
                        rollback_on_fail = bool(ff_cfg.get("rollback_on_tp_sl_fail", True))
                        rollback_result = None
                        if rollback_on_fail:
                            rollback_result = self._force_flatten_position(decision.symbol, position_side)
                        result = {
                            "status": "error",
                            "message": "保护单下发失败，已阻止裸仓",
                            "order": order_result,
                            "protection": protection,
                            "protection_guard": protection_guard,
                            "rollback": rollback_result,
                            "quantity": qty,
                            "price": order_price,
                            "leverage": leverage,
                            "margin": margin,
                            "position_value": position_value,
                            "leverage_sync": leverage_sync,
                            "quantity_info": qty_info,
                            "trigger_context": trigger_context,
                        }
                        self.attribution.log_execution(decision, result)
                        return result
                else:
                    protection = {
                        "status": "pending",
                        "message": "entry not filled yet, tp/sl skipped for now",
                    }

                result = {
                    "status": "success" if entry_filled else "pending",
                    "message": "开仓成交，保护单已处理" if entry_filled else "开仓委托已提交，待成交（非失败）",
                    "operation": decision.operation.value,
                    "order": order_result,
                    "protection": protection,
                    "quantity": qty,
                    "price": order_price,
                    "leverage": leverage,
                    "margin": margin,
                    "position_value": position_value,
                    "quantity_info": qty_info,
                    "leverage_sync": leverage_sync,
                    "trigger_context": trigger_context,
                }
                self.attribution.log_execution(decision, result)
                return result

            if decision.operation == Operation.CLOSE:
                snapshot_side, snapshot_size = self._position_from_snapshot(position)
                live_position = self._fetch_live_position_state(decision.symbol, preferred_side=snapshot_side)
                position_side = snapshot_side
                position_size = snapshot_size
                if live_position.get("ok"):
                    live_side = str(live_position.get("side", "")).upper()
                    live_size = self._to_float(live_position.get("size"), 0.0)
                    if live_size <= 0:
                        result = {
                            "status": "noop",
                            "message": "无可平仓位（交易所实时仓位为0）",
                            "position_sync": {
                                "snapshot_side": snapshot_side,
                                "snapshot_size": snapshot_size,
                                "live_side": live_side,
                                "live_size": live_size,
                                "source": live_position.get("source"),
                            },
                        }
                        self.attribution.log_execution(decision, result)
                        return result
                    position_size = live_size
                    if live_side in ("LONG", "SHORT"):
                        position_side = live_side
                if position_size <= 0 or position_side not in ("LONG", "SHORT"):
                    result = {
                        "status": "noop",
                        "message": "无可平仓位",
                        "position_sync": {
                            "snapshot_side": snapshot_side,
                            "snapshot_size": snapshot_size,
                            "live_ok": bool(live_position.get("ok")),
                            "live_error": live_position.get("error"),
                        },
                    }
                    self.attribution.log_execution(decision, result)
                    return result

                close_qty, close_qty_info = self._resolve_close_quantity(
                    symbol=decision.symbol,
                    position_size=position_size,
                    target_portion=decision.target_portion_of_balance,
                )
                if close_qty <= 0:
                    result = {
                        "status": "noop",
                        "message": "平仓数量为0",
                        "quantity_info": close_qty_info,
                    }
                    self.attribution.log_execution(decision, result)
                    return result

                base_close_price = self.risk.pick_close_price(decision, current_price, position_side)
                base_close_price = self.risk.align_close_price(base_close_price, current_price, position_side)
                close_side = "SELL" if position_side == "LONG" else "BUY"
                is_full_close_target = bool(close_qty_info.get("promoted_to_full_close")) or (
                    decision.target_portion_of_balance >= 1.0
                )

                final_result: Dict[str, Any] = {}
                close_path: List[Dict[str, Any]] = []
                remaining_close_qty = close_qty
                filled_close_qty = 0.0
                for i in range(self.close_retry_times):
                    req_qty = remaining_close_qty
                    try:
                        req_qty = float(self.client.format_quantity(decision.symbol, req_qty))
                    except Exception:
                        pass
                    if req_qty <= 0:
                        break
                    step = (self.close_ioc_retry_step_bps / 10000.0) * i
                    if position_side == "LONG":
                        retry_price = base_close_price * (1.0 - step)
                    else:
                        retry_price = base_close_price * (1.0 + step)
                    retry_price = self.risk.align_close_price(retry_price, current_price, position_side)
                    retry_price = self._format_price(decision.symbol, retry_price)
                    retry_result = self._place_limit_order(
                        symbol=decision.symbol,
                        side=close_side,
                        position_side=position_side,
                        quantity=req_qty,
                        price=retry_price,
                        tif=TimeInForce.IOC,
                        reduce_only=True,
                    )
                    executed_qty = min(self._to_float(retry_result.get("executedQty"), 0.0), max(req_qty, 0.0))
                    if executed_qty > 0:
                        filled_close_qty += executed_qty
                        remaining_close_qty = max(0.0, close_qty - filled_close_qty)
                        try:
                            remaining_close_qty = float(self.client.format_quantity(decision.symbol, remaining_close_qty))
                        except Exception:
                            pass
                    close_path.append(
                        {
                            "step": "close_ioc_retry",
                            "retry_index": i,
                            "request_quantity": req_qty,
                            "executed_quantity": executed_qty,
                            "remaining_quantity": remaining_close_qty,
                            "price": retry_price,
                            "success": self._is_success(retry_result),
                            "filled": self._is_filled(retry_result),
                            "fully_filled": self._is_fully_filled(retry_result),
                            "reduce_only_rejected": self._is_reduce_only_rejected(retry_result),
                        }
                    )
                    if remaining_close_qty <= 0:
                        final_result = {
                            "status": "success",
                            "operation": "close",
                            "order": retry_result,
                            "quantity": close_qty,
                            "filled_quantity": filled_close_qty,
                            "remaining_quantity": 0.0,
                            "retry_index": i,
                            "price": retry_price,
                        }
                        break

                if not final_result:
                    if self.close_gtc_fallback_enabled:
                        boundary_price = current_price * (0.99 if position_side == "LONG" else 1.01)
                        boundary_price = self._format_price(decision.symbol, boundary_price)
                        fallback_qty = remaining_close_qty
                        try:
                            fallback_qty = float(self.client.format_quantity(decision.symbol, fallback_qty))
                        except Exception:
                            pass
                        if fallback_qty <= 0:
                            final_result = {
                                "status": "success" if remaining_close_qty <= 0 else "error",
                                "operation": "close",
                                "quantity": close_qty,
                                "filled_quantity": filled_close_qty,
                                "remaining_quantity": remaining_close_qty,
                                "fallback": "gtc_reduce_only_skipped_zero_qty",
                                "price": boundary_price,
                            }
                        else:
                            fallback = self._place_limit_order(
                                symbol=decision.symbol,
                                side=close_side,
                                position_side=position_side,
                                quantity=fallback_qty,
                                price=boundary_price,
                                tif=TimeInForce.GTC,
                                reduce_only=True,
                            )
                            fallback_executed = min(self._to_float(fallback.get("executedQty"), 0.0), max(fallback_qty, 0.0))
                            if fallback_executed > 0:
                                filled_close_qty += fallback_executed
                                remaining_close_qty = max(0.0, close_qty - filled_close_qty)
                                try:
                                    remaining_close_qty = float(self.client.format_quantity(decision.symbol, remaining_close_qty))
                                except Exception:
                                    pass
                            close_path.append(
                                {
                                    "step": "close_gtc_fallback",
                                    "request_quantity": fallback_qty,
                                    "executed_quantity": fallback_executed,
                                    "remaining_quantity": remaining_close_qty,
                                    "price": boundary_price,
                                    "success": self._is_success(fallback),
                                    "filled": self._is_filled(fallback),
                                    "reduce_only_rejected": self._is_reduce_only_rejected(fallback),
                                }
                            )
                            fallback_status = (
                                "success"
                                if remaining_close_qty <= 0
                                else ("pending" if self._is_success(fallback) or self._is_filled(fallback) else "error")
                            )
                            final_result = {
                                "status": fallback_status,
                                "operation": "close",
                                "order": fallback,
                                "quantity": close_qty,
                                "filled_quantity": filled_close_qty,
                                "remaining_quantity": remaining_close_qty,
                                "fallback": "gtc_reduce_only",
                                "price": boundary_price,
                            }
                    else:
                        final_result = {
                            "status": "error",
                            "operation": "close",
                            "message": "IOC平仓重试耗尽，且GTC回退已禁用",
                            "quantity": close_qty,
                            "filled_quantity": filled_close_qty,
                            "remaining_quantity": remaining_close_qty,
                        }

                if final_result.get("status") == "error" and self.close_market_fallback_enabled:
                    market_qty = remaining_close_qty
                    try:
                        market_qty = float(self.client.format_quantity(decision.symbol, market_qty))
                    except Exception:
                        pass
                    if market_qty <= 0:
                        final_result = {
                            "status": "success" if remaining_close_qty <= 0 else "error",
                            "operation": "close",
                            "quantity": close_qty,
                            "filled_quantity": filled_close_qty,
                            "remaining_quantity": remaining_close_qty,
                            "fallback": "market_reduce_only_skipped_zero_qty",
                        }
                    else:
                        market_fallback = self._place_market_order(
                            symbol=decision.symbol,
                            side=close_side,
                            position_side=position_side,
                            quantity=market_qty,
                            reduce_only=True,
                        )
                        market_executed = min(self._to_float(market_fallback.get("executedQty"), 0.0), max(market_qty, 0.0))
                        if market_executed > 0:
                            filled_close_qty += market_executed
                            remaining_close_qty = max(0.0, close_qty - filled_close_qty)
                            try:
                                remaining_close_qty = float(self.client.format_quantity(decision.symbol, remaining_close_qty))
                            except Exception:
                                pass
                        close_path.append(
                            {
                                "step": "close_market_fallback",
                                "request_quantity": market_qty,
                                "executed_quantity": market_executed,
                                "remaining_quantity": remaining_close_qty,
                                "success": self._is_success(market_fallback),
                                "filled": self._is_filled(market_fallback),
                                "reduce_only_rejected": self._is_reduce_only_rejected(market_fallback),
                            }
                        )
                        market_status = (
                            "success"
                            if remaining_close_qty <= 0
                            else ("pending" if self._is_success(market_fallback) or self._is_filled(market_fallback) else "error")
                        )
                        final_result = {
                            "status": market_status,
                            "operation": "close",
                            "order": market_fallback,
                            "quantity": close_qty,
                            "filled_quantity": filled_close_qty,
                            "remaining_quantity": remaining_close_qty,
                            "fallback": "market_reduce_only",
                        }

                reduce_only_rejected = self._is_reduce_only_rejected(final_result.get("order")) or any(
                    bool(step.get("reduce_only_rejected")) for step in close_path
                )
                if final_result.get("status") == "error" and reduce_only_rejected:
                    live_after_reject = self._fetch_live_position_state(decision.symbol, preferred_side=position_side)
                    live_after_size = self._to_float(live_after_reject.get("size"), 0.0)
                    if live_after_reject.get("ok") and live_after_size <= 0:
                        final_result = {
                            "status": "success",
                            "operation": "close",
                            "message": "ReduceOnly rejected，但交易所实时仓位已为0，按已平仓处理",
                            "quantity": close_qty,
                            "filled_quantity": filled_close_qty,
                            "remaining_quantity": 0.0,
                            "fallback": "reduce_only_reconciled_flat",
                            "position_sync": {
                                "snapshot_side": snapshot_side,
                                "snapshot_size": snapshot_size,
                                "live_side": live_after_reject.get("side"),
                                "live_size": live_after_size,
                                "source": live_after_reject.get("source"),
                            },
                        }
                    else:
                        final_result["position_sync"] = {
                            "snapshot_side": snapshot_side,
                            "snapshot_size": snapshot_size,
                            "live_side": live_after_reject.get("side"),
                            "live_size": live_after_size,
                            "live_ok": bool(live_after_reject.get("ok")),
                            "source": live_after_reject.get("source"),
                            "error": live_after_reject.get("error"),
                        }

                if final_result.get("status") == "error" and not final_result.get("message"):
                    order_detail = final_result.get("order")
                    detail_msg = self._extract_message(order_detail) if isinstance(order_detail, dict) else ""
                    final_result["message"] = detail_msg or "平仓失败"

                close_order = final_result.get("order", {})
                close_filled = filled_close_qty > 0 or self._is_filled(close_order)
                target_close_completed = remaining_close_qty <= 0
                final_result["target_close_completed"] = target_close_completed
                if is_full_close_target:
                    final_result["full_close_completed"] = target_close_completed
                if target_close_completed:
                    if is_full_close_target:
                        try:
                            self.client.cancel_all_open_orders(decision.symbol)
                            final_result["cancel_open_orders"] = "ok"
                        except Exception as e:
                            final_result["cancel_open_orders"] = f"error:{e}"
                elif close_filled:
                    if final_result.get("status") == "success":
                        final_result["status"] = "pending"
                    final_result["message"] = (
                        "全平仅部分成交，保留保护单，等待剩余仓位处理"
                        if is_full_close_target
                        else "部分平仓仅部分成交，目标剩余仓位待继续处理"
                    )
                    final_result["remaining_quantity"] = remaining_close_qty
                final_result["degradation_path"] = close_path
                final_result["quantity_info"] = close_qty_info

                self.attribution.log_execution(decision, final_result)
                return final_result

            result = {"status": "error", "message": f"不支持的 operation: {decision.operation.value}"}
            self.attribution.log_execution(decision, result)
            return result
        except Exception as e:
            result = {
                "status": "error",
                "message": f"execute_decision exception: {e}",
                "trigger_context": trigger_context,
            }
            self.attribution.log_execution(decision, result)
            return result

    @staticmethod
    def decision_to_json(decision: FundFlowDecision) -> str:
        return json.dumps(decision.to_dict(), ensure_ascii=False)
