import math
import os
from typing import Any, Dict, List, Optional


class MarketGateway:
    """
    📊 行情与元数据网关
    负责：K线、行情、交易对精度、最小名义价值等元数据的获取与缓存
    """

    def __init__(self, broker) -> None:
        self.broker = broker
        self._symbol_info_cache: Dict[str, Dict[str, Any]] = {}

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[List[Any]]:
        url = f"{self.broker.MARKET_BASE}/fapi/v1/klines"
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        response = self.broker.request("GET", url, params=params)
        return response.json()

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"{self.broker.MARKET_BASE}/fapi/v1/ticker/24hr"
        response = self.broker.request("GET", url, params={"symbol": symbol})
        return response.json()

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        url = f"{self.broker.MARKET_BASE}/fapi/v1/fundingRate"
        response = self.broker.request("GET", url, params={"symbol": symbol, "limit": 1})
        data = response.json()
        if data:
            rate = data[0].get("fundingRate") or data[0].get("rate")
            return float(rate) if rate is not None else None
        return None

    def get_open_interest(self, symbol: str) -> Optional[float]:
        url = f"{self.broker.MARKET_BASE}/fapi/v1/openInterest"
        response = self.broker.request("GET", url, params={"symbol": symbol})
        data = response.json()
        return float(data.get("openInterest", 0)) if data else None

    def get_open_interest_hist(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        url = f"{self.broker.MARKET_BASE}/futures/data/openInterestHist"
        params: Dict[str, Any] = {
            "symbol": symbol,
            "period": period,
            "limit": max(1, min(int(limit or 30), 500)),
        }
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        response = self.broker.request("GET", url, params=params)
        data = response.json()
        return data if isinstance(data, list) else []

    def get_order_book(self, symbol: str, limit: int = 20) -> Optional[Dict[str, Any]]:
        # Binance futures depth supports: 5, 10, 20, 50, 100, 500, 1000.
        try:
            lim = int(limit or 20)
        except Exception:
            lim = 20
        supported = (5, 10, 20, 50, 100, 500, 1000)
        if lim not in supported:
            lim = min(supported, key=lambda x: abs(x - lim))
        url = f"{self.broker.MARKET_BASE}/fapi/v1/depth"
        response = self.broker.request("GET", url, params={"symbol": symbol, "limit": lim})
        data = response.json()
        if isinstance(data, dict):
            return data
        return None

    def get_exchange_info(self) -> Optional[Dict[str, Any]]:
        url = f"{self.broker.MARKET_BASE}/fapi/v1/exchangeInfo"
        response = self.broker.request("GET", url)
        return response.json()

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        info = self.get_exchange_info()
        if not info:
            return None

        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                quantity_precision = 3
                price_precision = 2
                step_size = 0.001
                min_qty = 0.001
                tick_size = 0.01
                min_notional = 5.0

                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                        min_qty = float(f.get("minQty") or step_size)
                        quantity_precision = self._get_precision(step_size)
                    elif f["filterType"] == "PRICE_FILTER":
                        tick_size = float(f["tickSize"])
                        price_precision = self._get_precision(tick_size)
                    elif f["filterType"] in ["MIN_NOTIONAL", "NOTIONAL"]:
                        min_notional = float(f.get("minNotional") or f.get("notional") or 5.0)

                res = {
                    "symbol": symbol,
                    "quantity_precision": quantity_precision,
                    "price_precision": price_precision,
                    "step_size": step_size,
                    "min_qty": min_qty,
                    "tick_size": tick_size,
                    "min_notional": min_notional,
                }
                self._symbol_info_cache[symbol] = res
                return res
        return None

    def format_quantity(self, symbol: str, quantity: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info:
            return round(quantity, 3)

        step_size = info["step_size"]
        precision = info["quantity_precision"]

        # 使用 math.floor 避免精度陷阱
        val = float(math.floor(quantity / step_size) * step_size)
        return round(val, precision)

    @staticmethod
    def _safe_env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except Exception:
            return default

    def _min_notional_target(self, min_notional: float) -> float:
        # 默认较旧逻辑更保守，减少 -4164 边界拒单。
        pct_buffer = max(0.0, self._safe_env_float("BINANCE_MIN_NOTIONAL_BUFFER_PCT", 0.02))
        abs_buffer = max(0.0, self._safe_env_float("BINANCE_MIN_NOTIONAL_BUFFER_ABS", 0.2))
        return max(min_notional * (1.0 + pct_buffer), min_notional + abs_buffer)

    def ensure_min_notional_quantity(self, symbol: str, quantity: float, price: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info or quantity <= 0 or price <= 0:
            return quantity

        min_notional = float(info["min_notional"])
        min_qty = float(info.get("min_qty", info.get("step_size", 0)) or 0)
        step_size = float(info.get("step_size", 0) or 0)
        precision = int(info.get("quantity_precision", 3) or 3)

        # PAPI 的 -4164 文案为“must be greater than”，且价格会在下单瞬间波动。
        min_notional_target = self._min_notional_target(min_notional)

        qty_now = float(quantity)
        if qty_now * price >= min_notional_target:
            qty_fmt = self.format_quantity(symbol, qty_now)
            if min_qty > 0 and qty_fmt < min_qty:
                qty_fmt = round(min_qty, precision)
            if qty_fmt * price >= min_notional_target:
                return qty_fmt
            qty_now = qty_fmt

        required_qty = min_notional_target / price
        if step_size > 0:
            required_steps = math.ceil((required_qty / step_size) - 1e-12)
            required_qty = required_steps * step_size

        adjusted_qty = round(required_qty, precision)
        if min_qty > 0 and adjusted_qty < min_qty:
            adjusted_qty = round(min_qty, precision)

        # 量化精度后再严格检查目标名义额，不足则按 step 继续上调。
        notional_now = adjusted_qty * price
        if step_size > 0 and notional_now < min_notional_target:
            step_notional = step_size * price
            if step_notional > 0:
                extra_steps = math.ceil(((min_notional_target - notional_now) / step_notional) - 1e-12)
                if extra_steps > 0:
                    adjusted_qty = round(adjusted_qty + extra_steps * step_size, precision)

        if step_size > 0 and adjusted_qty * price <= min_notional:
            adjusted_qty = round(adjusted_qty + step_size, precision)
        return adjusted_qty

    def _get_precision(self, val: float) -> int:
        if val >= 1:
            return 0
        s = f"{val:.10f}".rstrip("0")
        if "." in s:
            return len(s.split(".")[1])
        return 0
