"""
Classic + PAPI-only 专用下单器
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import math
import time


class ClassicPapiOrderExecutor:
    """
    专用于 Classic + PAPI-only 账户的安全下单器
    """

    def __init__(
        self,
        client: Any,
        max_account_risk: float = 0.3,
        safety_buffer: float = 0.85,
        dry_run: bool = True,
    ) -> None:
        self.client = client
        self.max_account_risk = max_account_risk
        self.safety_buffer = safety_buffer
        self.dry_run = dry_run

    def fetch_margin_snapshot(self) -> Dict[str, float]:
        url = f"{self.client.broker.PAPI_BASE}/papi/v1/um/account"
        response = self.client.broker.request("GET", url, signed=True)
        data = response.json()

        total_wallet = 0.0
        available = 0.0

        for asset in data.get("assets", []):
            asset_name = asset.get("asset")
            if asset_name in ("USDT", "FDUSD"):
                wallet = float(asset.get("crossWalletBalance", 0) or 0)
                initial_margin = float(asset.get("initialMargin", 0) or 0)
                total_wallet += wallet
                available += wallet - initial_margin

        # SPOT 备选方案：当 PAPI 的可用保证金为负时，使用 SPOT 余额
        if available <= 0:
            try:
                # 先尝试全仓杠杆账户
                margin_url = f"{self.client.broker.SPOT_BASE}/sapi/v1/margin/account"
                margin_response = self.client.broker.request("GET", margin_url, signed=True)
                margin_data = margin_response.json()
                for asset in margin_data.get("userAssets", []):
                    if asset.get("asset") == "USDT":
                        margin_usdt = float(asset.get("free", 0)) + float(asset.get("locked", 0))
                        if margin_usdt > 0:
                            available = max(available, margin_usdt)
                            print(f"   💡 [全仓杠杆备选] 使用杠杆USDT: {margin_usdt:.8f}")
                        break

                # 如果全仓杠杆也没有，尝试现货
                if available <= 0:
                    spot_url = f"{self.client.broker.SPOT_BASE}/api/v3/account"
                    spot_response = self.client.broker.request("GET", spot_url, signed=True)
                    spot_data = spot_response.json()
                    for asset in spot_data.get("balances", []):
                        if asset.get("asset") == "USDT":
                            spot_usdt = float(asset.get("free", 0)) + float(asset.get("locked", 0))
                            if spot_usdt > 0:
                                available = max(available, spot_usdt)
                                print(f"   💡 [现货备选] 使用现货USDT: {spot_usdt:.8f}")
                            break
            except Exception as e:
                print(f"   ⚠️ 备选方案失败: {e}")

        return {
            "total_wallet": total_wallet,
            "available": max(available, 0.0),
        }

    def calc_max_position(
        self,
        price: float,
        leverage: int,
        available_margin: float,
    ) -> float:
        usable_margin = available_margin * self.max_account_risk * self.safety_buffer
        max_notional = usable_margin * leverage
        qty = max_notional / price if price > 0 else 0.0
        qty = math.floor(qty * 1000) / 1000
        return max(qty, 0.0)

    def _normalize_positions(self) -> Dict[str, Dict[str, Any]]:
        raw_positions = self.client.get_all_positions() or {}
        if isinstance(raw_positions, dict):
            return raw_positions
        if isinstance(raw_positions, list):
            positions: Dict[str, Dict[str, Any]] = {}
            for pos in raw_positions:
                symbol = pos.get("symbol")
                if symbol:
                    positions[symbol] = pos
            return positions
        return {}

    def risk_guard(self, symbol: str, side: str) -> None:
        positions = self._normalize_positions()
        pos = positions.get(symbol)
        if not pos:
            return

        amt = float(pos.get("positionAmt", pos.get("amount", 0)) or 0)
        if amt == 0:
            return

        if amt > 0 and side.upper() == "SELL":
            raise RuntimeError(f"❌ 已有多仓 {symbol}，禁止反向 SELL")
        if amt < 0 and side.upper() == "BUY":
            raise RuntimeError(f"❌ 已有空仓 {symbol}，禁止反向 BUY")

    def place_market_order(
        self,
        symbol: str,
        side: str,
        price: float,
        leverage: int,
    ) -> Optional[Dict[str, Any]]:
        snapshot = self.fetch_margin_snapshot()
        available = snapshot["available"]

        if available <= 0:
            raise RuntimeError("❌ 可用保证金为 0，禁止下单")

        self.risk_guard(symbol, side)

        qty = self.calc_max_position(price, leverage, available)
        if qty <= 0:
            raise RuntimeError("❌ 计算出的下单数量为 0")

        if hasattr(self.client, "format_quantity"):
            qty = float(self.client.format_quantity(symbol, qty))

        if self.dry_run:
            print("🧪 [DRY-RUN] Classic PAPI 下单预演")
            print(f"symbol={symbol}")
            print(f"side={side}")
            print(f"price={price}")
            print(f"leverage={leverage}")
            print(f"available_margin={available:.4f}")
            print(f"final_qty={qty}")
            return {
                "dry_run": True,
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "available_margin": available,
                "leverage": leverage,
            }

        url = f"{self.client.broker.PAPI_BASE}/papi/v1/um/order"
        payload = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": qty,
            "timestamp": int(time.time() * 1000),
        }

        response = self.client.broker.request(
            "POST",
            url,
            signed=True,
            params=payload,
        )
        result = response.json()
        if isinstance(result, dict):
            result.setdefault("_calculated_qty", qty)
        return result
