"""
持仓数据管理器
负责获取和管理持仓信息
"""

from typing import Any, Dict, List, Optional


class PositionDataManager:
    """持仓数据管理器"""

    def __init__(self, client):
        """
        初始化持仓数据管理器

        Args:
            client: Binance API客户端
        """
        self.client = client

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _resolve_side(self, raw_side: Any, amount: float) -> str:
        side = str(raw_side or "").upper()
        if side in ("LONG", "SHORT"):
            return side
        if amount > 0:
            return "LONG"
        if amount < 0:
            return "SHORT"
        return ""

    def _build_position_payload(self, position: Dict[str, Any], *, hedge_conflict: bool = False, legs: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        amount_signed = self._to_float(position.get("positionAmt"), 0.0)
        if abs(amount_signed) <= 0:
            return None

        side = self._resolve_side(position.get("positionSide"), amount_signed)
        if side not in ("LONG", "SHORT"):
            return None

        amount = abs(amount_signed)
        entry_price = self._to_float(position.get("entryPrice"), 0.0)
        mark_price = self._to_float(position.get("markPrice"), 0.0)
        leverage = int(self._to_float(position.get("leverage"), 0.0))
        unrealized_pnl = self._to_float(
            position.get("unRealizedProfit", position.get("unrealizedProfit", 0.0)),
            0.0,
        )

        if entry_price > 0:
            if side == "LONG":
                pnl_percent = ((mark_price - entry_price) / entry_price) * 100
            else:
                pnl_percent = ((entry_price - mark_price) / entry_price) * 100
        else:
            pnl_percent = 0.0

        margin = abs(amount * entry_price / leverage) if leverage > 0 else 0.0

        out = {
            "side": side,
            "amount": amount,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "leverage": leverage,
            "margin": margin,
            "unrealized_pnl": unrealized_pnl,
            "pnl_percent": pnl_percent,
            "liquidation_price": self._to_float(position.get("liquidationPrice"), 0.0),
            "notional": abs(amount * mark_price),  # 名义价值
        }
        if hedge_conflict:
            out["hedge_conflict"] = True
        if legs:
            out["legs"] = legs
        return out

    def _pick_primary_leg(self, positions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not positions:
            return None
        return max(
            positions,
            key=lambda p: (
                abs(self._to_float(p.get("positionAmt"), 0.0) * self._to_float(p.get("markPrice"), 0.0)),
                abs(self._to_float(p.get("positionAmt"), 0.0)),
            ),
        )

    def get_current_position(self, symbol: str, side: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        获取当前持仓

        Returns:
            {
            'side': 'LONG' 或 'SHORT',
                'amount': 0.001,
                'entry_price': 115000.0,
                'mark_price': 115050.0,
                'leverage': 10,
                'margin': 115.0,
                'unrealized_pnl': 5.0,
                'pnl_percent': 0.43,
                'liquidation_price': 105000.0
            }
        """
        try:
            symbol_up = str(symbol or "").upper()
            requested_side = str(side or "").upper()

            raw_positions = self.client.get_all_positions() if hasattr(self.client, "get_all_positions") else []
            candidates: List[Dict[str, Any]] = []
            for pos in raw_positions or []:
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("symbol") or "").upper() != symbol_up:
                    continue
                amt = self._to_float(pos.get("positionAmt"), 0.0)
                if abs(amt) <= 0:
                    continue
                resolved = self._resolve_side(pos.get("positionSide"), amt)
                if requested_side in ("LONG", "SHORT") and resolved != requested_side:
                    continue
                candidates.append(pos)

            if not candidates:
                return None

            if requested_side in ("LONG", "SHORT"):
                return self._build_position_payload(candidates[0])

            primary = self._pick_primary_leg(candidates)
            if not primary:
                return None

            leg_views: List[Dict[str, Any]] = []
            side_set = set()
            for leg in candidates:
                amt = self._to_float(leg.get("positionAmt"), 0.0)
                leg_side = self._resolve_side(leg.get("positionSide"), amt)
                if leg_side in ("LONG", "SHORT"):
                    side_set.add(leg_side)
                leg_views.append(
                    {
                        "side": leg_side or "UNKNOWN",
                        "amount": abs(amt),
                        "notional": abs(amt * self._to_float(leg.get("markPrice"), 0.0)),
                    }
                )
            hedge_conflict = len(side_set) > 1
            return self._build_position_payload(primary, hedge_conflict=hedge_conflict, legs=leg_views if hedge_conflict else None)
        except Exception as e:
            print(f"⚠️ 获取持仓失败 {symbol}: {e}")
            return None

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有持仓

        Returns:
            {
            'BTCUSDT': {...},
                'ETHUSDT': {...},
                ...
            }
        """
        try:
            positions = self.client.get_all_positions() if hasattr(self.client, "get_all_positions") else []
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for pos in positions or []:
                if not isinstance(pos, dict):
                    continue
                symbol = str(pos.get("symbol") or "").upper()
                if not symbol:
                    continue
                amount = self._to_float(pos.get("positionAmt"), 0.0)
                if abs(amount) <= 0:
                    continue
                grouped.setdefault(symbol, []).append(pos)

            result: Dict[str, Dict[str, Any]] = {}
            for symbol, legs in grouped.items():
                primary = self._pick_primary_leg(legs)
                if not primary:
                    continue
                leg_views: List[Dict[str, Any]] = []
                side_set = set()
                for leg in legs:
                    amt = self._to_float(leg.get("positionAmt"), 0.0)
                    leg_side = self._resolve_side(leg.get("positionSide"), amt)
                    if leg_side in ("LONG", "SHORT"):
                        side_set.add(leg_side)
                    leg_views.append(
                        {
                            "side": leg_side or "UNKNOWN",
                            "amount": abs(amt),
                            "notional": abs(amt * self._to_float(leg.get("markPrice"), 0.0)),
                        }
                    )
                hedge_conflict = len(side_set) > 1
                pos_data = self._build_position_payload(primary, hedge_conflict=hedge_conflict, legs=leg_views if hedge_conflict else None)
                if pos_data is not None:
                    result[symbol] = pos_data
            return result
        except Exception as e:
            print(f"⚠️ 获取所有持仓失败: {e}")
            return {}

    def has_position(self, symbol: str) -> bool:
        """检查是否有持仓"""
        position = self.get_current_position(symbol)
        return position is not None
