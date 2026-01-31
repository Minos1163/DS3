import hashlib
import hmac
import os
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import requests  # type: ignore

from src.api.market_gateway import MarketGateway
from src.trading import position_state_machine
from src.trading.event_router import ExchangeEventRouter
from src.trading.intents import PositionSide as IntentPositionSide
from src.trading.intents import TradeIntent
from src.trading.order_gateway import OrderGateway


class ApiCapability(Enum):
    STANDARD = "STANDARD"
    PAPI_ONLY = "PAPI_ONLY"


class AccountMode(Enum):
    CLASSIC = "CLASSIC"
    UNIFIED = "UNIFIED"


class BinanceBroker:
    """底层的 HTTP 会话与签名引擎 (适配 PAPI/FAPI)"""

    FAPI_BASE = "https://fapi.binance.com"
    PAPI_BASE = "https://papi.binance.com"
    SPOT_BASE = "https://api.binance.com"
    MARKET_BASE = "https://fapi.binance.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.dry_run = os.getenv("BINANCE_DRY_RUN") == "1"
        self.capability = self._detect_api_capability()
        self.account_mode = self._detect_account_mode()

        self.order = OrderGateway(self)
        self.position = PositionGateway(self)
        self.balance = BalanceEngine(self)

        self._hedge_mode_cache: Optional[Tuple[bool, float]] = None
        self._HEDGE_MODE_CACHE_TTL = 10.0

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    def _signed_params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(params or {})
        payload.setdefault("recvWindow", 5000)
        payload["timestamp"] = int(time.time() * 1000)

        # 转换为精确字符串，避免科学计数法
        norm = {}
        for k, v in payload.items():
            norm[k] = self._normalize_value(v)

        parts = []
        for k, v in sorted(norm.items()):
            parts.append(f"{k}={v}")
        query = "&".join(parts)
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        )
        signature = mac.hexdigest()

        ordered = {}
        for k, v in sorted(norm.items()):
            ordered[k] = v
        ordered["signature"] = signature
        return ordered

    def _normalize_value(self, v: Any) -> str:
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (float, int)):
            # 避免科学计数法，并移除多余的 .0
            return "{:.10f}".format(float(v)).rstrip("0").rstrip(".")
        return str(v)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        allow_error: bool = False,
    ) -> requests.Response:
        input_params = dict(params or {})

        # 兜底：如果存在 closePosition，则物理移除 reduceOnly（PAPI 要求）
        # 注意：根据实际测试，PAPI 全仓平仓也需要 quantity 参数，所以不移除 quantity
        if (
            input_params.get("closePosition") is True
            or str(input_params.get("closePosition")).lower() == "true"
        ):
            input_params.pop("reduceOnly", None)
            input_params.pop("reduce_only", None)
            # 保持 quantity 字段，PAPI 全仓平仓需要这个参数

        payload = self._signed_params(input_params) if signed else input_params
        headers = self._headers()
        is_papi = url.startswith(self.PAPI_BASE)
        method_upper = method.upper()
        if is_papi and method_upper in {"POST", "PUT", "DELETE"}:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            resp = requests.request(
                method,
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
            )
        else:
            resp = requests.request(
                method,
                url,
                params=payload,
                headers=headers,
                timeout=self.timeout,
            )

        if not allow_error:
            if resp.status_code >= 400:
                # 避免一行过长，分开打印状态码和消息
                print("❌ Binance Error (%s):" % (resp.status_code,))
                print(resp.text)
            resp.raise_for_status()
        return resp

    def _detect_api_capability(self) -> ApiCapability:
        try:
            url = f"{self.FAPI_BASE}/fapi/v2/account"
            resp = self.request("GET", url, signed=True, allow_error=True)
            if resp.status_code == 200:
                return ApiCapability.STANDARD
        except Exception:
            pass
        return ApiCapability.PAPI_ONLY

    def _detect_account_mode(self) -> AccountMode:
        try:
            url = f"{self.PAPI_BASE}/papi/v1/um/account"
            resp = self.request("GET", url, signed=True, allow_error=True)
            if resp.status_code == 200:
                return AccountMode.UNIFIED
        except Exception:
            pass
        return AccountMode.CLASSIC

    def um_base(self) -> str:
        if (
            self.capability == ApiCapability.PAPI_ONLY
            or self.account_mode == AccountMode.UNIFIED
        ):
            return self.PAPI_BASE
        return self.FAPI_BASE

    def is_papi_only(self) -> bool:
        """是否为 PAPI_ONLY 能力或统一保证金账户（需要使用 PAPI-UM 下单）"""
        return (
            self.capability == ApiCapability.PAPI_ONLY
            or self.account_mode == AccountMode.UNIFIED
        )

    def get_hedge_mode(self) -> bool:
        """查询持仓模式 (缓存 10s)"""
        now = time.time()
        if self._hedge_mode_cache and (
            now - self._hedge_mode_cache[1] < self._HEDGE_MODE_CACHE_TTL
        ):
            return self._hedge_mode_cache[0]
        try:
            url = f"{self.PAPI_BASE}/papi/v1/um/positionSide/dual"
            resp = self.request("GET", url, signed=True, allow_error=True)
            data = resp.json()
            val = data.get("dualSidePosition", False)
            self._hedge_mode_cache = (val, now)
            return val
        except Exception:
            return False

    def calculate_position_side(self, side: str, reduce_only: bool) -> Optional[str]:
        if not self.get_hedge_mode():
            return None
        s = side.upper()
        if s == "BUY":
            return "SHORT" if reduce_only else "LONG"
        return "LONG" if reduce_only else "SHORT"


class PositionGateway:
    def __init__(self, broker: BinanceBroker) -> None:
        self.broker = broker

    def get_positions(self) -> List[Dict[str, Any]]:
        base = self.broker.um_base()
        if "papi" in base:
            path = "/papi/v1/um/positionRisk"
        else:
            path = "/fapi/v2/positionRisk"
        url = f"{base}{path}"
        resp = self.broker.request("GET", url, signed=True)
        return resp.json()

    def get_position(
        self, symbol: str, side: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        for p in self.get_positions():
            if p.get("symbol") == symbol:
                if side:
                    # 如果提供了 side (LONG/SHORT/BOTH)，进行匹配
                    if p.get("positionSide", "BOTH") == side.upper():
                        return p
                else:
                    # 未提供 side，返回第一个非零仓位（单向模式适用）
                    if abs(float(p.get("positionAmt", 0))) > 0:
                        return p
        return None

    def change_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        base = self.broker.um_base()
        path = "/papi/v1/um/leverage" if "papi" in base else "/fapi/v1/leverage"
        params = {"symbol": symbol, "leverage": leverage}
        url = f"{base}{path}"
        return self.broker.request(
            "POST",
            url,
            params=params,
            signed=True,
        ).json()

    def change_margin_type(self, symbol: str, margin_type: str) -> Dict[str, Any]:
        base = self.broker.um_base()
        path = "/papi/v1/um/marginType" if "papi" in base else "/fapi/v1/marginType"
        params = {"symbol": symbol, "marginType": margin_type.upper()}
        url = f"{base}{path}"
        return self.broker.request(
            "POST",
            url,
            params=params,
            signed=True,
        ).json()

    def set_hedge_mode(self, enabled: bool = True) -> Dict[str, Any]:
        base = self.broker.um_base()
        path = (
            "/papi/v1/um/positionSide/dual"
            if "papi" in base
            else "/fapi/v1/positionSide/dual"
        )
        params = {"dualSidePosition": "true" if enabled else "false"}
        url = f"{base}{path}"
        res = self.broker.request(
            "POST",
            url,
            params=params,
            signed=True,
        ).json()
        # 清除缓存强制更新
        self.broker._hedge_mode_cache = None
        return res


class BalanceEngine:
    def __init__(self, broker: BinanceBroker) -> None:
        self.broker = broker

    def get_balance(self) -> Dict[str, Any]:
        base = self.broker.um_base()
        # 🔥 修改点：对于 PAPI 账户，使用更全面的 /papi/v1/account 获取综合资产（含全仓杠杆和 U 本位合约）
        # 之前使用的 /papi/v1/um/account 仅显示 U 本位合约子账户
        is_papi = "papi" in base
        if is_papi:
            path = "/papi/v1/account"
        else:
            path = "/fapi/v2/account"
        url = f"{base}{path}"
        resp = self.broker.request("GET", url, signed=True)
        data = resp.json()
        # 统一标准化字段，确保兼容 AccountDataManager
        if is_papi:
            available = float(data.get("totalMarginBalance", 0)) - float(
                data.get("accountInitialMargin", 0)
            )
            total_wallet = float(data.get("totalWalletBalance", 0))
            available_balance = available
            total_margin = float(data.get("totalMarginBalance", 0))
            total_initial = float(data.get("accountInitialMargin", 0))
            total_unrealized = float(data.get("totalUnrealizedProfit", 0) or 0)
            account_equity = float(data.get("accountEquity", 0))
            return {
                "totalWalletBalance": total_wallet,
                "availableBalance": available_balance,
                "totalMarginBalance": total_margin,
                "totalInitialMargin": total_initial,
                "totalUnrealizedProfit": total_unrealized,
                "accountEquity": account_equity,
                "available": available_balance,
                "equity": account_equity,
                "raw": data,
            }

            # 标准 FAPI 路径
        total_wallet = float(data.get("totalWalletBalance", 0))
        avail = float(data.get("availableBalance", 0))
        total_margin = float(data.get("totalMarginBalance", 0) or total_wallet)
        total_initial = float(data.get("totalInitialMargin", 0))
        total_unrealized = float(data.get("totalUnrealizedProfit", 0))
        equity_val = float(data.get("totalMarginBalance", 0) or total_wallet)
        return {
            "totalWalletBalance": total_wallet,
            "availableBalance": avail,
            "totalMarginBalance": total_margin,
            "totalInitialMargin": total_initial,
            "totalUnrealizedProfit": total_unrealized,
            "available": avail,
            "equity": equity_val,
            "raw": data,
        }


class BinanceClient:
    """
    Binance API 客户端 (V2 瘦身架构)

    统一入口: execute_intent(intent)
    所有行情、下单、持仓逻辑均已委托至子模块。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: int = 30,
    ):
        k = api_key or os.getenv("BINANCE_API_KEY")
        s = api_secret or os.getenv("BINANCE_SECRET")
        if not k or not s:
            raise ValueError("❌ 缺少 API 凭证")

        self.broker = BinanceBroker(k, s, timeout=timeout)
        self.market = MarketGateway(self.broker)
        self.position_gateway = self.broker.position
        self.balance_engine = self.broker.balance
        self._order_gateway = self.broker.order
        sm = position_state_machine.PositionStateMachineV2(self)
        self.state_machine = sm
        self.event_router = ExchangeEventRouter(self.state_machine)

        # 使用两个参数避免超过行长限制
        print("[Client] 初始化完成 | 模式:", self.broker.account_mode.value)

    def execute_intent(self, intent: TradeIntent) -> Dict[str, Any]:
        """唯一交易入口"""
        return self.state_machine.apply_intent(intent)

    def sync_state(self):
        """同步本地状态机与交易所真实状态 (防止状态丢失)"""
        positions = self.get_all_positions()
        open_orders = self.get_open_orders()
        self.state_machine.sync_with_exchange(positions, open_orders)
        snapshots_count = len(self.state_machine.snapshots)
        return {"status": "success", "snapshots": snapshots_count}

    def handle_exchange_event(self, event_data: dict, source: str = "WS"):
        """
        处理来自外部的交易所事件 (WebSocket 推送或消息队列)
        将原始数据转化为统一的 ExchangeEvent 并路由至状态机。
        """
        # 这里仅作示例，实际需根据 source 类型和 event_data 格式进行详细解析
        from src.trading.events import ExchangeEvent, ExchangeEventType

        # 1. 如果是 WebSocket 的订单成交推送 (e: 'ORDER_TRADE_UPDATE')
        if event_data.get("e") == "ORDER_TRADE_UPDATE":
            o = event_data.get("o", {})
            event_type = (
                ExchangeEventType.ORDER_FILLED
                if o.get("X") == "FILLED"
                else ExchangeEventType.ORDER_CANCELED
            )
            event = ExchangeEvent(
                type=event_type,
                symbol=o.get("s", ""),
                order_id=o.get("i"),
                side=o.get("S"),
                position_side=o.get("ps", "BOTH"),
                filled_qty=float(o.get("l", 0)),
            )
            self.event_router.dispatch(event)

        # 2. 如果是 WebSocket 的持仓变更推送 (e: 'ACCOUNT_UPDATE')
        elif event_data.get("e") == "ACCOUNT_UPDATE":
            a = event_data.get("a", {})
            for p in a.get("P", []):
                event = ExchangeEvent(
                    type=ExchangeEventType.POSITION_UPDATE,
                    symbol=p.get("s", ""),
                    position_amt=float(p.get("pa", 0)),
                    position_side=p.get("ps", "BOTH"),
                )
                self.event_router.dispatch(event)

    # 行情 (委托)
    def get_klines(self, *args, **kwargs):
        return self.market.get_klines(*args, **kwargs)

    def get_ticker(self, *args, **kwargs):
        return self.market.get_ticker(*args, **kwargs)

    def get_funding_rate(self, *args, **kwargs):
        return self.market.get_funding_rate(*args, **kwargs)

    def get_open_interest(self, *args, **kwargs):
        return self.market.get_open_interest(*args, **kwargs)

    def format_quantity(self, symbol: str, qty: float) -> float:
        return self.market.format_quantity(symbol, qty)

    def ensure_min_notional_quantity(
        self, symbol: str, quantity: float, price: float
    ) -> float:
        return self.market.ensure_min_notional_quantity(symbol, quantity, price)

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.market.get_symbol_info(symbol)

    # 账户 (委托)
    def get_account(self) -> Dict[str, Any]:
        return self.balance_engine.get_balance()

    def get_position(
        self, symbol: str, side: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        return self.position_gateway.get_position(symbol, side)

    def get_all_positions(self) -> List[Dict[str, Any]]:
        return self.position_gateway.get_positions()

    def set_hedge_mode(self, enabled: bool = True):
        return self.position_gateway.set_hedge_mode(enabled)

    # 订单 (委托)
    def cancel_order(self, symbol: str, order_id: int):
        return self._order_gateway.cancel_order(symbol, order_id)

    def cancel_all_open_orders(self, symbol: str):
        """撤销某个币种的所有挂单"""
        base = self.broker.um_base()
        if "papi" in base:
            path = "/papi/v1/um/allOpenOrders"
        else:
            path = "/fapi/v1/allOpenOrders"
        url = f"{base}{path}"
        return self.broker.request(
            "DELETE",
            url,
            params={"symbol": symbol},
            signed=True,
        ).json()

    def get_open_orders(self, symbol: Optional[str] = None):
        return self._order_gateway.query_open_orders(symbol)

    # 内部执行逻辑 (供状态机调用)
    def _execute_order_v2(
        self,
        params: Dict[str, Any],
        side: str,
        reduce_only: bool,
    ) -> Dict[str, Any]:
        """由状态机调用的原始下单接口"""
        if self.broker.dry_run:
            # 模拟下单返回
            return {
                "status": "success",
                "dry_run": True,
                "orderId": 888,
                "params": params,
            }
        return self._order_gateway.place_standard_order(
            symbol=params.get("symbol", ""),
            side=side,
            params=params,
            reduce_only=reduce_only,
        )

    def _execute_protection_v2(
        self,
        symbol: str,
        side: IntentPositionSide,
        tp: Optional[float],
        sl: Optional[float],
    ) -> Dict[str, Any]:
        """由状态机调用的保护单下单接口"""
        if self.broker.dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "tp": tp,
                "sl": sl,
            }

        results = self._order_gateway.place_protection_orders(
            symbol=symbol,
            side=side.value,
            tp=tp,
            sl=sl,
        )
        return {"status": "success", "orders": results}

    def get_server_time(self):
        url = f"{self.broker.FAPI_BASE}/fapi/v1/time"
        return self.broker.request("GET", url).json()

    def test_connection(self):
        try:
            return self.get_server_time() is not None
        except Exception:
            return False
