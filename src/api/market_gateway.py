import math
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
                tick_size = 0.01
                min_notional = 5.0

                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
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

    def ensure_min_notional_quantity(self, symbol: str, quantity: float, price: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info or quantity <= 0 or price <= 0:
            return quantity

        min_notional = info["min_notional"]
        if quantity * price >= min_notional:
            return quantity

        required_qty = min_notional / price
        step_size = info["step_size"]
        if step_size > 0:
            required_qty = math.ceil(required_qty / step_size) * step_size

        return self.format_quantity(symbol, required_qty)

    def _get_precision(self, val: float) -> int:
        if val >= 1:
            return 0
        s = f"{val:.10f}".rstrip("0")
        if "." in s:
            return len(s.split(".")[1])
        return 0
