"""
Binance API客户端封装
"""
import os
import time
import hmac
import hashlib
import math
from enum import Enum
from typing import Any, Dict, List, Optional

import requests


class AccountMode(Enum):
    CLASSIC = "CLASSIC"
    UNIFIED = "UNIFIED"


class ApiCapability(Enum):
    PAPI_ONLY = "PAPI_ONLY"
    STANDARD = "STANDARD"


class BinanceBroker:
    FAPI_BASE = "https://fapi.binance.com"
    PAPI_BASE = "https://papi.binance.com"
    SPOT_BASE = "https://api.binance.com"

    def __init__(self, api_key: str, api_secret: str, timeout: int = 30) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.capability = self._detect_api_capability()
        # 即使是 PAPI-only 密钥，也先探测账户模型（Classic 走影子余额，Unified 走真实余额）
        self.account_mode = self._detect_account_mode()
        self.order = OrderGateway(self)
        self.position = PositionGateway(self)
        self.balance = BalanceEngine(self)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json"
        }

    def _signed_params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(params or {})
        payload.setdefault("recvWindow", 5000)
        payload["timestamp"] = int(time.time() * 1000)

        # 使用排序后的参数生成签名，并保持发送参数顺序一致
        sorted_items = [(key, payload[key]) for key in sorted(payload)]
        query = "&".join(f"{key}={value}" for key, value in sorted_items)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        ordered_payload = {key: value for key, value in sorted_items}
        ordered_payload["signature"] = signature
        return ordered_payload

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        allow_error: bool = False
    ) -> requests.Response:
        query_params = self._signed_params(params) if signed else dict(params or {})
        response = requests.request(
            method,
            url,
            params=query_params,
            headers=self._headers(),
            timeout=self.timeout
        )
        if not allow_error:
            response.raise_for_status()
        return response

    def _detect_api_capability(self) -> ApiCapability:
        """
        检测API Key的权限能力
        - STANDARD: 标准期货API Key，可以访问FAPI
        - PAPI_ONLY: 仅PAPI权限，专门用于统一保证金账户

        注意：PAPI模式已支持，所有下单将走PAPI-UM接口
        """
        try:
            url = f"{self.FAPI_BASE}/fapi/v2/account"
            response = self.request("GET", url, signed=True, allow_error=True)
            if response.status_code == 401:
                # 401 表示无权限访问 FAPI，说明是 PAPI Key（统一保证金账户）
                print("[检测] API检测: 当前Key是PAPI_ONLY（统一保证金账户）")
                return ApiCapability.PAPI_ONLY
            elif response.status_code == 200:
                # 正常访问FAPI，是标准期货Key
                print("[检测] API检测: 当前Key是STANDARD（完整FAPI权限）")
                return ApiCapability.STANDARD
            else:
                # 其他状态码可能是限流或服务问题，不能判断为PAPI_ONLY
                print(f"[检测] API检测: FAPI返回非401/200状态码 {response.status_code}，暂时认为是STANDARD")
                return ApiCapability.STANDARD
        except requests.RequestException as e:
            # 网络异常不能作为判断PAPI-only的依据
            print(f"[检测] API检测: 网络异常 {e}，暂时认为是STANDARD")
            return ApiCapability.STANDARD

    def _detect_account_mode(self) -> AccountMode:
        try:
            url = f"{self.PAPI_BASE}/papi/v1/um/account"
            response = self.request("GET", url, signed=True, allow_error=True)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    unwrap_keys = (
                        "data",
                        "account",
                        "accountInfo",
                        "futureAccountResp",
                        "umAccountResp",
                        "umAccount",
                        "umAccountInfo"
                    )
                    for key in unwrap_keys:
                        inner = data.get(key)
                        if isinstance(inner, dict):
                            data = inner
                            break

                    status = data.get("accountStatus")
                    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
                    unified_markers = (
                        data.get("totalWalletBalance"),
                        data.get("totalMarginBalance"),
                        data.get("accountEquity"),
                        data.get("equity")
                    )
                    if (status and status != "UNKNOWN") or assets or any(v for v in unified_markers if v is not None):
                        return AccountMode.UNIFIED
        except requests.RequestException:
            pass
        return AccountMode.CLASSIC

    def um_base(self) -> str:
        if self.capability == ApiCapability.PAPI_ONLY or self.account_mode == AccountMode.UNIFIED:
            return self.PAPI_BASE
        return self.FAPI_BASE


class OrderGateway:
    def __init__(self, broker: BinanceBroker) -> None:
        self.broker = broker
        self._hedge_mode_cache: Optional[bool] = None

    def _get_hedge_mode(self) -> bool:
        """
        查询当前是否为双向持仓模式（Hedge Mode）

        Returns:
            True=双向持仓（Hedge Mode）, False=单向持仓
        """
        if self._hedge_mode_cache is not None:
            return self._hedge_mode_cache

        try:
            url = f"{self.broker.PAPI_BASE}/papi/v1/um/positionSide/dual"
            response = self.broker.request("GET", url, signed=True, allow_error=True)
            if response.status_code == 200:
                data = response.json()
                # Binance 返回 {"dualSidePosition": true/false}
                self._hedge_mode_cache = data.get("dualSidePosition", False)
                return self._hedge_mode_cache
        except Exception:
            # 异常情况默认假设为单向持仓（安全）
            self._hedge_mode_cache = False
            return self._hedge_mode_cache

        # 兜底返回（理论上不会走到这里）
        return False

    def _position_side(self, side: str, reduce_only: bool) -> str:
        """
        根据账户模式和操作返回正确的 positionSide

        Args:
            side: BUY 或 SELL
            reduce_only: 是否为平仓操作

        Returns:
            positionSide 值（BOTH, LONG, 或 SHORT）
        """
        # 如果不是统一账户，使用 BOTH（单向持仓）
        if self.broker.account_mode != AccountMode.UNIFIED:
            return "BOTH"

        # 统一账户下，检查是否为双向持仓模式
        is_hedge = self._get_hedge_mode()
        if not is_hedge:
            return "BOTH"

        # 双向持仓模式下，根据 side 和 reduce_only 决定
        side = side.upper()
        if side == "BUY":
            # 买入时：开仓=LONG，平空=SHORT
            return "SHORT" if reduce_only else "LONG"
        else:  # SELL
            # 卖出时：平多=LONG，开空=SHORT
            return "LONG" if reduce_only else "SHORT"

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "MARKET",
        reduce_only: bool = False,
        **extra: Any
    ) -> Dict[str, Any]:
        """
        PAPI Unified Margin 下单（自动适配持仓模式）

        Args:
            symbol: 交易对
            side: BUY 或 SELL
            quantity: 数量
            order_type: 订单类型（默认MARKET）
            reduce_only: 是否为平仓操作
            **extra: 额外参数
        """
        position_side = self._position_side(side, reduce_only)

        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type,
            "quantity": quantity,

            # PAPI 必须显式声明
            "reduceOnly": "true" if reduce_only else "false",

            # 自动适配持仓模式
            "positionSide": position_side,
        }

        # 允许额外参数（如 timeInForce 等）
        params.update(extra)

        url = f"{self.broker.PAPI_BASE}/papi/v1/um/order"

        response = self.broker.request(
            "POST",
            url,
            params=params,
            signed=True
        )

        return response.json()


class PositionGateway:
    def __init__(self, broker: BinanceBroker) -> None:
        self.broker = broker

    def get_positions(self) -> List[Dict[str, Any]]:
        base = self.broker.um_base()
        if base == self.broker.PAPI_BASE:
            url = f"{base}/papi/v1/um/positionRisk"
        else:
            url = f"{base}/fapi/v2/positionRisk"
        response = self.broker.request("GET", url, signed=True)
        positions = response.json()
        return [p for p in positions if float(p.get("positionAmt", 0)) != 0]

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        for pos in self.get_positions():
            if pos.get("symbol") == symbol:
                return pos
        return None


class BalanceEngine:
    def __init__(self, broker: BinanceBroker) -> None:
        self.broker = broker
        self._cached_unified_snapshot: Optional[Dict[str, Any]] = None

    def _papi_unified_account(self) -> Dict[str, Any]:
        """
        统一账户优先使用 /papi/v1/account
        若不可用则回退 /papi/v1/um/account
        """
        url = f"{self.broker.PAPI_BASE}/papi/v1/account"
        response = self.broker.request("GET", url, signed=True, allow_error=True)
        if response.status_code == 200:
            return response.json()

        url = f"{self.broker.PAPI_BASE}/papi/v1/um/account"
        response = self.broker.request("GET", url, signed=True)
        return response.json()

    def get_balance(self) -> Dict[str, Any]:
        if self.broker.account_mode == AccountMode.UNIFIED:
            balance = self._unified_balance()
            if self.broker.capability == ApiCapability.PAPI_ONLY:
                balance["note"] = self._papi_only_message()
            return balance

        if self.broker.capability == ApiCapability.PAPI_ONLY:
            unified = self._try_papi_unified_balance()
            if unified is not None:
                unified["mode"] = "PAPI_FALLBACK"
                unified["note"] = self._papi_only_message()
                return unified

        if self.broker.capability == ApiCapability.STANDARD:
            balance = self._classic_um_balance()
        else:
            balance = self._classic_shadow_balance()

        if self.broker.capability == ApiCapability.PAPI_ONLY:
            balance["note"] = self._papi_only_message()
        return balance

    def _unified_balance(self) -> Dict[str, Any]:
        data = self._papi_unified_account()

        # 兼容各种包装结构（部分返回会套壳 data / futureAccountResp 等）
        if isinstance(data, dict):
            unwrap_keys = (
                "data",
                "account",
                "accountInfo",
                "futureAccountResp",
                "umAccountResp",
                "umAccount",
                "umAccountInfo"
            )
            for key in unwrap_keys:
                inner = data.get(key)
                if isinstance(inner, dict):
                    data = inner
                    break

        assets_candidates = []
        if isinstance(data, dict):
            for key in ("assets", "balances", "crossMarginAssetVoList", "assetList"):
                if isinstance(data.get(key), list) and data.get(key):
                    assets_candidates = data.get(key)
                    break
        assets = assets_candidates if isinstance(assets_candidates, list) else []

        # 资产级回退：有些账户不会返回顶层合计字段，需从 assets 聚合
        assets_total_wallet = sum(
            float(
                a.get("walletBalance")
                or a.get("crossWalletBalance")
                or a.get("balance")
                or 0
            )
            for a in assets
        )
        assets_total_available = sum(
            float(
                a.get("availableBalance")
                or a.get("available")
                or a.get("free")
                or a.get("crossWalletBalance")
                or 0
            )
            for a in assets
        )
        assets_total_unrealized = sum(
            float(
                a.get("unrealizedProfit")
                or a.get("crossUnPnl")
                or a.get("unRealizedProfit")
                or 0
            )
            for a in assets
        )
        assets_equity = assets_total_wallet + assets_total_unrealized

        top_equity = float(
            (isinstance(data, dict) and (
                data.get("accountEquity")
                or data.get("equity")
                or data.get("marginBalance")
                or data.get("totalMarginBalance")
            ))
            or 0
        )
        top_available = float(
            (isinstance(data, dict) and (
                data.get("availableBalance")
                or data.get("available")
                or data.get("availableForTrade")
                or data.get("maxWithdrawAmount")
            ))
            or 0
        )
        top_wallet = float(
            (isinstance(data, dict) and (
                data.get("totalWalletBalance")
                or data.get("walletBalance")
                or data.get("marginBalance")
                or data.get("totalCrossWalletBalance")
                or data.get("totalMarginBalance")
                or data.get("accountEquity")
            ))
            or 0
        )
        top_margin_balance = float(
            (isinstance(data, dict) and (
                data.get("totalMarginBalance")
                or data.get("marginBalance")
                or data.get("accountEquity")
            ))
            or 0
        )

        equity = top_equity or assets_equity
        available = top_available or assets_total_available
        total_wallet = top_wallet or assets_total_wallet
        total_margin_balance = top_margin_balance or (assets_total_wallet + assets_total_unrealized)

        # SPOT 备选方案：当 PAPI 的可用保证金为负时，使用 SPOT 余额
        papi_available = available
        if papi_available <= 0:
            try:
                # 先尝试全仓杠杆账户
                margin_url = f"{self.broker.SPOT_BASE}/sapi/v1/margin/account"
                margin_response = self.broker.request("GET", margin_url, signed=True)
                margin_data = margin_response.json()
                for asset in margin_data.get("userAssets", []):
                    if asset.get("asset") == "USDT":
                        margin_usdt = float(asset.get("free", 0)) + float(asset.get("locked", 0))
                        if margin_usdt > 0:
                            available = max(papi_available, margin_usdt)
                            break
                
                # 如果全仓杠杆也没有，尝试现货
                if available <= 0:
                    spot_breakdown = self._spot_balance_breakdown()
                    spot_available = spot_breakdown.get("usdt", 0.0)
                    available = max(available, spot_available)
            except Exception:
                pass

        return {
            "equity": equity,
            "available": available,
            "status": data.get("accountStatus"),
            "totalWalletBalance": total_wallet,
            "totalMarginBalance": total_margin_balance,
            "totalInitialMargin": float(data.get("totalInitialMargin", 0) or 0),
            "totalMaintMargin": float(data.get("totalMaintMargin", 0) or 0),
            "totalUnrealizedProfit": float(data.get("totalUnrealizedProfit", 0) or assets_total_unrealized or 0),
            "assets": assets,
            "raw": data,
            "papi_available_before_fallback": papi_available
        }

    def _try_papi_unified_balance(self) -> Optional[Dict[str, Any]]:
        try:
            data = self._unified_balance()
        except Exception:
            return None

        equity = float(data.get("equity", 0) or 0)
        available = float(data.get("available", 0) or 0)
        status = data.get("status")
        total_wallet = float(data.get("totalWalletBalance", 0) or 0)
        total_margin = float(data.get("totalMarginBalance", 0) or 0)
        assets = data.get("assets") if isinstance(data, dict) else None
        has_assets = isinstance(assets, list) and len(assets) > 0

        if (
            equity > 0
            or available > 0
            or total_wallet > 0
            or total_margin > 0
            or has_assets
            or status in {"NORMAL", "MARGIN_CALL"}
        ):
            return data

        return None

    def _classic_shadow_balance(self) -> Dict[str, Any]:
        spot_breakdown = self._spot_balance_breakdown()
        spot_usdt = spot_breakdown.get("usdt", 0.0)
        spot_ldusdt = spot_breakdown.get("ldusdt", 0.0)
        spot_total = spot_breakdown.get("total", spot_usdt)
        positions = self.broker.position.get_positions()
        used_margin = 0.0
        unrealized = 0.0
        for pos in positions:
            amt = abs(float(pos.get("positionAmt", 0)))
            price = float(pos.get("entryPrice", 0)) if pos.get("entryPrice") else 0.0
            leverage = max(1.0, float(pos.get("leverage", 1)))
            used_margin += amt * price / leverage
            unrealized += float(pos.get("unRealizedProfit", 0))

        available_balance = max(0.0, spot_usdt - used_margin)

        snapshot = self._cached_unified_snapshot
        if snapshot is None:
            try:
                snapshot = self._unified_balance()
            except Exception:
                snapshot = None

        assets = snapshot.get("assets") if isinstance(snapshot, dict) else []
        asset_wallet = 0.0
        asset_available = 0.0
        if isinstance(assets, list) and assets:
            asset_wallet = sum(
                float(
                    a.get("walletBalance")
                    or a.get("crossWalletBalance")
                    or a.get("balance")
                    or 0
                )
                for a in assets
            )
            asset_available = sum(
                float(
                    a.get("availableBalance")
                    or a.get("available")
                    or a.get("free")
                    or a.get("crossWalletBalance")
                    or 0
                )
                for a in assets
            )

        total_wallet_candidate = max(spot_usdt, asset_wallet, 0.0)
        available_candidate = max(available_balance, asset_available, 0.0)
        equity = total_wallet_candidate + unrealized

        return {
            "totalWalletBalance": total_wallet_candidate,
            "walletBalance": total_wallet_candidate,
            "availableBalance": available_candidate,
            "usedMargin": used_margin,
            "totalInitialMargin": sum(float(pos.get("isolatedMargin", 0)) for pos in positions) if positions else 0.0,
            "totalUnrealizedProfit": unrealized,
            "equity": equity,
            "riskAvailable": max(0.0, total_wallet_candidate + unrealized - used_margin),
            "mode": "SHADOW",
            "spotUsdtBalance": spot_usdt,
            "spotLdUsdtBalance": spot_ldusdt,
            "spotTotalBalance": spot_total,
            "assets": assets,
            "raw": snapshot.get("raw") if isinstance(snapshot, dict) else None,
            "accountStatus": snapshot.get("status") if isinstance(snapshot, dict) else None
        }

    def _classic_um_balance(self) -> Dict[str, Any]:
        url = f"{self.broker.FAPI_BASE}/fapi/v2/account"
        try:
            response = self.broker.request("GET", url, signed=True, allow_error=True)
            if response.status_code == 401:
                return self._classic_shadow_balance()
            response.raise_for_status()
            data = response.json()
            return {
                "totalWalletBalance": float(data.get("totalWalletBalance", 0)),
                "availableBalance": float(data.get("availableBalance", 0)),
                "totalMarginBalance": float(data.get("totalMarginBalance", 0)),
                "totalInitialMargin": float(data.get("totalInitialMargin", 0)),
                "totalMaintMargin": float(data.get("totalMaintMargin", 0)),
                "totalUnrealizedProfit": float(data.get("totalUnrealizedProfit", 0)),
                "equity": float(data.get("totalMarginBalance", 0)) or float(data.get("totalWalletBalance", 0))
            }
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                return self._classic_shadow_balance()
            raise
    def _papi_only_message(self) -> str:
        return (
            "当前 API Key 只具备 PAPI(统一账户) 权限，无法直接调用标准 FAPI 认证接口。"
            " 若需访问合约挂单/下单，请登录币安控制台启用 USDT-M 权限，"
            "并确保 API Key 所在 IP 已加入白名单。"
            " (检测: account_mode={mode}, api_capability={cap})"
        ).format(
            mode=self.broker.account_mode.value,
            cap=self.broker.capability.value
        )

    def _spot_balance_breakdown(self) -> Dict[str, float]:
        url = f"{self.broker.SPOT_BASE}/api/v3/account"
        response = self.broker.request("GET", url, signed=True)
        usdt = 0.0
        ldusdt = 0.0
        for asset in response.json().get("balances", []):
            symbol = asset.get("asset")
            total = float(asset.get("free", 0)) + float(asset.get("locked", 0))
            if symbol == "USDT":
                usdt = total
            elif symbol == "LDUSDT":
                ldusdt = total
        return {
            "usdt": usdt,
            "ldusdt": ldusdt,
            "total": usdt + ldusdt
        }

    def _spot_usdt(self) -> float:
        return self._spot_balance_breakdown().get("usdt", 0.0)


class BinanceClient:
    """Binance API客户端封装"""
    
    def __init__(self, api_key: Optional[str] = None, 
                 api_secret: Optional[str] = None, timeout: int = 30):
        """
        初始化Binance客户端（Broker架构）
        """
        resolved_api_key = api_key or os.getenv('BINANCE_API_KEY')
        resolved_api_secret = api_secret or os.getenv('BINANCE_SECRET')

        if not resolved_api_key:
            raise ValueError('需要提供 BINANCE_API_KEY（环境变量或参数）')
        if not resolved_api_secret:
            raise ValueError('需要提供 BINANCE_SECRET（环境变量或参数）')

        self.api_key: str = resolved_api_key
        self.api_secret: str = resolved_api_secret
        self.timeout = timeout
        self.broker = BinanceBroker(self.api_key, self.api_secret, timeout=timeout)
        self.order = self.broker.order
        self.position = self.broker.position
        self.balance_engine = self.broker.balance
        self._symbol_info_cache: Dict[str, Dict[str, Any]] = {}
        print(f"[连接] 连接到币安正式网 (PAPI统一保证金模式)")
        print(f"[成功] 模式: {self.broker.account_mode.value} / 能力: {self.broker.capability.value}")

    def _um_endpoint(self, fapi_path: str, papi_path: str) -> str:
        base = self.broker.um_base()
        if base == self.broker.PAPI_BASE:
            return f"{base}{papi_path}"
        return f"{base}{fapi_path}"
    
    # 由 Broker 提供共享请求方法，不再单独实现
    
    # ==================== 市场数据 ====================
    
    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> List[List[Any]]:
        url = f"{self.broker.FAPI_BASE}/fapi/v1/klines"
        params: Dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        try:
            response = self.broker.request("GET", url, params=params)
            return response.json()
        except Exception as e:
            print(f"⚠️ 获取K线失败 {symbol} {interval}: {e}")
            return []
    
    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"{self.broker.FAPI_BASE}/fapi/v1/ticker/24hr"
        try:
            response = self.broker.request("GET", url, params={"symbol": symbol})
            return response.json()
        except Exception as e:
            print(f"⚠️ 获取行情失败 {symbol}: {e}")
            return None

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        url = f"{self.broker.FAPI_BASE}/fapi/v1/fundingRate"
        try:
            response = self.broker.request("GET", url, params={"symbol": symbol, "limit": 1})
            data = response.json()
            if data:
                rate = data[0].get('fundingRate') or data[0].get('rate')
                return float(rate) if rate is not None else None
        except Exception as e:
            print(f"⚠️ 获取资金费率失败 {symbol}: {e}")
        return None

    def get_open_interest(self, symbol: str) -> Optional[float]:
        url = f"{self.broker.FAPI_BASE}/fapi/v1/openInterest"
        try:
            response = self.broker.request("GET", url, params={"symbol": symbol})
            data = response.json()
            return float(data.get('openInterest', 0)) if data else None
        except Exception as e:
            print(f"⚠️ 获取持仓量失败 {symbol}: {e}")
            return None
    
    # ==================== 账户和持仓数据 ====================
    
    def get_account(self) -> Dict[str, Any]:
        return self.balance_engine.get_balance()

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.position.get_position(symbol)

    def get_all_positions(self) -> List[Dict[str, Any]]:
        return self.position.get_positions()
    
    # ==================== 交易操作 ====================
    
    def create_market_order(self, symbol: str, side: str, quantity: float, **kwargs) -> Dict[str, Any]:
        """
        创建市价单（开仓或平仓）（参照 DS3.py 的成功方法）
        
        Args:
            symbol: 交易对
            side: 买卖方向 'BUY' 或 'SELL'
            quantity: 数量
            **kwargs: 其他参数
            
        Returns:
            订单信息
        """
        return self.order.place_order(symbol, side, quantity, **kwargs)
    
    def create_limit_order(self, symbol: str, side: str, quantity: float,
                          price: float, **kwargs) -> Dict[str, Any]:
        """
        创建限价单（PAPI Unified Margin，自动适配持仓模式）

        Args:
            symbol: 交易对
            side: 买卖方向
            quantity: 数量
            price: 价格
            **kwargs: 其他参数（如 reduce_only=True 等）

        Returns:
            订单信息
        """
        reduce_only = kwargs.get("reduce_only", False)
        position_side = self.order._position_side(side, reduce_only)

        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price,
            # PAPI 必须显式声明
            "reduceOnly": "true" if reduce_only else "false",
            # 自动适配持仓模式
            "positionSide": position_side,
        }
        # 移除 reduce_only，避免作为额外参数传递
        kwargs.pop("reduce_only", None)
        params.update(kwargs)
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/order"
        response = self.broker.request("POST", url, params=params, signed=True)
        return response.json()
    
    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/order"
        params = {"symbol": symbol, "orderId": order_id}
        response = self.broker.request("DELETE", url, params=params, signed=True)
        return response.json()

    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/allOpenOrders"
        params = {"symbol": symbol}
        response = self.broker.request("DELETE", url, params=params, signed=True)
        return response.json()
    
    # ==================== 仓位管理 ====================
    
    def change_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        修改杠杆倍数
        
        Args:
            symbol: 交易对
            leverage: 杠杆倍数（1-100）
            
        Returns:
            修改结果
        """
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/leverage"
        params = {"symbol": symbol, "leverage": leverage}
        response = self.broker.request("POST", url, params=params, signed=True)
        return response.json()

    def change_margin_type(self, symbol: str, margin_type: str = 'ISOLATED') -> Dict[str, Any]:
        """
        修改保证金类型

        Args:
            symbol: 交易对
            margin_type: 'ISOLATED'(逐仓) 或 'CROSSED'(全仓)
        """
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/marginType"
        params = {"symbol": symbol, "marginType": margin_type.upper()}
        response = self.broker.request("POST", url, params=params, signed=True)
        return response.json()

    def set_hedge_mode(self, enabled: bool = True):
        """
        设置持仓模式（双向持仓）

        Args:
            enabled: True=启用双向持仓, False=单向持仓
        """
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/positionSide/dual"
        params = {"dualSidePosition": "true" if enabled else "false"}
        response = self.broker.request("POST", url, params=params, signed=True)
        return response.json()
    
    # ==================== 止盈止损 ====================
    
    def set_take_profit_stop_loss(self, symbol: str, side: str, quantity: float,
                                   take_profit_price: Optional[float] = None,
                                   stop_loss_price: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        设置止盈止损（PAPI Unified Margin，自动适配持仓模式）

        注意：币安期货的止盈止损是通过特殊订单类型实现的
        当closePosition=True时，quantity参数不会被使用

        Args:
            symbol: 交易对
            side: 原开仓方向 'BUY' 或 'SELL'（用于双向持仓模式判断）
            quantity: 数量（当closePosition=True时不会被使用，但为保持接口一致性而保留）
            take_profit_price: 止盈价
            stop_loss_price: 止损价

        Returns:
            创建的订单列表
        """
        # quantity参数在closePosition=True时不会被使用
        # 这里使用下划线表示故意不使用该参数
        _ = quantity
        orders = []
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/order"

        # 对于止盈止损，需要确定正确的 positionSide
        # 止盈止损总是平仓操作（reduce_only=True）
        position_side = self.order._position_side(side, reduce_only=True)

        if take_profit_price is not None:
            # 止盈是平仓操作，方向与原开仓方向相反
            order_side = "SELL" if side == "BUY" else "BUY"
            params = {
                "symbol": symbol,
                "side": order_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": take_profit_price,
                "closePosition": True,
                # PAPI 必须显式声明
                "reduceOnly": "true",
                # 自动适配持仓模式（双向模式下为LONG/SHORT，单向为BOTH）
                "positionSide": position_side,
            }
            response = self.broker.request("POST", url, params=params, signed=True)
            orders.append(response.json())
        if stop_loss_price is not None:
            # 止损是平仓操作，方向与原开仓方向相反
            order_side = "SELL" if side == "BUY" else "BUY"
            params = {
                "symbol": symbol,
                "side": order_side,
                "type": "STOP_MARKET",
                "stopPrice": stop_loss_price,
                "closePosition": True,
                # PAPI 必须显式声明
                "reduceOnly": "true",
                # 自动适配持仓模式（双向模式下为LONG/SHORT，单向为BOTH）
                "positionSide": position_side,
            }
            response = self.broker.request("POST", url, params=params, signed=True)
            orders.append(response.json())
        return orders
    
    # ==================== 查询订单 ====================

    def get_order(self, symbol: str, order_id: int) -> Optional[Dict[str, Any]]:
        """查询订单"""
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/order"
        try:
            response = self.broker.request("GET", url, params={"symbol": symbol, "orderId": order_id}, signed=True)
            return response.json()
        except Exception as e:
            print(f"[警告] 查询订单失败 {symbol} {order_id}: {e}")
            return None

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有挂单"""
        url = f"{self.broker.PAPI_BASE}/papi/v1/um/openOrders"
        try:
            params = {"symbol": symbol} if symbol else {}
            response = self.broker.request("GET", url, params=params, signed=True)
            return response.json()
        except Exception as e:
            print(f"[警告] 获取挂单失败: {e}")
            return []
    
    # ==================== 工具方法 ====================
    
    def get_exchange_info(self) -> Optional[Dict[str, Any]]:
        """
        获取交易所信息（包含交易对精度）
        
        Returns:
            交易所信息字典
        """
        url = f"{self.broker.FAPI_BASE}/fapi/v1/exchangeInfo"
        try:
            response = self.broker.request("GET", url)
            return response.json()
        except Exception as e:
            print(f"⚠️ 获取交易所信息失败: {e}")
            return None
    
    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取指定交易对的信息（包含精度）
        
        Args:
            symbol: 交易对，如 'BTCUSDT'
            
        Returns:
            交易对信息字典，包含 stepSize（数量精度）和 tickSize（价格精度）
        """
        try:
            if symbol in self._symbol_info_cache:
                return self._symbol_info_cache[symbol]
            info = self.get_exchange_info()
            if not info:
                return None
            
            for s in info.get('symbols', []):
                if s['symbol'] == symbol:
                    # 提取数量精度（stepSize）
                    quantity_precision = None
                    price_precision = None
                    step_size = None
                    tick_size = None
                    min_notional = None
                    
                    for f in s.get('filters', []):
                        if f['filterType'] == 'LOT_SIZE':
                            step_size = float(f['stepSize'])
                            # 计算小数位数
                            if step_size >= 1:
                                quantity_precision = 0
                            else:
                                # 计算stepSize的小数位数
                                step_str = str(step_size).rstrip('0')
                                if '.' in step_str:
                                    quantity_precision = len(step_str.split('.')[-1])
                                else:
                                    quantity_precision = 0
                        elif f['filterType'] == 'PRICE_FILTER':
                            tick_size = float(f['tickSize'])
                            if tick_size >= 1:
                                price_precision = 0
                            else:
                                # 计算tickSize的小数位数
                                tick_str = str(tick_size).rstrip('0')
                                if '.' in tick_str:
                                    price_precision = len(tick_str.split('.')[-1])
                                else:
                                    price_precision = 0
                        elif f['filterType'] == 'MIN_NOTIONAL':
                            min_notional = float(f.get('minNotional', f.get('min_notional') or 0) or 0)
                        elif f['filterType'] == 'NOTIONAL':
                            min_notional = float(f.get('notional') or 0)
                    
                    symbol_info = {
                        'symbol': symbol,
                        'quantity_precision': quantity_precision,
                        'price_precision': price_precision,
                        'step_size': step_size,
                        'tick_size': tick_size,
                        'min_notional': min_notional,
                        'raw': s
                    }
                    self._symbol_info_cache[symbol] = symbol_info
                    return symbol_info
            
            return None
        except Exception as e:
            print(f"⚠️ 获取交易对信息失败 {symbol}: {e}")
            return None
    
    def format_quantity(self, symbol: str, quantity: float) -> float:
        """
        格式化数量到正确的精度
        
        Args:
            symbol: 交易对
            quantity: 原始数量
            
        Returns:
            格式化后的数量
        """
        try:
            symbol_info = self.get_symbol_info(symbol)
            if not symbol_info:
                # 如果获取失败，使用默认精度（3位小数）
                return round(quantity, 3)
            
            step_size = symbol_info.get('step_size')
            if step_size and step_size > 0:
                # 向下取整到 stepSize 的倍数
                quantity = float(int(quantity / step_size) * step_size)
            
            precision = symbol_info.get('quantity_precision')
            if precision is not None:
                # 使用指定精度四舍五入
                formatted = round(quantity, precision)
                # 确保不会因为精度问题导致数量为0
                if formatted <= 0 and quantity > 0:
                    # 如果格式化后为0但原数量>0，使用最小步长
                    if step_size and step_size > 0:
                        formatted = step_size
                    else:
                        formatted = round(quantity, 3)
                return formatted
            else:
                # 默认保留3位小数
                return round(quantity, 3)
        except Exception as e:
            print(f"⚠️ 格式化数量失败 {symbol}: {e}")
            # 失败时返回保留3位小数的值
            return round(quantity, 3)

    def ensure_min_notional_quantity(self, symbol: str, quantity: float, price: float) -> float:
        """确保数量满足最低名义要求"""
        try:
            if quantity <= 0 or price <= 0:
                return quantity
            symbol_info = self.get_symbol_info(symbol)
            if not symbol_info:
                return quantity

            min_notional = symbol_info.get('min_notional')
            if not min_notional or min_notional <= 0:
                return quantity

            current_notional = quantity * price
            if current_notional >= min_notional:
                return quantity

            required_qty = min_notional / price
            step_size = symbol_info.get('step_size')
            if step_size and step_size > 0:
                required_qty = math.ceil(required_qty / step_size) * step_size

            adjusted_quantity = max(quantity, required_qty)
            formatted_quantity = self.format_quantity(symbol, adjusted_quantity)

            if formatted_quantity * price < min_notional and step_size and step_size > 0:
                formatted_quantity += step_size
                formatted_quantity = self.format_quantity(symbol, formatted_quantity)

            if formatted_quantity != quantity:
                print(f"📏 {symbol} 数量调整以满足最小名义 {min_notional:.2f}: {quantity:.8f} -> {formatted_quantity:.8f}")

            return formatted_quantity
        except Exception as e:
            print(f"⚠️ 确保最小名义失败 {symbol}: {e}")
            return quantity
    
    def get_server_time(self) -> Optional[Dict[str, Any]]:
        """获取服务器时间"""
        url = f"{self.broker.FAPI_BASE}/fapi/v1/time"
        try:
            response = self.broker.request("GET", url)
            return response.json()
        except requests.RequestException as e:
            print(f"⚠️ 获取服务器时间失败: {e}")
            return None
    
    def test_connection(self) -> bool:
        """测试连接"""
        try:
            self.get_server_time()
            return True
        except Exception as e:
            print(f"⚠️ 连接测试失败: {e}")
            return False
