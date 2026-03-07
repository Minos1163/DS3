from src.trading.intents import IntentAction, PositionSide, TradeIntent

# position_state_machine.py
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# =========================
# 状态枚举
# =========================


class PositionLifecycle(Enum):
    FLAT = "FLAT"
    OPEN = "OPEN"
    PROTECTED = "PROTECTED"
    CLOSING = "CLOSING"


# =========================
# Protection State
# =========================


@dataclass
class ProtectionState:
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    tp_order_id: Optional[int] = None
    sl_order_id: Optional[int] = None

    def is_active(self) -> bool:
        """只要有一个保护挂单存在，即视为受保护状态"""
        return self.tp_order_id is not None or self.sl_order_id is not None


# =========================
# Position Snapshot
# =========================


@dataclass
class PositionSnapshot:
    symbol: str
    side: PositionSide
    quantity: float
    lifecycle: PositionLifecycle
    protection: ProtectionState = field(default_factory=ProtectionState)
    last_update_ts: float = field(default_factory=time.time)

    def is_open(self) -> bool:
        return self.lifecycle in {
            PositionLifecycle.OPEN,
            PositionLifecycle.PROTECTED,
            PositionLifecycle.CLOSING,
        }


# =========================
# Invariant Checker
# =========================


class PositionInvariantViolation(RuntimeError):
    pass


class PositionInvariantChecker:
    @staticmethod
    def check(snapshot: Optional[PositionSnapshot], intent: TradeIntent) -> None:
        # -------- 基础 --------
        if intent.action == IntentAction.OPEN:
            # 🔥 移除本地快照检查，让交易所 API 判断是否真的有仓位
            # 避免第一次请求失败后，retry 时错误地阻止开仓
            # 只有在交易所返回明确错误时才阻止

            if intent.side is None or intent.quantity is None or intent.quantity <= 0:
                raise PositionInvariantViolation("❌ OPEN 必须指定 side + 正 quantity")

        if intent.action == IntentAction.CLOSE:
            if snapshot is None or not snapshot.is_open():
                # 为了鲁棒性，如果无已知快照但请求 CLOSE，我们也允许尝试 (client 会处理)
                pass

        if intent.action == IntentAction.REDUCE:
            if snapshot is None or not snapshot.is_open():
                raise PositionInvariantViolation("❌ 无仓位却请求 REDUCE")
            if intent.quantity is None or intent.quantity <= 0:
                raise PositionInvariantViolation("❌ REDUCE 必须指定正 quantity")
            if intent.quantity > snapshot.quantity:
                # 可选：检查减仓数量是否超过持仓，或者允许尝试扣减
                pass

        if intent.action in {
            IntentAction.SET_PROTECTION,
            IntentAction.UPDATE_PROTECTION,
        }:
            if snapshot is None or not snapshot.is_open():
                raise PositionInvariantViolation("❌ 无仓位却设置止盈止损")

            if intent.take_profit is None and intent.stop_loss is None:
                raise PositionInvariantViolation("❌ SET_PROTECTION 至少需要 TP 或 SL")

        # -------- 保护态合法性 --------
        if snapshot and snapshot.protection.is_active():
            if snapshot.lifecycle == PositionLifecycle.FLAT:
                raise PositionInvariantViolation("❌ FLAT 状态不允许存在 TP/SL")

        # -------- 数量不可为负 --------
        if snapshot and snapshot.quantity < 0:
            raise PositionInvariantViolation("❌ snapshot.quantity 非法")


# =========================
# PositionStateMachineV2
# =========================


class PositionStateMachineV2:
    """
    🔥 系统唯一交易大脑
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self.snapshots: Dict[str, PositionSnapshot] = {}

    # ---------- 核心入口 ----------

    def apply_intent(self, intent: TradeIntent) -> Dict[str, Any]:
        snapshot = self.snapshots.get(intent.symbol)

        # 1️⃣ Invariant Check
        try:
            PositionInvariantChecker.check(snapshot, intent)
        except PositionInvariantViolation as e:
            print(f"[State Violation] {e}")
            return {"status": "error", "message": str(e)}

        # 2️⃣ 执行
        if intent.action == IntentAction.OPEN:
            return self._open(intent)

        if intent.action == IntentAction.REDUCE:
            return self._reduce(intent)

        if intent.action in {
            IntentAction.SET_PROTECTION,
            IntentAction.UPDATE_PROTECTION,
        }:
            return self._set_protection(intent)

        if intent.action == IntentAction.CLOSE:
            return self._close(intent)

        raise RuntimeError(f"Unsupported intent: {intent.action}")

    # ---------- 行为实现 ----------

    def _open(self, intent: TradeIntent) -> Dict[str, Any]:
        assert intent.side is not None
        assert intent.quantity is not None

        allow_position_increase = False
        existing_snapshot = self.snapshots.get(intent.symbol)
        if existing_snapshot is not None and existing_snapshot.side == intent.side and float(existing_snapshot.quantity) > 0:
            allow_position_increase = True
        if not allow_position_increase:
            try:
                pos_check_side = intent.side.value if intent.side else None
                pos = self.client.get_position(intent.symbol, side=pos_check_side)
                if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                    allow_position_increase = True
            except Exception:
                pass

        # 打开前清理该 Symbol 所有挂单（防止旧的 TP/SL 意外触发）
        try:
            self.client.cancel_all_open_orders(intent.symbol)
        except Exception:
            pass

        # 使用意图中的 order_type，默认 MARKET
        order_type = intent.order_type if intent.order_type else "MARKET"

        params = {
            "symbol": intent.symbol,
            "type": order_type,
            "quantity": intent.quantity,
        }
        if allow_position_increase:
            # Internal marker consumed by OrderGateway; stripped before exchange request.
            params["_allow_position_increase"] = True

        # Hedge 模式下必须带 positionSide
        if self.client.broker.get_hedge_mode():
            params["positionSide"] = intent.side.value

        order_side = "BUY" if intent.side == PositionSide.LONG else "SELL"

        result = self.client._execute_order_v2(
            params=params,
            side=order_side,
            reduce_only=False,
        )

        # 🔥 关键修复：检查订单是否真的成功或已存在仓位
        # 如果API返回错误，立即返回错误，不创建/更新快照
        if result.get("status") == "error":
            return result

        # 如果下单返回特殊警告（下单失败但交易所已有仓位），视为成功——从交易所读取真实仓位并建立快照
        warn_flag = result.get("warning") == "order_failed_but_position_exists"
        pos_exists_flag = result.get("position_exists") is True
        if warn_flag or pos_exists_flag:
            # 根据 intent.side 确定查询方向
            pos_check_side = intent.side.value if intent.side else None
            pos = self.client.get_position(intent.symbol, side=pos_check_side)
            if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                amt = abs(float(pos.get("positionAmt", 0)))
                # 确定快照的 side（优先使用 positionSide 字段）
                ps = pos.get("positionSide", None)
                try:
                    snap_side = PositionSide(ps) if ps else intent.side
                except Exception:
                    snap_side = intent.side

                existing_snapshot = self.snapshots.get(intent.symbol)
                if existing_snapshot is not None and existing_snapshot.side == snap_side:
                    existing_snapshot.quantity = amt
                    existing_snapshot.last_update_ts = time.time()
                else:
                    snap = PositionSnapshot(
                        symbol=intent.symbol,
                        side=snap_side,
                        quantity=amt,
                        lifecycle=PositionLifecycle.OPEN,
                        last_update_ts=time.time(),
                    )
                    self.snapshots[intent.symbol] = snap

                # 如果开仓意图自带保护，则尝试设置保护
                if intent.take_profit or intent.stop_loss:
                    protection_res = self._set_protection(intent)
                    return {
                        "status": "success",
                        "open": result,
                        "protection": protection_res,
                        "position_exists": True,
                    }

                return {
                    "status": "success",
                    "open": result,
                    "position_exists": True,
                }

            # 无法从交易所确认仓位，视为可疑失败
            return {
                "status": "error",
                "message": "订单失败且无法确认交易所持仓",
                "detail": result,
            }

        # 检查是否有 orderId 或其他成功标识
        if "orderId" not in result and result.get("dry_run") is not True:
            return {"status": "error", "message": "订单响应缺少 orderId"}

        # 🔥 只有在订单真正成功时，才更新状态快照
        existing_snapshot = self.snapshots.get(intent.symbol)
        if existing_snapshot is not None and existing_snapshot.side == intent.side:
            # 已有同向仓位，增加数量
            existing_snapshot.quantity += float(intent.quantity)
            existing_snapshot.last_update_ts = time.time()
        else:
            # 新仓位
            snap = PositionSnapshot(
                symbol=intent.symbol,
                side=intent.side,
                quantity=float(intent.quantity),
                lifecycle=PositionLifecycle.OPEN,
                last_update_ts=time.time(),
            )
            self.snapshots[intent.symbol] = snap

        # 如果开仓意图自带保护，则立即执行
        if intent.take_profit or intent.stop_loss:
            protection_res = self._set_protection(intent)
            return {
                "status": "success",
                "open": result,
                "protection": protection_res,
            }

        return {"status": "success", "open": result}

    def _reduce(self, intent: TradeIntent) -> Dict[str, Any]:
        """
        部分平仓（向后兼容方法）
        现在统一使用 CLOSE action，但保留此方法以兼容旧代码
        """
        snap = self.snapshots.get(intent.symbol)
        assert snap is not None
        assert intent.quantity is not None

        # 减仓方向与持仓方向相反
        order_side = "SELL" if snap.side == PositionSide.LONG else "BUY"

        # 使用意图中的 order_type，默认 MARKET
        order_type = intent.order_type if intent.order_type else "MARKET"

        params = {
            "symbol": intent.symbol,
            "type": order_type,
            "quantity": intent.quantity,
        }

        # Hedge 模式下必须带 positionSide
        if self.client.broker.get_hedge_mode():
            params["positionSide"] = snap.side.value

        # 🔥 使用 intent.reduce_only（对于 REDUCE，通常是 True）
        reduce_only = intent.reduce_only if intent.reduce_only is not None else True

        result = self.client._execute_order_v2(
            params=params,
            side=order_side,
            reduce_only=reduce_only,
        )

        if result.get("status") != "error":
            # 扣减快照数量
            snap.quantity = max(0.0, snap.quantity - float(intent.quantity))
            if snap.quantity == 0:
                snap.lifecycle = PositionLifecycle.FLAT
                if intent.symbol in self.snapshots:
                    del self.snapshots[intent.symbol]

        return {"status": "reduced", "order": result}

    def _set_protection(self, intent: TradeIntent) -> Dict[str, Any]:
        snap = self.snapshots.get(intent.symbol)
        # 兼容性：如果快照丢失但有持仓，尝试重建快照
        if snap is None:
            # 传递 intent.side 获取特定边位的持仓
            query_side = intent.side.value if intent.side else None
            pos = self.client.get_position(intent.symbol, side=query_side)
            if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                amt = float(pos.get("positionAmt", 0))
                # 注意：如果指定了 side，那么 amt 对应的就是那个 side 的
                snap_side = PositionSide(pos.get("positionSide", "BOTH"))
                snap = PositionSnapshot(
                    symbol=intent.symbol,
                    side=snap_side,
                    quantity=abs(amt),
                    lifecycle=PositionLifecycle.OPEN,
                )
                self.snapshots[intent.symbol] = snap
            else:
                return {
                    "status": "error",
                    "message": "No position for protection",
                }

        # 执行真正的保护单下达
        result = self.client._execute_protection_v2(
            symbol=intent.symbol,
            side=snap.side,
            tp=intent.take_profit,
            sl=intent.stop_loss,
        )

        # 解析 Order ID 并存入快照
        tp_id = None
        sl_id = None
        if result.get("status") == "success":
            for order in result.get("orders", []):
                # 如果下单失败（比如价格太近），order 会包含 code
                if "orderId" in order:
                    otype = order.get("type") or order.get("strategyType", "")
                    if "TAKE_PROFIT" in otype:
                        tp_id = order["orderId"]
                    elif "STOP" in otype:
                        sl_id = order["orderId"]

        snap.protection = ProtectionState(
            take_profit=intent.take_profit,
            stop_loss=intent.stop_loss,
            tp_order_id=tp_id,
            sl_order_id=sl_id,
        )
        snap.lifecycle = PositionLifecycle.PROTECTED if snap.protection.is_active() else PositionLifecycle.OPEN
        snap.last_update_ts = time.time()

        return {"status": "protected", "orders": result, "snapshot": snap}

    def _close(self, intent: TradeIntent) -> Dict[str, Any]:
        """
        立即市价平仓入口。
        🔥 重要修正：PAPI 全仓平仓使用 closePosition=True 且必须带 quantity
        部分平仓使用 quantity + reduceOnly=True
        """
        # 无论是否有快照，先清理条件单 + 挂单（避免遗留未触发止盈止损）
        try:
            self.client.cancel_all_conditional_orders(intent.symbol)
        except Exception:
            pass
        try:
            # PAPI 条件单与普通挂单分离；非 PAPI 已在 conditional 中统一撤销
            if "papi" in self.client.broker.um_base():
                self.client.cancel_all_open_orders(intent.symbol)
        except Exception:
            pass

        # 获取当前真实持仓（用于获取精确数量）
        query_side = intent.side.value if intent.side else None
        pos = self.client.get_position(intent.symbol, side=query_side)
        if not pos:
            return {"status": "success", "message": f"No {query_side or ''} position to close"}

        amt = float(pos.get("positionAmt", 0))
        p_side = pos.get("positionSide", "BOTH")

        if abs(amt) == 0:
            return {"status": "success", "message": "Position is zero"}

        # 确定平仓方向
        if p_side == "LONG":
            order_side = "SELL"
        elif p_side == "SHORT":
            order_side = "BUY"
        else:
            order_side = "SELL" if amt > 0 else "BUY"

        # 🔥 核心逻辑：全仓平仓必须带 quantity
        is_full_close = intent.quantity is None or intent.quantity == 0 or abs(intent.quantity - abs(amt)) < 1e-8
        if is_full_close:
            # 全仓平仓：带 quantity 和 closePosition=True
            order_type = intent.order_type if intent.order_type else "MARKET"
            quantity = abs(amt)
            params = {
                "symbol": intent.symbol,
                "type": order_type,
                "closePosition": True,
                "quantity": quantity,
            }
            reduce_only = False
        else:
            # 部分平仓：使用 quantity + reduceOnly=True
            order_type = intent.order_type if intent.order_type else "MARKET"
            qty = intent.quantity if intent.quantity is not None else 0.0
            quantity = abs(qty)
            params = {
                "symbol": intent.symbol,
                "type": order_type,
                "quantity": quantity,
            }
            reduce_only = True  # 🔥 部分平仓必需

        # Hedge 模式显式处理 positionSide
        if p_side in ["LONG", "SHORT"]:
            params["positionSide"] = p_side

        # 执行下单
        result = self.client._execute_order_v2(
            params=params,
            side=order_side,
            reduce_only=reduce_only,
        )

        # 清除/更新快照
        if result.get("status") != "error":
            if is_full_close:
                # 全仓平仓：移除快照
                if intent.symbol in self.snapshots:
                    del self.snapshots[intent.symbol]
            else:
                # 部分平仓：更新数量
                if intent.symbol in self.snapshots:
                    snap = self.snapshots[intent.symbol]
                    qty = intent.quantity if intent.quantity is not None else 0.0
                    snap.quantity = max(0.0, snap.quantity - float(qty))
                    if snap.quantity == 0:
                        del self.snapshots[intent.symbol]

        return {"status": "closed", "order": result}

    def sync_with_exchange(self, positions: List[Dict], open_orders: List[Dict]):
        """
        同步本地状态机与交易所真实状态。
        """
        # 记录交易所当前的 (Symbol, Side) 活跃对
        active_pairs = set()
        for p in positions:
            if abs(float(p.get("positionAmt", 0))) > 0:
                sym = p["symbol"].upper()
                ps = p.get("positionSide", "BOTH").upper()
                active_pairs.add((sym, ps))

        open_order_ids = {o["orderId"] for o in open_orders}

        # 1. 清理本地已消失的仓位
        for symbol in list(self.snapshots.keys()):
            snap = self.snapshots[symbol]
            snap_side = snap.side.value.upper() if snap.side else "BOTH"
            if (symbol.upper(), snap_side) not in active_pairs:
                del self.snapshots[symbol]

        # 2. 检查受保护仓位的挂单状态
        for symbol, snap in self.snapshots.items():
            if snap.lifecycle == PositionLifecycle.PROTECTED:
                tp_id = snap.protection.tp_order_id
                if tp_id and tp_id not in open_order_ids:
                    snap.protection.tp_order_id = None
                    snap.protection.take_profit = None

                sl_id = snap.protection.sl_order_id
                if sl_id and sl_id not in open_order_ids:
                    snap.protection.sl_order_id = None
                    snap.protection.stop_loss = None

                if not snap.protection.is_active():
                    snap.lifecycle = PositionLifecycle.OPEN

            # 3. 仓位数量校准
            for p in positions:
                p_side = p.get("positionSide", "BOTH").upper()
                snap_side = snap.side.value.upper() if snap.side else "BOTH"
                if p["symbol"].upper() == symbol and p_side == snap_side:
                    snap.quantity = abs(float(p.get("positionAmt", 0)))
                    break

    # =========================
    # Event Handlers (Step 7)
    # =========================

    def on_order_filled(
        self,
        symbol: str,
        position_side: Optional[str],
        order_id: Optional[int],
        filled_qty: Optional[float],
    ):
        """订单成交事件入口 (由 EventRouter 驱动)"""
        snap = self.snapshots.get(symbol)
        if not snap:
            # 发现新成交但无快照，建立基础快照
            side = PositionSide.LONG if position_side == "LONG" else PositionSide.SHORT
            qty = float(filled_qty) if filled_qty is not None else 0.0
            self.snapshots[symbol] = PositionSnapshot(
                symbol=symbol,
                side=side,
                quantity=qty,
                lifecycle=PositionLifecycle.OPEN,
            )
            return

        # --- 核心逻辑: 指令/保护匹配 ---
        # 1. 如果成交 ID 匹配当前的保护单 ID -> 意味着保护触发，仓位归零
        protection_ids = (
            snap.protection.tp_order_id,
            snap.protection.sl_order_id,
        )
        if order_id is not None and order_id in protection_ids:
            print("[SM] Protection triggered for:", symbol)
            print("Order:", order_id)
            if symbol in self.snapshots:
                del self.snapshots[symbol]
            return

        # 2. 如果是正在进行的 CLOSING 动作成交
        if snap.lifecycle == PositionLifecycle.CLOSING:
            # 简化处理：CLOSING 动作只要有成交，且数量匹配或交易所更新显示 0，则平仓
            pass

        # 3. 基础数量更新 (兜底逻辑)
        # 注意：这里我们更倾向于依赖 on_position_update 的绝对值同步
        # 因为在 Web 套接字中，账户更新推送通常比订单成交推送更接近事实

    def on_order_canceled(self, symbol: str, order_id: Optional[int]):
        """挂单取消事件入口"""
        snap = self.snapshots.get(symbol)
        if not snap:
            return

        # 如果取消的是保护单，状态回退到 OPEN
        if order_id is None:
            return

        if snap.protection.tp_order_id == order_id:
            snap.protection.tp_order_id = None
            snap.protection.take_profit = None

        if snap.protection.sl_order_id == order_id:
            snap.protection.sl_order_id = None
            snap.protection.stop_loss = None

        if not snap.protection.is_active() and snap.lifecycle == PositionLifecycle.PROTECTED:
            snap.lifecycle = PositionLifecycle.OPEN

    def on_position_update(
        self,
        symbol: str,
        position_amt: Optional[float],
        position_side: Optional[str],
    ):
        """
        仓位级别终极同步 (这是解决所有状态漂移的保底逻辑)
        position_amt 为 signed (正为多，负为空，0为平)
        """
        if position_amt is None:
            return

        abs_amt = abs(position_amt)
        snap = self.snapshots.get(symbol)

        # 仓位归零
        if abs_amt == 0:
            if snap:
                print("[SM] Position Zeroed by Exchange Update for:", symbol)
                del self.snapshots[symbol]
            return

        # 仓位存在
        side = PositionSide.LONG if position_amt > 0 else PositionSide.SHORT
        if not snap:
            # 发现“幽灵仓位”（本地不知情），立即创建快照补全
            self.snapshots[symbol] = PositionSnapshot(
                symbol=symbol,
                side=side,
                quantity=abs_amt,
                lifecycle=PositionLifecycle.OPEN,
            )
        else:
            # 校准本地快照
            snap.quantity = abs_amt
            snap.side = side
            # 如果正在平仓中但交易所显示还有持仓且不再变动，重置回 OPEN 状态
            if snap.lifecycle == PositionLifecycle.CLOSING:
                snap.lifecycle = PositionLifecycle.OPEN
