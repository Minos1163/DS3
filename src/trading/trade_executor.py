from src.api.binance_client import BinanceClient

from src.trading.intent_builder import IntentBuilder

from src.trading.intent_guard import IntentGuard

from src.trading.intents import IntentAction

from src.trading.intents import PositionSide as IntentPositionSide

from src.trading.intents import TradeIntent

from src.utils.decorators import log_execution, retry_on_failure

import dataclasses
import os
from datetime import datetime
from typing import Any, Dict, Optional


class TradeExecutor:
    """
    PAPI ONLY · 实盘安全执行器
    - 强制 Hedge Mode / positionSide
    - OPEN / CLOSE 逻辑完全隔离
    - 防 retry 双开仓
    - TP / SL 仅允许 OPEN
    """

    def __init__(self, client: BinanceClient, config: Dict[str, Any]):
        # 保持接口兼容性，接受 config 参数（但不使用）
        self.client = client
        self.state = client.state_machine
        # 保存配置以支持可配置的回滚策略
        self.config = config or {}

    # =========================
    # 核心执行入口（私有）
    # =========================

    def _has_position(self, symbol: str, side: IntentPositionSide) -> bool:
        """检查状态机中是否存在指定 symbol 和 side 的仓位快照"""
        snapshot = self.state.snapshots.get(symbol)
        return snapshot is not None and snapshot.side == side

    def _execute_open(self, intent: TradeIntent) -> Dict[str, Any]:
        assert intent.action == IntentAction.OPEN
        assert intent.side is not None  # OPEN 意图必须有 side
        # Enforce protected opens: every OPEN must carry both TP and SL.
        if intent.take_profit is None or intent.stop_loss is None:
            return {
                "status": "error",
                "message": "OPEN 必须同时设置止盈和止损，已拒绝开仓",
            }

        # 🔥 移除本地快照检查，让交易所 API 判断是否真的有仓位
        # 避免第一次请求失败后，retry 时错误地阻止开仓
        # 只有在交易所返回明确错误时才阻止

        # ===== 价格校验 =====
        ticker = self.client.get_ticker(intent.symbol)
        price = float(ticker.get("lastPrice", 0)) if ticker else 0.0
        IntentGuard.validate(intent, price)

        # ===== 主订单 =====
        try:
            res = self.client.execute_intent(intent)
        except Exception as e:
            # 如果执行期间抛出与重复开仓相关的 RuntimeError（例如 L2/L1 检查）
            msg = str(e)
            if (
                "[OPEN BLOCKED]" in msg
                or "-1116" in msg
                or "Invalid orderType" in msg
                or "order_failed_but_position_exists" in msg
            ):
                # 优化逻辑：先直接询问交易所持仓，优先以交易所确认为准，避免本地预创建快照导致的误判
                try:
                    pos = self.client.get_position(
                        intent.symbol,
                        side=(intent.side.value if intent.side else None),
                    )
                    if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                        # 重建快照到状态机（以交易所数据为准）
                        amt = abs(float(pos.get("positionAmt", 0)))
                        ps = pos.get("positionSide", None)
                        if ps == "LONG":
                            snap_side = IntentPositionSide.LONG
                        elif ps == "SHORT":
                            snap_side = IntentPositionSide.SHORT
                        else:
                            snap_side = intent.side

                        from src.trading.position_state_machine import (
                            PositionLifecycle,
                            PositionSnapshot,
                        )

                        snap = PositionSnapshot(
                            symbol=intent.symbol,
                            side=snap_side,
                            quantity=amt,
                            lifecycle=PositionLifecycle.OPEN,
                        )
                        self.state.snapshots[intent.symbol] = snap
                        return {
                            "status": "success",
                            "open": {
                                "warning": "exception_but_position_exists",
                                "detail": msg,
                            },
                            "position_exists": True,
                        }
                except Exception:
                    # 查询交易所失败则回落到同步本地状态机并检查
                    pass

                # 主动同步状态机与交易所，确认是否实际已经有仓位（回退方案）
                try:
                    self.client.sync_state()
                except Exception:
                    pass

                # 如果状态机显示已有仓位，则视为成功（仅在无法直接从交易所确认时作为补偿性手段）
                snap = self.state.snapshots.get(intent.symbol)
                if snap and snap.is_open():
                    return {
                        "status": "success",
                        "open": {
                            "warning": "exception_but_position_exists",
                            "detail": msg,
                        },
                        "position_exists": True,
                    }

            # 其他异常继续抛出以触发重试逻辑
            raise

        # ===== TP / SL（只允许 OPEN）=====
        if intent.take_profit or intent.stop_loss:
            # 先尝试使用状态机返回的保护结果（避免重复下单）
            protection = None
            if isinstance(res, dict):
                protection = res.get("protection")

            # 若状态机未返回保护结果，则补发一次保护单
            if protection is None:
                protection = self.client._execute_protection_v2(
                    symbol=intent.symbol,
                    side=intent.side,
                    tp=intent.take_profit,
                    sl=intent.stop_loss,
                )

            if not self._protection_ok(protection):
                # 失败时，自动尝试一次更宽的保护价
                retry_tp, retry_sl = self._widen_protection_prices(
                    intent.side.value if intent.side else "LONG",
                    intent.take_profit,
                    intent.stop_loss,
                )
                protection_retry = self.client._execute_protection_v2(
                    symbol=intent.symbol,
                    side=intent.side,
                    tp=retry_tp,
                    sl=retry_sl,
                )
                if self._protection_ok(protection_retry):
                    return res

                # 记录保护单失败
                self._log_protection_failure(
                    intent.symbol,
                    intent.side.value if intent.side else "",
                    intent.take_profit,
                    intent.stop_loss,
                    protection if isinstance(protection, dict) else {},
                    protection_retry,
                )

                # Hard safety policy: protection failed => rollback open immediately.
                try:
                    self._close(intent.symbol, intent.side, None)
                except Exception:
                    pass
                try:
                    self.client.cancel_all_conditional_orders(intent.symbol)
                except Exception:
                    pass
                try:
                    self.client.cancel_all_open_orders(intent.symbol)
                except Exception:
                    pass
                return {
                    "status": "error",
                    "message": "TP/SL 下单失败，已强制回滚开仓并撤销未触发委托",
                    "open": res,
                    "protection": protection,
                    "protection_retry": protection_retry,
                }

        return res

    def _protection_ok(self, protection: Dict[str, Any]) -> bool:
        """判定保护单是否成功挂出（允许部分成功按失败处理）"""
        if not isinstance(protection, dict):
            return False
        status = str(protection.get("status", "")).lower()
        if status not in ("success", "protected"):
            return False
        orders = protection.get("orders")
        # state machine format: {"status":"protected","orders":{"status":"success","orders":[...]}}
        if isinstance(orders, dict):
            nested_status = str(orders.get("status", "")).lower()
            if nested_status and nested_status != "success":
                return False
            orders = orders.get("orders")
        if not isinstance(orders, list) or not orders:
            return False
        for order in orders:
            if not isinstance(order, dict):
                return False
            if "code" in order:
                return False
            # PAPI 条件单返回 strategyStatus
            if order.get("status") == "REJECTED":
                return False
            if order.get("strategyStatus") == "REJECTED":
                return False
        return True

    def _widen_protection_prices(
        self,
        side: str,
        take_profit: Optional[float],
        stop_loss: Optional[float],
    ) -> tuple[Optional[float], Optional[float]]:
        """将 TP/SL 向外放宽一点，降低立即触发或精度拒绝概率"""
        widen_factor = 0.003  # 0.3%
        side = str(side).upper()
        tp = take_profit
        sl = stop_loss
        if tp is not None:
            if side == "LONG":
                tp = tp * (1 + widen_factor)
            else:
                tp = tp * (1 - widen_factor)
        if sl is not None:
            if side == "LONG":
                sl = sl * (1 - widen_factor)
            else:
                sl = sl * (1 + widen_factor)
        return tp, sl

    def _log_protection_failure(
        self,
        symbol: str,
        side: str,
        take_profit: Optional[float],
        stop_loss: Optional[float],
        first_result: Dict[str, Any],
        retry_result: Dict[str, Any],
    ) -> None:
        """记录强制保护失败原因到日志文件"""
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            logs_dir = os.path.join(project_root, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            path = os.path.join(logs_dir, "tp_sl_failures.log")
            ts = datetime.now().isoformat()
            line = (
                f"{ts} symbol={symbol} side={side} "
                f"tp={take_profit} sl={stop_loss} "
                f"first={first_result} retry={retry_result}\n"
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _execute_close(self, intent: TradeIntent) -> Dict[str, Any]:
        assert intent.action == IntentAction.CLOSE
        assert intent.side is not None  # CLOSE 意图必须有 side
        side = intent.side  # 类型: IntentPositionSide (非 None)

        # Always purge stale protection orders before closing.
        try:
            self.client.cancel_all_conditional_orders(intent.symbol)
        except Exception:
            pass

        # ===== 仓位存在性校验 =====
        pos = self.client.get_position(intent.symbol, side.value)
        if not pos or float(pos.get("positionAmt", 0)) == 0:
            return {
                "status": "noop",
                "symbol": intent.symbol,
                "message": f"{side} 无仓位",
            }

        # ===== 禁止 TP / SL =====
        if intent.take_profit or intent.stop_loss:
            raise RuntimeError("CLOSE 不允许携带 TP / SL")

        # ===== 区分全仓/部分平仓 =====
        # 如果 intent.quantity 为 None 或为 0，则全仓平仓，使用 closePosition=True
        # 否则部分平仓，使用 reduceOnly=True
        if intent.quantity is None or intent.quantity == 0:
            # 全仓平仓：不设置 reduceOnly，让状态机使用 closePosition
            intent = dataclasses.replace(intent, quantity=abs(float(pos["positionAmt"])))
        else:
            # 部分平仓：使用 reduceOnly=True
            intent = dataclasses.replace(intent, reduce_only=True)

        res = self.client.execute_intent(intent)
        # Best-effort post-close cleanup to avoid lingering untriggered orders.
        try:
            self.client.cancel_all_conditional_orders(intent.symbol)
        except Exception:
            pass
        try:
            self.client.cancel_all_open_orders(intent.symbol)
        except Exception:
            pass
        return res

    # =========================
    # 开仓接口
    # =========================
    @log_execution
    @retry_on_failure(max_retries=3, delay=20)
    def open_long(
        self,
        symbol: str,
        quantity: float,
        leverage: Optional[int] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Dict[str, Any]:

        if leverage:
            self.client.position_gateway.change_leverage(symbol, leverage)

        qty = self.client.format_quantity(symbol, quantity)

        intent = IntentBuilder.build_open_long(
            symbol=symbol,
            quantity=qty,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

        return self._execute_open(intent)

    @log_execution
    @retry_on_failure(max_retries=3, delay=20)
    def open_short(
        self,
        symbol: str,
        quantity: float,
        leverage: Optional[int] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Dict[str, Any]:

        if leverage:
            self.client.position_gateway.change_leverage(symbol, leverage)

        qty = self.client.format_quantity(symbol, quantity)

        intent = IntentBuilder.build_open_short(
            symbol=symbol,
            quantity=qty,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

        return self._execute_open(intent)

    # =========================
    # 平仓接口（明确 side）
    # =========================
    @log_execution
    @retry_on_failure(max_retries=3, delay=20)
    def close_long(self, symbol: str, quantity: Optional[float] = None) -> Dict[str, Any]:
        return self._close(symbol, IntentPositionSide.LONG, quantity)

    @log_execution
    @retry_on_failure(max_retries=3, delay=20)
    def close_short(self, symbol: str, quantity: Optional[float] = None) -> Dict[str, Any]:
        return self._close(symbol, IntentPositionSide.SHORT, quantity)

    def _close(
        self,
        symbol: str,
        side: IntentPositionSide,
        quantity: Optional[float],
    ) -> Dict[str, Any]:

        pos = self.client.get_position(symbol, side.value)
        if not pos or float(pos.get("positionAmt", 0)) == 0:
            return {"status": "noop", "message": f"{symbol} {side} 无仓位"}

        amt = abs(float(pos["positionAmt"]))
        qty = amt if quantity is None else min(amt, quantity)
        qty = self.client.format_quantity(symbol, qty)

        intent = IntentBuilder.build_close(
            symbol=symbol,
            side=side,
            quantity=qty,
        )

        return self._execute_close(intent)

    # =========================
    # 兼容性方法
    # =========================
    @log_execution
    @retry_on_failure(max_retries=3, delay=20)
    def close_position(
        self,
        symbol: str,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Dict[str, Any]:
        """兼容性方法：自动检测 side 并平仓，忽略 TP/SL 参数"""
        if take_profit is not None or stop_loss is not None:
            print("⚠️  CLOSE 动作不支持 TP/SL 参数，已忽略")

        # 获取仓位信息，使用 positionSide 而不是 positionAmt 正负
        pos = self.client.get_position(symbol)
        if not pos or float(pos.get("positionAmt", 0)) == 0:
            return {"status": "noop", "message": f"{symbol} 无持仓"}

        # 使用 positionSide 字段，确保 Hedge Mode 下正确
        side_str = pos.get("positionSide", "BOTH")
        if side_str == "LONG":
            side = IntentPositionSide.LONG
        elif side_str == "SHORT":
            side = IntentPositionSide.SHORT
        else:
            # 对于 ONEWAY 模式，根据 positionAmt 正负判断
            qty = float(pos.get("positionAmt", 0))
            side = IntentPositionSide.LONG if qty > 0 else IntentPositionSide.SHORT

        return self._close(symbol, side, None)

    @log_execution
    def close_all_positions(
        self,
        symbol: Optional[str] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Dict[str, Any]:
        """兼容性方法：平掉所有仓位，忽略 TP/SL 参数"""
        if take_profit is not None or stop_loss is not None:
            print("⚠️  CLOSE 动作不支持 TP/SL 参数，已忽略")

        results = []

        for pos in self.client.get_all_positions():
            if float(pos.get("positionAmt", 0)) == 0:
                continue

            s = pos["symbol"]
            if symbol and s != symbol:
                continue

            side_str = pos.get("positionSide", "BOTH")
            if side_str == "LONG":
                side = IntentPositionSide.LONG
            elif side_str == "SHORT":
                side = IntentPositionSide.SHORT
            else:
                qty = float(pos.get("positionAmt", 0))
                side = IntentPositionSide.LONG if qty > 0 else IntentPositionSide.SHORT

            try:
                res = self._close(s, side, None)
                results.append({"symbol": s, "side": side, "result": res})
            except Exception as e:
                results.append({"symbol": s, "side": side, "error": str(e)})

        return {"status": "success", "results": results}

    @log_execution
    @retry_on_failure(max_retries=3, delay=20)
    def reduce_position(
        self,
        symbol: str,
        quantity: float,
        side: IntentPositionSide,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Dict[str, Any]:
        """部分平仓（兼容性方法，禁止 TP/SL）"""
        if take_profit is not None or stop_loss is not None:
            raise RuntimeError("REDUCE 动作不允许携带 TP / SL")

        qty = self.client.format_quantity(symbol, quantity)
        return self._close(symbol, side, qty)
