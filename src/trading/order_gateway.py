import requests  # type: ignore

import math

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


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

    def _log_order_reject(
        self,
        symbol: str,
        side: str,
        params: Dict[str, Any],
        error: Any,
    ) -> None:
        """记录订单拒绝告警到日志文件（可选）"""
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            logs_dir = os.path.join(project_root, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            path = os.path.join(logs_dir, "order_rejects.log")
            ts = datetime.now().isoformat()
            line = f"{ts} symbol={symbol} side={side} params={params} error={error}\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

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
                msg = "[OPEN BLOCKED] " + symbol + " " + side + " within " + str(delay) + "s lock"
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
            msg = "[OPEN BLOCKED] " + symbol + " already has open position (real check via positionAmt)"
            raise RuntimeError(msg)

        # 记录锁（先锁，防并发）
        self._open_locks[lock_key] = now

        final = self._finalize_params(params, side, reduce_only)

        # 确保下单满足交易所最小名义(notional)要求，避免 -4164 错误
        try:
            qty = final.get("quantity")
            price = final.get("price")
            if qty and (not price or float(price) <= 0):
                # 尝试从行情获取当前价格
                try:
                    ticker = self.broker.get_ticker(symbol)
                    price = float(ticker.get("lastPrice", 0)) if ticker else None
                except Exception:
                    price = None

            if qty and price and float(price) > 0:
                try:
                    adjusted = self.broker.ensure_min_notional_quantity(symbol, float(qty), float(price))
                    if adjusted != float(qty):
                        # 更新最终参数为符合最小名义量的数量
                        final["quantity"] = adjusted
                        print(f"[INFO] Adjusted quantity for min_notional: {qty} -> {adjusted} (price={price})")
                except Exception:
                    # 容错：如果检查失败，继续按原参数下单（上层会捕获并处理错误）
                    pass
        except Exception:
            pass

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
                # 记录订单拒绝（可选告警日志）
                self._log_order_reject(symbol, side, final, data)

                # 🚫 致命权限错误：直接抛出，禁止 retry
                if self._is_fatal_auth_error(data):
                    msg = "[FATAL AUTH ERROR] API key has no futures permission or invalid IP: " + str(data)
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
                cond_l3 = not reduce_only and self.has_open_position(symbol, pos_check_side)
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
                    "[FATAL AUTH ERROR] API key has no futures permission or invalid IP: " + str(e)
                ) from e

            # 🚫 -1116 Invalid orderType: 检查仓位，若已变則直接返回 warning
            if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
                # 尝试解析交易所返回的 JSON 错误
                try:
                    err_data = e.response.json()
                except Exception:
                    err_data = None

                if err_data:
                    # 记录订单拒绝（可选告警日志）
                    self._log_order_reject(symbol, side, final, err_data)

                    # 处理最小名义额错误（-4164）：尝试读取交易所信息并自动调整一次重试
                    if err_data.get("code") == -4164:
                        try:
                            ex_url = f"{self.broker.MARKET_BASE}/fapi/v1/exchangeInfo"
                            resp = self.broker.request("GET", ex_url, params={"symbol": symbol}, allow_error=True)
                            info = resp.json() if resp is not None else {}
                            min_notional = None
                            step_size = None
                            for s in info.get("symbols", []):
                                if s.get("symbol") == symbol:
                                    for f in s.get("filters", []):
                                        if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                                            try:
                                                min_notional = float(f.get("minNotional") or f.get("notional") or 5.0)
                                            except Exception:
                                                min_notional = 5.0
                                        if f.get("filterType") == "LOT_SIZE":
                                            try:
                                                step_size = float(f.get("stepSize"))
                                            except Exception:
                                                step_size = None
                                    break

                            price = final.get("price")
                            if not price:
                                try:
                                    t = self.broker.request(
                                        "GET",
                                        f"{self.broker.MARKET_BASE}/fapi/v1/ticker/24hr",
                                        params={"symbol": symbol},
                                        allow_error=True,
                                    )
                                    price = float(t.json().get("lastPrice", 0)) if t is not None else None
                                except Exception:
                                    price = None

                            if min_notional and price and price > 0:
                                required_qty = min_notional / float(price)
                                if step_size and step_size > 0:
                                    required_qty = math.ceil(required_qty / step_size) * step_size
                                required_qty = round(required_qty, 8)
                                print(
                                    f"❗ -4164 最小名义额限制: symbol={symbol} min_notional={min_notional} price={price} -> required_qty~={required_qty}"
                                )

                                # 尝试用调整后的数量重试一次下单（仅一次）
                                try:
                                    final_retry = dict(final)
                                    final_retry["quantity"] = required_qty
                                    print(f"🔁 尝试 -4164 自动重试: quantity -> {required_qty}")
                                    resp2 = self.broker.request(
                                        method="POST",
                                        url=self._order_endpoint(),
                                        params=final_retry,
                                        signed=True,
                                    )
                                    data2 = resp2.json()
                                    if "code" in data2 and data2["code"] < 0:
                                        # 仍然失败：记录并继续按原逻辑抛出
                                        self._log_order_reject(symbol, side, final_retry, data2)
                                    else:
                                        return data2
                                except Exception as retry_exc:
                                    try:
                                        self._log_order_reject(symbol, side, final_retry, str(retry_exc))
                                    except Exception:
                                        pass

                        except Exception:
                            # 容错：读取 exchangeInfo / 价格 或 计算过程中出错，放弃自动重试路径
                            pass

                    # 处理 -1116（Invalid orderType）: 若交易所已有仓位，则返回 warning
                    if err_data.get("code") == -1116:
                        pos = self.broker.position.get_position(symbol, side=pos_check_side)
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
                # else: 无法解析 err_data，继续后续处理
            else:
                # 非 HTTPError 场景也记录一次
                self._log_order_reject(symbol, side, final, str(e))

            # 🔥 L3: 失败后 → 再查一次仓位（防止已成交）
            cond_l3_exc = not reduce_only and self.has_open_position(symbol, pos_check_side)
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
        # end of place_standard_order

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
        # 🔥 根据账户类型选择端点
        base = self.broker.um_base()
        if "papi" in base:
            path = "/papi/v1/um/openOrders"
        else:
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

    def place_protection_orders(
        self, symbol: str, side: str, tp: Optional[float], sl: Optional[float]
    ) -> List[Dict[str, Any]]:
        """
        下发止盈/止损保护单（MARKET 型触发单），用于在开仓后快速下保护单。

        返回包含每个创建订单的响应 JSON 列表。
        """
        results: List[Dict[str, Any]] = []
        # 计算下单方向：如果仓位方向为 LONG，则保护单为卖出 (SELL)，反之为 BUY
        order_side = "SELL" if str(side).upper() == "LONG" else "BUY"
        # 计算 positionSide（Hedge 模式适配）
        try:
            pos_side = self.broker.calculate_position_side(order_side, True)
        except Exception:
            pos_side = None

        endpoint = self._order_endpoint()

        for price, otype in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
            if price is None:
                continue
            # 🔥 PAPI-UM 和 FAPI 都使用 type 字段
            p: Dict[str, Any] = {
                "symbol": symbol,
                "side": order_side,
                "type": otype,
                "stopPrice": price,
                "closePosition": True,
            }
            if pos_side:
                p["positionSide"] = pos_side

            try:
                resp = self.broker.request("POST", endpoint, params=p, signed=True)
                results.append(resp.json())
            except Exception as e:
                # 记录并继续尝试下一个保护单
                try:
                    self._log_order_reject(symbol, order_side, p, str(e))
                except Exception:
                    pass

        return results

    def _finalize_params(self, params: Dict[str, Any], side: str, reduce_only: bool) -> Dict[str, Any]:
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
        if p.get("closePosition") is True or str(p.get("closePosition")).lower() == "true":
            p["closePosition"] = True
            if "quantity" not in p or not p["quantity"]:
                pos = self.broker.position.get_position(p.get("symbol"), side="BOTH")
                if pos:
                    p["quantity"] = abs(float(pos.get("positionAmt", 0)))
                else:
                    raise ValueError(f"无法获取仓位数量: {p.get('symbol')}")
            p.pop("reduceOnly", None)
            p.pop("reduce_only", None)
        else:
            # 开仓或部分平仓
            p.pop("closePosition", None)
            if reduce_only:
                # 对于 PAPI（或统一保证金）端点，部分平仓不要发送 reduceOnly（Binance 会拒绝）
                try:
                    if self.broker.is_papi_only():
                        p.pop("reduceOnly", None)
                    else:
                        p["reduceOnly"] = True
                except Exception:
                    # 若检查失败，保守行为：不删除已有字段，仍尝试设置
                    p["reduceOnly"] = True
            else:
                p.pop("reduceOnly", None)
            if is_hedge and "positionSide" not in p:
                ps = self.broker.calculate_position_side(side, reduce_only)
                if ps:
                    p["positionSide"] = ps
        return p
