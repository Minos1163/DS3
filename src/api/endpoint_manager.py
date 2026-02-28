"""
Binance 端口管理和安全平仓工具

⚠️ 关键修复：
  - papi.binance.com 是账户接口，不能用于下单/平仓
  - futures 订单必须用 fapi.binance.com
  - 平仓单必须加 reduceOnly=true 防止反向开仓
"""

from enum import Enum
from typing import Any, Dict, Literal, Optional


class BinanceEndpoint(Enum):
    """Binance API 端点枚举"""

    # 现货交易
    SPOT = "https://api.binance.com"

    # U本位合约 (USDT-M Futures) ← SOLUSDT 就是这个
    FUTURES_USDT = "https://fapi.binance.com"

    # 币本位合约 (Coin-M Futures)
    FUTURES_COIN = "https://dapi.binance.com"

    # Portfolio Margin / 统一账户 (仅用于账户信息，不能下单)
    PAPI_ACCOUNT_ONLY = "https://papi.binance.com"


class EndpointRouter:
    """智能路由器：自动选择正确的端点"""

    @staticmethod
    def get_endpoint_for_order(symbol: str, is_spot: bool = False) -> str:
        """
        获取下单端点

        Args:
            symbol: 交易对 (如 SOLUSDT)
            is_spot: 是否是现货

        Returns:
            正确的端点 URL
        """
        if is_spot:
            return BinanceEndpoint.SPOT.value

        # 判断是否是币本位合约 (如 BTCUSD_PERP)
        if "USD_PERP" in symbol or "PERP" in symbol:
            return BinanceEndpoint.FUTURES_COIN.value

        # 默认使用 U本位合约 (如 SOLUSDT)
        return BinanceEndpoint.FUTURES_USDT.value

    @staticmethod
    def get_path_for_order(endpoint_url: str, order_type: str = "market") -> str:
        """获取下单路径"""
        if endpoint_url == BinanceEndpoint.SPOT.value:
            return "/api/v3/order"
        elif endpoint_url in [
            BinanceEndpoint.FUTURES_USDT.value,
            BinanceEndpoint.FUTURES_COIN.value,
        ]:
            return "/fapi/v1/order"
        else:
            raise ValueError(f"❌ 不支持的端点用于下单: {endpoint_url}")

    @staticmethod
    def validate_endpoint_for_order(endpoint_url: str) -> bool:
        """验证端点是否可用于下单"""
        allowed_endpoints = [
            BinanceEndpoint.SPOT.value,
            BinanceEndpoint.FUTURES_USDT.value,
            BinanceEndpoint.FUTURES_COIN.value,
        ]

        if endpoint_url not in allowed_endpoints:
            return False

        return True


class SafeClosePosition:
    """安全平仓执行器"""

    def __init__(self, client):
        """初始化

        Args:
            client: BinanceClient 实例
        """
        self.client = client

    def close_futures_position(
        self,
        symbol: str,
        position_side: Optional[Literal["LONG", "SHORT", "BOTH"]] = None,
    ) -> Dict[str, Any]:
        """
        安全地平仓期货合约头寸

        ✅ 该函数确保：
          1. 使用正确的端点 (fapi.binance.com)
          2. 自动检测持仓方向
          3. 添加 reduceOnly=true 防止反向开仓
          4. 撤销所有挂单防止冲突

        Args:
            symbol: 交易对 (如 SOLUSDT)
            position_side: 持仓方向 ("LONG", "SHORT", "BOTH")
                          如果为 None 自动检测

        Returns:
            订单结果

        Raises:
            ValueError: 如果检测不到持仓
        """
        try:
            print(f"🔐 安全平仓开始: {symbol}")

            # 第一步：获取当前持仓
            position = self.client.get_position(symbol)
            if not position:
                raise ValueError(f"❌ 无法获取 {symbol} 的持仓信息")

            position_amt = float(position.get("positionAmt", 0))

            # 第二步：检查是否有持仓
            if position_amt == 0:
                print(f"⚠️ {symbol} 无持仓，无需平仓")
                return {"status": "no_position"}

            # 第三步：确定平仓方向
            if position_amt > 0:
                # 多头 → 平仓要卖出 (SELL)
                close_side = "SELL"
                close_direction = "多头"
            else:
                # 空头 → 平仓要买入 (BUY)
                close_side = "BUY"
                close_direction = "空头"

            close_qty = abs(position_amt)

            print(f"   📊 检测到持仓: {close_direction} {close_qty} {symbol}")

            # 第四步：撤销所有条件单 + 挂单 (防止遗留未触发止盈止损)
            print("   🗑️  撤销所有条件单与挂单...")
            try:
                # 先清理条件单（PAPI 条件单不会被 allOpenOrders 删除）
                if hasattr(self.client, "cancel_all_conditional_orders"):
                    self.client.cancel_all_conditional_orders(symbol)
                    print("      ✅ 已撤销条件单")
                # 再清理普通挂单
                self.client.cancel_all_orders(symbol)
                print("      ✅ 已撤销普通挂单")
            except Exception as e:
                print(f"   ⚠️  撤销挂单失败 (继续): {e}")

            # 第五步：验证端点
            endpoint = EndpointRouter.get_endpoint_for_order(symbol, is_spot=False)
            if not EndpointRouter.validate_endpoint_for_order(endpoint):
                raise ValueError(f"❌ 端点验证失败: {endpoint}")
            print(f"   ✅ 端点验证通过: {endpoint}")

            # 第六步：格式化数量
            formatted_qty = self.client.format_quantity(symbol, close_qty)
            if formatted_qty <= 0:
                raise ValueError(f"❌ 平仓数量无效: {close_qty} → {formatted_qty}")
            print(f"   ✅ 数量格式化: {close_qty} → {formatted_qty}")

            # 第七步：执行平仓 (关键: 必须加 reduceOnly=true)
            print("   📤 发送平仓订单...")
            print(f"      side={close_side}, qty={formatted_qty}, reduceOnly=true")

            order = self.client.create_market_order(
                symbol=symbol,
                side=close_side,
                quantity=formatted_qty,
                reduce_only=True,  # ⚠️ 关键参数
            )

            # 第八步：验证返回结果
            if not order:
                raise ValueError("平仓订单返回为空")

            order_id = order.get("orderId", "unknown")
            status = order.get("status", "unknown")

            print("   ✅ 平仓成功!")
            print(f"      订单ID: {order_id}")
            print(f"      状态: {status}")
            print(f"   🎉 {symbol} 平仓完成\n")

            return order

        except Exception as e:
            print(f"   ❌ 平仓失败: {e}\n")
            raise

    def close_spot_position(self, symbol: str) -> Dict[str, Any]:
        """
        安全地平仓现货头寸

        Args:
            symbol: 交易对 (如 BTCUSDT)

        Returns:
            订单结果
        """
        try:
            print(f"🔐 安全平仓现货开始: {symbol}")

            # 获取现货余额
            balance = self.client.get_balance(symbol.replace("USDT", ""))
            if not balance:
                raise ValueError(f"❌ 无法获取 {symbol} 的余额")

            free = float(balance.get("free", 0))
            if free <= 0:
                print(f"⚠️ {symbol} 无余额，无需平仓")
                return {"status": "no_balance"}

            print(f"   📊 检测到现货: {free} {symbol}")

            # 验证端点
            endpoint = EndpointRouter.get_endpoint_for_order(symbol, is_spot=True)
            if not EndpointRouter.validate_endpoint_for_order(endpoint):
                raise ValueError(f"❌ 端点验证失败: {endpoint}")
            print(f"   ✅ 端点验证通过: {endpoint}")

            # 格式化数量
            formatted_qty = self.client.format_quantity(symbol, free)
            if formatted_qty <= 0:
                raise ValueError(f"❌ 平仓数量无效: {free} → {formatted_qty}")

            # 执行现货卖出
            order = self.client.create_market_order(symbol=symbol, side="SELL", quantity=formatted_qty)

            print(f"   ✅ 现货平仓成功: 卖出 {formatted_qty} {symbol}\n")
            return order

        except Exception as e:
            print(f"   ❌ 现货平仓失败: {e}\n")
            raise


# ==================== 诊断工具 ====================


class EndpointDiagnostics:
    """端点诊断工具"""

    @staticmethod
    def diagnose_order_failure(error_message: str, symbol: str, endpoint_used: str) -> str:
        """
        诊断订单失败原因

        Args:
            error_message: 错误信息
            symbol: 交易对
            endpoint_used: 使用的端点

        Returns:
            诊断结果
        """
        diagnosis = []
        diagnosis.append("\n❌ 订单失败诊断")
        diagnosis.append(f"{'=' * 60}")
        diagnosis.append(f"交易对: {symbol}")
        diagnosis.append(f"端点: {endpoint_used}")
        diagnosis.append(f"错误: {error_message}")
        diagnosis.append(f"{'=' * 60}")

        # 常见错误诊断
        if "404" in error_message:
            diagnosis.append("\n⚠️ 错误类型: 404 Not Found")
            diagnosis.append("可能原因:")
            if "papi" in endpoint_used:
                diagnosis.append("  1. ❌ 使用了 papi.binance.com 下单")
                diagnosis.append("     → papi 只能用于账户信息，不能下单/平仓")
                diagnosis.append("  ✅ 解决: 改用 fapi.binance.com 或 api.binance.com")
            else:
                diagnosis.append("  1. 路径不正确")
                diagnosis.append("     futures: /fapi/v1/order")
                diagnosis.append("     spot: /api/v3/order")

        elif "reduceOnly" in error_message:
            diagnosis.append("\n⚠️ 错误类型: reduceOnly 参数问题")
            diagnosis.append("可能原因:")
            diagnosis.append("  1. reduceOnly=true 但当前无持仓")
            diagnosis.append("  2. reduceOnly 值不是 'true' (布尔转字符串)")
            diagnosis.append("  ✅ 解决: 检查持仓，确保参数值正确")

        elif "signature" in error_message.lower():
            diagnosis.append("\n⚠️ 错误类型: 签名错误")
            diagnosis.append("可能原因:")
            diagnosis.append("  1. API Key/Secret 错误")
            diagnosis.append("  2. 时间戳不同步")
            diagnosis.append("  ✅ 解决: 检查 API 密钥，同步系统时间")

        return "\n".join(diagnosis)

    @staticmethod
    def print_endpoint_cheatsheet():
        """打印端点速查表"""
        cheatsheet = """
╔════════════════════════════════════════════════════════════════╗
║           Binance API 端点速查表 (快速参考)                      ║
╚════════════════════════════════════════════════════════════════╝

┌─ 现货交易 ─────────────────────────────────────────────────────┐
│ 域名: api.binance.com                                           │
│ 下单: POST /api/v3/order                                        │
│ 查询: GET  /api/v3/account                                      │
└────────────────────────────────────────────────────────────────┘

┌─ U本位合约 (USDT-M Futures) ✅ ← SOLUSDT 在这里 ──────────────┐
│ 域名: fapi.binance.com                                          │
│ 下单: POST /fapi/v1/order                                       │
│ 平仓: POST /fapi/v1/order (+ reduceOnly=true)                  │
│ 查询: GET  /fapi/v1/account                                     │
│ 持仓: GET  /fapi/v1/positions (单个)                            │
└────────────────────────────────────────────────────────────────┘

┌─ 币本位合约 (Coin-M Futures) ──────────────────────────────────┐
│ 域名: dapi.binance.com                                          │
│ 下单: POST /dapi/v1/order                                       │
│ 平仓: POST /dapi/v1/order (+ reduceOnly=true)                  │
│ 查询: GET  /dapi/v1/account                                     │
└────────────────────────────────────────────────────────────────┘

┌─ PAPI (Portfolio Margin / 统一账户) ⚠️ 仅限账户操作 ──────────┐
│ 域名: papi.binance.com                                          │
│ ✅ 可用: GET  /papi/v1/um/account (账户信息)                    │
│ ✅ 可用: GET  /papi/v1/um/positionRisk (持仓风险)               │
│ ❌ 禁用: POST /papi/v1/order (会 404 Not Found!)               │
│                                                                 │
│ 原因: papi 不是下单接口，改用 fapi/dapi                        │
└────────────────────────────────────────────────────────────────┘

╔════════════════════════════════════════════════════════════════╗
║                  平仓必备参数 (reduceOnly)                      ║
╠════════════════════════════════════════════════════════════════╣
║ 开仓:     reduceOnly=false (或不传)                             ║
║ 平仓:     reduceOnly=true  ← ⚠️  关键!                         ║
║                                                                 ║
║ 作用: 防止平仓时误反向开仓                                      ║
║      例如: 想平多头，结果没有成交，却反向开了空头              ║
╚════════════════════════════════════════════════════════════════╝
        """
        print(cheatsheet)


if __name__ == "__main__":
    # 打印诊断信息
    EndpointDiagnostics.print_endpoint_cheatsheet()

    # 示例诊断
    diagnosis = EndpointDiagnostics.diagnose_order_failure(
        error_message="404 Not Found - /papi/v1/order",
        symbol="SOLUSDT",
        endpoint_used="papi.binance.com",
    )
    print(diagnosis)
