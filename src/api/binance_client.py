from typing import Any, Dict, List, Optional, Tuple

import requests  # type: ignore

from requests.adapters import HTTPAdapter

from urllib3.util.retry import Retry

from src.api.market_gateway import MarketGateway

from src.trading import position_state_machine

from src.trading.event_router import ExchangeEventRouter

from src.trading.intents import PositionSide as IntentPositionSide

from src.trading.intents import TradeIntent

from src.trading.order_gateway import OrderGateway

from src.trading.tp_sl import PapiTpSlManager, TpSlConfig

import hashlib
import hmac
import os
import time
from enum import Enum


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
        self._time_offset_ms: int = 0
        self._time_offset_updated_at: float = 0.0
        self._TIME_OFFSET_TTL = 60.0

        # 设置 requests 会话重试策略
        self._session = requests.Session()
        self._proxies = self._load_proxies()
        self._disable_env_proxy = os.getenv("BINANCE_DISABLE_PROXY") == "1"
        self._proxy_fallback = os.getenv("BINANCE_PROXY_FALLBACK") == "1"
        self._force_direct = os.getenv("BINANCE_FORCE_DIRECT") == "1"
        self._session.trust_env = not self._disable_env_proxy
        retry_strategy = Retry(
            total=3,  # 最多重试 3 次
            backoff_factor=0.5,  # 重试延迟：0.5s, 1s, 2s
            status_forcelist=[429, 500, 502, 503, 504],  # 重试这些状态码
            allowed_methods=["GET", "POST", "PUT", "DELETE"],  # SSL/连接错误会自动重试
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # 初始化时间偏移（避免 -1021 时间戳超前/滞后）
        self._sync_time_offset(force=True)

    def _load_proxies(self) -> Optional[Dict[str, str]]:
        proxy = os.getenv("BINANCE_PROXY")
        http_proxy = os.getenv("BINANCE_HTTP_PROXY")
        https_proxy = os.getenv("BINANCE_HTTPS_PROXY")

        if proxy:
            return {"http": proxy, "https": proxy}
        proxies = {}
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        return proxies or None

    def _is_proxy_related_error(self, error: Exception) -> bool:
        if isinstance(error, requests.exceptions.ProxyError):
            return True
        message = str(error).lower()
        return "proxy" in message

    def get_connection_mode(self) -> str:
        """返回当前连接模式（代理/直连）"""
        if self._force_direct or self._disable_env_proxy:
            return "直连"
        if self._proxies:
            return f"代理({list(self._proxies.values())[0]})"
        return "系统代理"

    def get_forced_account_mode(self) -> Optional[str]:
        forced_mode = os.getenv("BINANCE_ACCOUNT_MODE", "").strip().upper()
        if forced_mode in {"UNIFIED", "CLASSIC"}:
            return forced_mode
        return None

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    def _signed_params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(params or {})
        payload.setdefault("recvWindow", 10000)
        payload["timestamp"] = int(time.time() * 1000) + self._get_time_offset_ms()

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

    def _get_time_offset_ms(self) -> int:
        now = time.time()
        if now - self._time_offset_updated_at > self._TIME_OFFSET_TTL:
            self._sync_time_offset(force=True)
        return self._time_offset_ms

    def _sync_time_offset(self, force: bool = False) -> None:
        if not force and (time.time() - self._time_offset_updated_at) <= self._TIME_OFFSET_TTL:
            return
        try:
            url = f"{self.MARKET_BASE}/fapi/v1/time"
            resp = self._session.request("GET", url, timeout=self.timeout)
            data = resp.json()
            server_time = int(data.get("serverTime", 0))
            if server_time > 0:
                local_time = int(time.time() * 1000)
                self._time_offset_ms = server_time - local_time
                self._time_offset_updated_at = time.time()
        except Exception:
            # 保留上一次时间偏移，避免因同步失败而中断请求
            self._time_offset_updated_at = time.time()

    def _is_timestamp_error(self, resp: requests.Response) -> bool:
        try:
            data = resp.json()
        except Exception:
            return False
        return str(data.get("code")) == "-1021"

    def _is_html_error(self, resp: requests.Response) -> bool:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            return True
        try:
            text = resp.text.lower()
            if "<html" in text and "binance.com/en/error" in text:
                return True
        except Exception:
            return False
        return False

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
        if input_params.get("closePosition") is True or str(input_params.get("closePosition")).lower() == "true":
            input_params.pop("reduceOnly", None)
            input_params.pop("reduce_only", None)
            # 保持 quantity 字段，PAPI 全仓平仓需要这个参数

        # 如果目标是 PAPI 下单端点，则强制移除 reduceOnly（某些 PAPI 版本会拒绝此参数）
        try:
            if isinstance(url, str) and url.startswith(self.PAPI_BASE):
                input_params.pop("reduceOnly", None)
                input_params.pop("reduce_only", None)
        except Exception:
            pass

        # NOTE: payload must be (re)computed each attempt because timestamp/signature
        # depends on current time offset which may be resynced on -1021 errors.
        headers = self._headers()
        is_papi = url.startswith(self.PAPI_BASE)
        method_upper = method.upper()

        # 自动重试连接错误和超时
        max_retries = 3
        retry_delay = 1
        last_exception = None
        fallback_used = False
        timestamp_retry_limit = 3
        timestamp_retry_count = 0

        for attempt in range(max_retries):
            # recompute payload on each attempt to refresh timestamp/signature
            payload = self._signed_params(input_params) if signed else dict(input_params)
            try:
                request_kwargs = {
                    "headers": headers,
                    "timeout": self.timeout,
                    "proxies": None if self._force_direct else self._proxies,
                }

                if is_papi and method_upper in {"POST", "PUT", "DELETE"}:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    resp = self._session.request(
                        method,
                        url,
                        data=payload,
                        **request_kwargs,
                    )
                else:
                    resp = self._session.request(
                        method,
                        url,
                        params=payload,
                        **request_kwargs,
                    )

                if not allow_error:
                    if self._is_html_error(resp) and not fallback_used:
                        fallback_used = True
                        print("⚠️ 检测到 HTML 错误页，可能被代理重定向，尝试直连重试一次...")
                        trust_env_original = self._session.trust_env
                        self._session.trust_env = False
                        try:
                            fallback_kwargs = {
                                "headers": headers,
                                "timeout": self.timeout,
                                "proxies": None,
                            }
                            if is_papi and method_upper in {"POST", "PUT", "DELETE"}:
                                headers["Content-Type"] = "application/x-www-form-urlencoded"
                                resp = self._session.request(
                                    method,
                                    url,
                                    data=payload,
                                    **fallback_kwargs,
                                )
                            else:
                                resp = self._session.request(
                                    method,
                                    url,
                                    params=payload,
                                    **fallback_kwargs,
                                )
                            if resp.status_code < 400:
                                self._force_direct = True
                                print("✅ 直连成功，后续请求固定直连")
                                return resp
                        finally:
                            self._session.trust_env = trust_env_original
                    if resp.status_code == 400 and self._is_timestamp_error(resp):
                        print("⚠️ 检测到时间戳偏差(-1021)，正在同步服务器时间并重试...")
                        self._sync_time_offset(force=True)
                        timestamp_retry_count += 1
                        current_recv = input_params.get("recvWindow")
                        try:
                            current_recv_val = int(current_recv) if current_recv is not None else 0
                        except (TypeError, ValueError):
                            current_recv_val = 0
                        if current_recv_val < 60000:
                            input_params["recvWindow"] = 60000
                        if timestamp_retry_count >= timestamp_retry_limit:
                            raise RuntimeError("时间戳偏差(-1021)仍然存在，已重试多次。")
                        continue
                    if resp.status_code >= 400:
                        # 避免一行过长，分开打印状态码和消息
                        print("❌ Binance Error (%s):" % (resp.status_code,))
                        print(resp.text)
                    resp.raise_for_status()
                return resp
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.SSLError,
                requests.exceptions.ProxyError,
            ) as e:
                last_exception = e
                if self._proxy_fallback and not fallback_used and self._is_proxy_related_error(e):
                    fallback_used = True
                    print("⚠️ 代理异常，尝试直连重试一次...")
                    trust_env_original = self._session.trust_env
                    self._session.trust_env = False
                    try:
                        fallback_kwargs = {
                            "headers": headers,
                            "timeout": self.timeout,
                            "proxies": None,
                        }
                        if is_papi and method_upper in {"POST", "PUT", "DELETE"}:
                            headers["Content-Type"] = "application/x-www-form-urlencoded"
                            resp = self._session.request(
                                method,
                                url,
                                data=payload,
                                **fallback_kwargs,
                            )
                        else:
                            resp = self._session.request(
                                method,
                                url,
                                params=payload,
                                **fallback_kwargs,
                            )

                        if not allow_error:
                            if resp.status_code >= 400:
                                print("❌ Binance Error (%s):" % (resp.status_code,))
                                print(resp.text)
                            resp.raise_for_status()
                        # 直连成功后固定直连（后续请求不再使用代理）
                        self._force_direct = True
                        print("✅ 直连成功，后续请求固定直连")
                        return resp
                    except Exception as fallback_error:
                        # 直连失败，恢复代理配置继续重试
                        print(f"❌ 直连失败: {fallback_error}，恢复代理继续重试...")
                        self._force_direct = False
                        last_exception = fallback_error
                    finally:
                        self._session.trust_env = trust_env_original
                if attempt < max_retries - 1:
                    print(f"⚠️ 连接错误（第{attempt + 1}次），等待 {retry_delay}s 后重试...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    print(f"❌ 连接失败（已尝试 {max_retries} 次）")
                    raise

        if last_exception is not None:
            raise last_exception
        raise RuntimeError("请求失败，未知原因")

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
        forced_mode = os.getenv("BINANCE_ACCOUNT_MODE", "").strip().upper()
        if forced_mode in {"UNIFIED", "CLASSIC"}:
            return AccountMode[forced_mode]
        try:
            url = f"{self.PAPI_BASE}/papi/v1/account"
            resp = self.request("GET", url, signed=True, allow_error=True)
            if resp.status_code == 200:
                return AccountMode.UNIFIED
        except Exception:
            pass
        return AccountMode.CLASSIC

    def um_base(self) -> str:
        if self.capability == ApiCapability.PAPI_ONLY or self.account_mode == AccountMode.UNIFIED:
            return self.PAPI_BASE
        return self.FAPI_BASE

    def is_papi_only(self) -> bool:
        """是否为 PAPI_ONLY 能力或统一保证金账户（需要使用 PAPI-UM 下单）"""
        return self.capability == ApiCapability.PAPI_ONLY or self.account_mode == AccountMode.UNIFIED

    def get_hedge_mode(self) -> bool:
        """查询持仓模式 (缓存 10s)"""
        now = time.time()
        if self._hedge_mode_cache and (now - self._hedge_mode_cache[1] < self._HEDGE_MODE_CACHE_TTL):
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

    def get_position(self, symbol: str, side: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
        path = "/papi/v1/um/positionSide/dual" if "papi" in base else "/fapi/v1/positionSide/dual"
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
            available = float(data.get("totalMarginBalance", 0)) - float(data.get("accountInitialMargin", 0))
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
        print("[Client] 连接模式:", self.broker.get_connection_mode())
        forced_mode = self.broker.get_forced_account_mode()
        if forced_mode:
            print("[Client] 强制账户模式:", forced_mode)

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
            event_type = ExchangeEventType.ORDER_FILLED if o.get("X") == "FILLED" else ExchangeEventType.ORDER_CANCELED
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

    def ensure_min_notional_quantity(self, symbol: str, quantity: float, price: float) -> float:
        return self.market.ensure_min_notional_quantity(symbol, quantity, price)

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.market.get_symbol_info(symbol)

    # 账户 (委托)

    def get_account(self) -> Dict[str, Any]:
        return self.balance_engine.get_balance()

    def get_position(self, symbol: str, side: Optional[str] = None) -> Optional[Dict[str, Any]]:
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

    def cancel_all_conditional_orders(self, symbol: str):
        """撤销某个币种的所有条件单（STOP/TAKE_PROFIT）"""
        base = self.broker.um_base()
        if "papi" in base:
            path = "/papi/v1/um/conditional/all"
            url = f"{base}{path}"
            return self.broker.request(
                "DELETE",
                url,
                params={"symbol": symbol},
                signed=True,
            ).json()

        # 非 PAPI 模式：使用 allOpenOrders 统一撤销（包含条件单）
        return self.cancel_all_open_orders(symbol)

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

        # 🔥 优先使用position的实际entryPrice，而非ticker的lastPrice
        # 因为开仓后立即设置保护单时，ticker价格可能与实际成交价不同
        entry_price = 0.0
        pos = self.get_position(symbol, side=side.value)
        if pos and abs(float(pos.get("positionAmt", 0))) > 0:
            entry_price = float(pos.get("entryPrice", 0))

        # 如果没有position或entryPrice为0，则使用ticker的lastPrice作为fallback
        if entry_price <= 0:
            try:
                ticker = self.get_ticker(symbol)
                entry_price = float(ticker.get("lastPrice", 0)) if ticker else 0.0
            except Exception:
                entry_price = 0.0

        # 校验entry_price是否有效
        if entry_price <= 0:
            return {
                "status": "error",
                "message": f"Invalid entry_price for {symbol}: {entry_price}. Cannot place protection orders.",
                "orders": [],
            }

        if self.broker.is_papi_only():
            manager = PapiTpSlManager(self.broker)
            cfg = TpSlConfig(
                symbol=symbol,
                position_side=side.value,
                entry_price=entry_price,
                stop_loss_price=sl,
                take_profit_price=tp,
            )
            results = manager.place_tp_sl(cfg)
            return {"status": "success", "orders": results}

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
