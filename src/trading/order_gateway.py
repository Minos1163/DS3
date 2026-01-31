import time
from typing import Any, Dict, List, Optional

import requests  # type: ignore


class OrderGateway:
    """
    负责：Binance 订单指令的格式化、参数映射、终端选择及实际发送。
    """

    def __init__(self, broker: Any) -> None:
        self.broker = broker
        # 🔒 L1: symbol + side 时间锁（20秒内禁止重复 OPEN）
        self._open_locks: Dict[str, float] = {}

    def _is_fatal_auth_error(self, err: Any) -> bool:
        """检测致命权限错误（401 / -2015 / -2014）- 不可重试"""
        if isinstance(err, dict):
            return err.get("code") in (-2015, -2014)
        msg = str(err)
        checks = ["401", "Unauthorized", "-2015", "-2014"]
        return any(s in msg for s in checks)

    def has_open_position(self, symbol: str, side: Optional[str] = None) -> bool:
        """🔥 L2: 统一的「是否已有仓位」判断（支持方向 LONG/SHORT/BOTH 和 BUY/SELL）

        接受的 side 可以是 'LONG'/'SHORT' 或者 'BUY'/'SELL'，也可以为 None (等同于 BOTH)。
        """
        if side:
            s = side.upper()
            if s == "BUY":
                query_side = "LONG"
            elif s == "SELL":
                query_side = "SHORT"
            elif s in ("LONG", "SHORT"):
                query_side = s
            else:
                query_side = "BOTH"
        else:
            query_side = "BOTH"

        pos = self.broker.position.get_position(symbol, side=query_side)
        if not pos:
            return False
        try:
            return abs(float(pos.get("positionAmt", 0))) > 0
        except Exception:
            return False

    def place_standard_order(
        self,
        symbol: str,
        side: str,
        params: Dict[str, Any],
        reduce_only: bool = False,
        delay: int = 20,
    ) -> Dict[str, Any]:
        """
        执行标准订单（开仓、平仓）

        🔒 三层防护机制：
        - L1: 时间锁（同symbol+side 20秒内禁止重复）
        - L2: 真实仓位检查（不是openOrders）
        - L3: 失败后再次检查仓位（防止已成交）
        """
        now = time.time()
        lock_key = f"{symbol}:{side}"

        # 判断是否为全仓平仓（closePosition）——对平仓不应触发开仓锁/开仓检查
        is_close_position = bool(params.get("closePosition"))

        # 🔒 L1: 时间锁（20秒内禁止重复开仓）
        # 仅在非平仓且非 reduce_only 的情况下生效
        if not reduce_only and not is_close_position:
            last_ts = self._open_locks.get(lock_key)
            if last_ts and now - last_ts < delay:
                msg = (
                    "[OPEN BLOCKED] "
                    + symbol
                    + " "
                    + side
                    + " within "
                    + str(delay)
                    + "s lock"
                )
                raise RuntimeError(msg)

        # 计算用于仓位检查的 position side（兼容 BUY/SELL 和 LONG/SHORT）
        s_up = side.upper() if isinstance(side, str) else ""
        if s_up in ("BUY", "LONG"):
            pos_check_side = "LONG"
        elif s_up in ("SELL", "SHORT"):
            pos_check_side = "SHORT"
        else:
            pos_check_side = "BOTH"

        # 🔒 L2: 真实仓位检查（不是openOrders），按方向检查避免重复开仓
        # 对于平仓请求（closePosition）应跳过此检查
        cond_skip_l2 = not reduce_only and not is_close_position
        if cond_skip_l2 and self.has_open_position(symbol, pos_check_side):
            msg = (
                "[OPEN BLOCKED] "
                + symbol
                + " already has open position (real check via positionAmt)"
            )
            raise RuntimeError(msg)

        # 记录锁（先锁，防并发）
        self._open_locks[lock_key] = now

        final = self._finalize_params(params, side, reduce_only)

        try:
            response = self.broker.request(
                method="POST",
                url=self._order_endpoint(),
                params=final,
                signed=True,
            )
            data = response.json()

            # Binance 返回错误
            if "code" in data and data["code"] < 0:
                # 🚫 致命权限错误：直接抛出，禁止 retry
                if self._is_fatal_auth_error(data):
                    msg = (
                        "[FATAL AUTH ERROR] API key has no futures permission "
                        "or invalid IP: " + str(data)
                    )
                    raise RuntimeError(msg)

                # 🚫 -1116 Invalid orderType: 检查仓位（按方向），若已变则直接返回 warning
                if data.get("code") == -1116:
                    pos = self.broker.position.get_position(symbol, side=pos_check_side)
                    if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                        print("[WARN] -1116: position exists")
                        print(data)
                        return {
                            "warning": "order_failed_but_position_exists",
                            "symbol": symbol,
                            "side": side,
                            "error": data,
                            "position_exists": True,
                        }

                # 🔥 L3: 失败后 → 再查一次仓位（防止已成交）
                cond_l3 = not reduce_only and self.has_open_position(
                    symbol, pos_check_side
                )
                if cond_l3:
                    print("[WARN] Order failed but position exists")
                    print(data)
                    # 返回特殊状态，避免上层误判
                    return {
                        "warning": "order_failed_but_position_exists",
                        "symbol": symbol,
                        "side": side,
                        "error": data,
                        "position_exists": True,
                    }
                raise RuntimeError(f"Binance Error: {data}")

            return data

        except Exception as e:
            # 🚫 致命权限错误：直接抛出，禁止 retry
            if self._is_fatal_auth_error(e):
                raise RuntimeError(
                    "[FATAL AUTH ERROR] API key has no futures permission or invalid IP: "
                    + str(e)
                ) from e

            # 🚫 -1116 Invalid orderType: 检查仓位，若已变則直接返回 warning
            if (
                isinstance(e, requests.HTTPError)
                and getattr(e, "response", None) is not None
            ):
                try:
                    err_data = e.response.json()
                    if err_data.get("code") == -1116:
                        pos = self.broker.position.get_position(
                            symbol, side=pos_check_side
                        )
                        if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                            print("[WARN] -1116: position exists")
                            print(err_data)
                            return {
                                "warning": "order_failed_but_position_exists",
                                "symbol": symbol,
                                "side": side,
                                "error": err_data,
                                "position_exists": True,
                            }
                except Exception:
                    pass

            # 🔥 L3: 失败后 → 再查一次仓位（防止已成交）
            cond_l3_exc = not reduce_only and self.has_open_position(
                symbol, pos_check_side
            )
            if cond_l3_exc:
                print("[WARNING] Exception but position exists:")
                print(e)
                return {
                    "warning": "order_failed_but_position_exists",
                    "symbol": symbol,
                    "side": side,
                    "error": str(e),
                    "position_exists": True,
                }
            raise

            # 🔒 不立即释放锁，让delay真正生效
            # 依赖时间戳检查，而不是立即释放
        finally:
            # 🔒 不立即释放锁，让delay真正生效
            # 依赖时间戳检查，而不是立即释放
            pass

    def place_protection_orders(
        self,
        symbol: str,
        side: str,
        tp: Optional[float],
        sl: Optional[float],
    ) -> List[Dict[str, Any]]:
        """执行 TP/SL 止盈止损单"""
        results = []
        # 计算下单方向与仓位方向 (Hedge 模式适配)
        order_side = "SELL" if side.upper() == "LONG" else "BUY"
        pos_side = self.broker.calculate_position_side(order_side, True)

        endpoint = self._order_endpoint()

        for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
            if price:
                # 🔥 PAPI-UM 和 FAPI 都使用 type 字段
                p = {
                    "symbol": symbol,
                    "side": order_side,
                    "type": otype,
                    "stopPrice": price,
                    "closePosition": True,
                }
                if pos_side:
                    p["positionSide"] = pos_side

                res = self.broker.request("POST", endpoint, params=p, signed=True)
                results.append(res.json())

        return results

    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        endpoint = self._order_endpoint()
        params = {"symbol": symbol, "orderId": order_id}
        return self.broker.request(
            "DELETE",
            endpoint,
            params=params,
            signed=True,
        ).json()

    def query_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        # 🔥 统一使用 FAPI 端点
        base = self.broker.FAPI_BASE
        path = "/fapi/v1/openOrders"
        params = {"symbol": symbol} if symbol else {}
        url = f"{base}{path}"
        resp = self.broker.request("GET", url, params=params, signed=True)
        return resp.json()

    # --- 内部协议细节 ---

    def _order_endpoint(self) -> str:
        """
        动态选择订单端点：
        - PAPI-UM: /papi/v1/um/order
        - FAPI: /fapi/v1/order
        """
        if self.broker.is_papi_only():  # 检查是否为 PAPI_ONLY 模式
            base = self.broker.PAPI_BASE  # 使用 PAPI 基础路径
            return f"{base}/papi/v1/um/order"
        base = self.broker.FAPI_BASE  # 使用 FAPI 基础路径
        return f"{base}/fapi/v1/order"

    def _finalize_params(
        self, params: Dict[str, Any], side: str, reduce_only: bool
    ) -> Dict[str, Any]:
        """
        格式化订单参数，兼容 PAPI 实盘：
        - 全仓平仓必须传 closePosition=True + quantity（PAPI 要求）
        - 部分平仓使用 quantity + reduceOnly=True
        - MARKET 单不带 price
        - ONEWAY 模式禁止 positionSide
        - 🔥 PAPI UM 和 FAPI 都使用 'type' 字段（不是 orderType）
        """
        p = dict(params)
        p["side"] = side.upper()
        is_hedge = self.broker.get_hedge_mode()

        # 🔥 PAPI UM 和 FAPI 都使用 type 字段
        if "type" in p:
            p["type"] = p["type"].upper()
        else:
            p["type"] = "MARKET"  # 默认值

        # 删除任何 orderType 字段（PAPI UM 不认这个）
        p.pop("orderType", None)

        # MARKET 不带 price
        if p.get("type") == "MARKET":
            p.pop("price", None)

        if not is_hedge:
            p.pop("positionSide", None)

        # 全仓平仓必须带 quantity
        if (
            p.get("closePosition") is True
            or str(p.get("closePosition")).lower() == "true"
        ):
            p["closePosition"] = True
            print("[DEBUG _finalize_params] Before quantity check:")
            print(p.get("quantity"))
            if "quantity" not in p or not p["quantity"]:
                print("[DEBUG] quantity missing, fetching position")
                pos = self.broker.position.get_position(p.get("symbol"), side="BOTH")
                if pos:
                    p["quantity"] = abs(float(pos.get("positionAmt", 0)))
                    print("[DEBUG _finalize_params] Fetched quantity")
                    print(p["quantity"])
                else:
                    raise ValueError(f"无法获取仓位数量: {p.get('symbol')}")
            else:
                print("[DEBUG _finalize_params] Quantity already present:")
                print(p["quantity"])
            p.pop("reduceOnly", None)
            p.pop("reduce_only", None)
        else:
            # 开仓或部分平仓
            p.pop("closePosition", None)
            if reduce_only:
                p["reduceOnly"] = True
            else:
                p.pop("reduceOnly", None)
            if is_hedge and "positionSide" not in p:
                ps = self.broker.calculate_position_side(side, reduce_only)
                if ps:
                    p["positionSide"] = ps
        return p
