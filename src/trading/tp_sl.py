import time

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Literal, Tuple
import json
import os

Side = Literal["BUY", "SELL"]
PositionSide = Literal["LONG", "SHORT"]


@dataclass
class TpSlConfig:
    symbol: str
    position_side: PositionSide
    entry_price: float

    quantity: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None

    rr_ratio: Optional[float] = None


class PapiTpSlManager:
    def __init__(self, broker: Any) -> None:
        self.broker = broker
        self._tick_size_cache: Dict[str, float] = {}
        self._tick_cache_path = os.getenv("BINANCE_TICK_CACHE_PATH", "data/tick_size_cache.json")
        self._load_tick_cache()

    def _load_tick_cache(self) -> None:
        try:
            if not os.path.exists(self._tick_cache_path):
                return
            with open(self._tick_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        self._tick_size_cache[k] = float(v)
                    except Exception:
                        continue
        except Exception:
            return

    def _save_tick_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._tick_cache_path), exist_ok=True)
            with open(self._tick_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._tick_size_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            return

    def place_tp_sl(self, cfg: TpSlConfig) -> List[Dict[str, Any]]:
        # 🔥 校验entry_price是否有效
        if cfg.entry_price <= 0:
            print(f"❌ TP/SL entry_price无效: {cfg.entry_price}, symbol: {cfg.symbol}")
            return [{"code": -4006, "msg": "Entry price less than or equal to zero."}]

        sl_price, tp_price = self._resolve_prices(cfg)
        orders = []

        if sl_price and sl_price > 0:
            orders.append(self._build_sl_order(cfg, sl_price))

        if tp_price and tp_price > 0:
            orders.append(self._build_tp_order(cfg, tp_price))

        results = []
        for order in orders:
            if not order.get("quantity"):
                # 尝试在短时间内重试获取仓位数量（防止刚开仓时尚未同步）
                qty = None
                for _ in range(3):
                    qty = self._resolve_close_quantity(cfg)
                    if qty:
                        order["quantity"] = qty
                        break
                    time.sleep(0.2)
                if not order.get("quantity"):
                    print(f"⚠️ TP/SL 未能获取数量，跳过: {order.get('symbol')}")
                    continue
            resp = self.broker.request(
                "POST",
                self._order_endpoint(),
                params=order,
                signed=True,
                allow_error=True,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"status_code": resp.status_code, "text": resp.text}
            results.append(data)

        return results

    def _resolve_prices(self, cfg: TpSlConfig) -> Tuple[Optional[float], Optional[float]]:
        entry = cfg.entry_price
        sl = cfg.stop_loss_price
        tp = cfg.take_profit_price

        # 优先使用 stop_loss + RR 反算 take_profit（当 take_profit_pct 未提供或非常小时）
        rr = cfg.rr_ratio if cfg.rr_ratio is not None else 1.0

        # 先根据 stop_loss_pct 计算止损（如果未显式提供止损价格）
        if sl is None and cfg.stop_loss_pct:
            sl_pct = self._normalize_pct(cfg.stop_loss_pct)
            sl = self._calc_by_pct(entry, sl_pct, cfg.position_side, True)

        # 决定是否使用 RR 计算 TP：当未提供 take_profit_price 且 take_profit_pct 缺失或接近 0 时，使用 RR
        use_rr = False
        if (tp is None) and (cfg.take_profit_pct is None or float(cfg.take_profit_pct) <= 1e-6):
            use_rr = True

        if use_rr and sl is not None:
            tp = self._calc_by_rr(entry, sl, rr, cfg.position_side)
        else:
            # 否则若明确提供了 take_profit_pct，则按百分比计算
            if tp is None and cfg.take_profit_pct:
                tp_pct = self._normalize_pct(cfg.take_profit_pct)
                tp = self._calc_by_pct(entry, tp_pct, cfg.position_side, False)

        return sl, tp

    def _calc_by_pct(self, entry: float, pct: float, pos_side: PositionSide, is_sl: bool) -> float:
        if pos_side == "LONG":
            return entry * (1 - pct) if is_sl else entry * (1 + pct)
        return entry * (1 + pct) if is_sl else entry * (1 - pct)

    def _calc_by_rr(self, entry: float, sl: float, rr: float, pos_side: PositionSide) -> float:
        risk = abs(entry - sl)
        if pos_side == "LONG":
            return entry + risk * rr
        return entry - risk * rr

    def _normalize_pct(self, pct: float) -> float:
        try:
            val = float(pct)
        except Exception:
            return 0.0
        val = abs(val)
        if val > 1.0:
            return val / 100.0
        return val

    def _base_order(self, cfg: TpSlConfig) -> Dict[str, Any]:
        order_side: Side = "SELL" if cfg.position_side == "LONG" else "BUY"
        order: Dict[str, Any] = {
            "symbol": cfg.symbol,
            "side": order_side,
            "workingType": "MARK_PRICE",
            "timeInForce": "GTC",
            "closePosition": True,
        }

        pos_side = self.broker.calculate_position_side(order_side, True)
        if pos_side:
            order["positionSide"] = pos_side

        # PAPI 条件单必须带 quantity
        if cfg.quantity is not None:
            order["quantity"] = cfg.quantity
        else:
            qty = self._resolve_close_quantity(cfg)
            if qty is not None:
                order["quantity"] = qty

        return order

    def _build_sl_order(self, cfg: TpSlConfig, stop_price: float) -> Dict[str, Any]:
        order = self._base_order(cfg)
        order.update(
            {
                "strategyType": "STOP",
                "stopPrice": self._round(stop_price, cfg.symbol),
                "price": self._round(stop_price, cfg.symbol),
            }
        )
        return order

    def _build_tp_order(self, cfg: TpSlConfig, stop_price: float) -> Dict[str, Any]:
        order = self._base_order(cfg)
        order.update(
            {
                "strategyType": "TAKE_PROFIT",
                "stopPrice": self._round(stop_price, cfg.symbol),
                "price": self._round(stop_price, cfg.symbol),
            }
        )
        return order

    def _resolve_close_quantity(self, cfg: TpSlConfig) -> Optional[float]:
        try:
            pos = self.broker.position.get_position(cfg.symbol, side=cfg.position_side)
            if not pos:
                return None
            qty = abs(float(pos.get("positionAmt", 0)))
            return qty if qty > 0 else None
        except Exception:
            return None

    def _order_endpoint(self) -> str:
        base = self.broker.PAPI_BASE
        return f"{base}/papi/v1/um/conditional/order"

    def _round(self, price: float, symbol: str) -> float:
        tick_size = self._get_tick_size(symbol)
        if tick_size is None:
            return self._round_fallback(price)
        return self._round_to_tick(price, tick_size)

    def _round_fallback(self, price: float) -> float:
        if price <= 0:
            return price
        if price < 0.0001:
            decimals = 8
        elif price < 0.01:
            decimals = 6
        elif price < 1:
            decimals = 5
        elif price < 10:
            decimals = 4
        elif price < 100:
            decimals = 3
        else:
            decimals = 2
        return round(price, decimals)

    def _round_to_tick(self, price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return price
        price_dec = Decimal(str(price))
        tick_dec = Decimal(str(tick_size))
        rounded = (price_dec / tick_dec).to_integral_value(rounding=ROUND_DOWN) * tick_dec
        return float(rounded)

    def _get_tick_size(self, symbol: str) -> Optional[float]:
        if symbol in self._tick_size_cache:
            return self._tick_size_cache[symbol]
        try:
            url = f"{self.broker.FAPI_BASE}/fapi/v1/exchangeInfo"
            resp = self.broker.request("GET", url, params={"symbol": symbol}, signed=False)
            data = resp.json()
            symbols = data.get("symbols", []) if isinstance(data, dict) else []
            if symbols:
                filters = symbols[0].get("filters", [])
                for f in filters:
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f.get("tickSize", 0))
                        if tick > 0:
                            self._tick_size_cache[symbol] = tick
                            self._save_tick_cache()
                            return tick
        except Exception:
            pass
        return None
