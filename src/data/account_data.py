"""
账户数据管理器
负责获取账户信息
"""

from typing import Any, Dict, List, Optional
import time
import os
from src.config.config_loader import ConfigLoader


class AccountDataManager:
    """账户数据管理器"""

    def __init__(self, client, config_path: Optional[str] = None):
        """
        初始化账户数据管理器

        Args:
            client: Binance API客户端
            config_path: 交易配置文件路径（可选）
        """
        self.client = client
        self.config_path = config_path
        # 缓存上一次成功的账户摘要（用于请求失败时回退）
        self._last_account_summary: Optional[Dict[str, Any]] = None
        self._last_account_updated_at: float = 0.0
        # 缓存有效期（秒），默认 60 秒，可按需调整或暴露为参数
        self._cache_ttl: float = 60.0
        # 默认关闭 traceback 刷屏，排查时可通过环境变量开启
        self._verbose_errors: bool = os.getenv("BINANCE_VERBOSE_ACCOUNT_ERRORS") == "1"

    @staticmethod
    def _brief_error(error: BaseException, max_len: int = 220) -> str:
        msg = str(error).strip().replace("\n", " ")
        if len(msg) > max_len:
            return msg[:max_len] + "..."
        return msg

    def get_account_summary(self) -> Optional[Dict[str, Any]]:
        """
        获取账户摘要 - 支持统一账户、全仓杠杆、U本位、币本位合约

        统一账户计算方式：
        - 可用保证金 = totalWalletBalance - totalInitialMargin
        - 权益 = totalWalletBalance + totalUnrealizedProfit

        Returns:
            {
            'total_balance': 10000.0,
                'available_balance': 8000.0,
                'used_margin': 2000.0,
                'total_unrealized_pnl': 100.0,
                'equity': 10100.0,
                'margin_ratio': 0.2,
                ...
            }
        """
        # ========== 获取账户信息：增加重试，并在失败时使用缓存回退 ==========
        retries = 3
        backoff = 1.0
        account = None
        _last_exc: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                account = self.client.get_account()
                break
            except Exception as e:
                _last_exc = e
                print(f"⚠️ 获取账户信息失败（尝试 {attempt + 1}/{retries}）：{self._brief_error(e)}")
                if self._verbose_errors:
                    try:
                        import traceback

                        traceback.print_exc()
                    except Exception:
                        pass
                time.sleep(backoff)
                backoff *= 2

        if not account:
            # 如果有缓存且缓存仍在有效期内，作为回退返回缓存摘要
            if (
                self._last_account_summary is not None
                and (time.time() - self._last_account_updated_at) <= self._cache_ttl
            ):
                print("⚠️ 使用缓存账户摘要回退（最近更新时间：%s）" % time.ctime(self._last_account_updated_at))
                return self._last_account_summary
            # 否则记录并返回 None
            print("❌ 获取账户信息失败，且无可用缓存，返回 None")
            return None

        try:
            # 如果包含 raw，则作为辅助来源，但优先使用 account 顶层字段
            raw = account.get("raw") if isinstance(account, dict) else None
            raw_data = raw if isinstance(raw, dict) else None

            # 兼容 raw 的套壳结构
            if isinstance(raw_data, dict):
                unwrap_keys = (
                    "data",
                    "account",
                    "accountInfo",
                    "futureAccountResp",
                    "umAccountResp",
                    "umAccount",
                    "umAccountInfo",
                )
                for key in unwrap_keys:
                    inner = raw_data.get(key)
                    if isinstance(inner, dict):
                        raw_data = inner
                        break

            # ============ 统一账户 (优先使用 account 顶层字段) ============
            total_wallet_balance = self._extract_float(account, ["totalWalletBalance"])
            total_initial_margin = self._extract_float(account, ["totalInitialMargin"])
            total_unrealized_profit = self._extract_float(account, ["totalUnrealizedProfit"])
            equity = self._extract_float(account, ["equity", "accountEquity", "totalMarginBalance"])
            available_balance = self._extract_float(
                account,
                [
                    "available",
                    "availableBalance",
                    "availableForTrade",
                    "maxWithdrawAmount",
                ],
            )

            # raw 回退（尤其是 /papi/v1/account 返回的 accountEquity）
            if total_wallet_balance == 0 and isinstance(raw_data, dict):
                total_wallet_balance = self._extract_float(
                    raw_data,
                    ["totalWalletBalance", "accountEquity", "actualEquity"],
                )
            if total_initial_margin == 0 and isinstance(raw_data, dict):
                total_initial_margin = self._extract_float(
                    raw_data,
                    ["totalInitialMargin", "accountInitialMargin"],
                )
            if total_unrealized_profit == 0 and isinstance(raw_data, dict):
                total_unrealized_profit = self._extract_float(
                    raw_data,
                    ["totalUnrealizedProfit", "totalMarginOpenLoss"],
                )
            if equity == 0 and isinstance(raw_data, dict):
                equity = self._extract_float(
                    raw_data,
                    ["equity", "accountEquity", "actualEquity"],
                )
            if available_balance == 0 and isinstance(raw_data, dict):
                available_balance = self._extract_float(
                    raw_data,
                    [
                        "available",
                        "totalAvailableBalance",
                        "virtualMaxWithdrawAmount",
                    ],
                )

            # ============ 根据统一账户字段计算可用保证金 ============
            # 统一账户的可用保证金 = 钱包余额 - 占用保证金
            calculated_available = total_wallet_balance - total_initial_margin if total_wallet_balance > 0 else 0

            # 如果API直接返回的available有效，优先使用；否则使用计算值
            if available_balance <= 0 and calculated_available > 0:
                available_balance = calculated_available

            # ============ 根据统一账户字段计算权益 ============
            # 如果没有直接的equity字段，则计算：equity = wallet + unrealized
            if equity <= 0:
                equity = total_wallet_balance + total_unrealized_profit

            # ============ 资产级聚合值 (作为备用和验证) ============
            assets: List[Any] = []
            if isinstance(account, dict) and isinstance(account.get("assets"), list) and account.get("assets"):
                assets = account.get("assets")  # type: ignore[assignment]
            elif isinstance(raw_data, dict) and isinstance(raw_data.get("assets"), list):
                assets = raw_data.get("assets")  # type: ignore[assignment]
            assets_total_wallet = 0.0
            assets_total_unrealized = 0.0
            assets_total_margin = 0.0
            assets_usdt_wallet = 0.0
            assets_usdt_unrealized = 0.0
            assets_usdt_margin = 0.0

            if isinstance(assets, list):
                for a in assets:
                    wallet = float(a.get("walletBalance") or a.get("crossWalletBalance") or a.get("balance") or 0)
                    assets_total_wallet += wallet

                    unrealized = float(
                        a.get("unrealizedProfit") or a.get("crossUnPnl") or a.get("unRealizedProfit") or 0
                    )
                    assets_total_unrealized += unrealized

                    margin = float(a.get("initialMargin") or a.get("totalInitialMargin") or 0)
                    assets_total_margin += margin

                    asset_symbol = a.get("asset") or a.get("currency") or a.get("symbol")
                    if asset_symbol in ("USDT", "FDUSD"):
                        assets_usdt_wallet += wallet
                        assets_usdt_unrealized += unrealized
                        assets_usdt_margin += margin

            # ============ 回退逻辑：统一字段无效时优先使用 USDT/FDUSD 聚合值 ============
            if total_wallet_balance == 0:
                total_wallet_balance = assets_usdt_wallet or assets_total_wallet
            if total_initial_margin == 0:
                total_initial_margin = assets_usdt_margin or assets_total_margin
            if total_unrealized_profit == 0:
                # 如果 account 接口没有提供 total_unrealized_profit（返回0），
                # 尝试从持仓接口聚合未实现盈亏作为回退（某些账户/端点不会在账户快照中包含该字段）
                total_unrealized_profit = assets_usdt_unrealized or assets_total_unrealized
                try:
                    # 支持通过配置文件或环境变量设置阈值（单位 USDT），默认 0.01
                    threshold = 0.01
                    try:
                        cfg = ConfigLoader.load_trading_config(
                            self.config_path or "config/trading_config_vps.json"
                        )
                        threshold = ConfigLoader.get_unrealized_pnl_threshold_usdt(cfg)
                    except Exception:
                        try:
                            threshold = float(os.getenv("UNREALIZED_AGGREGATE_THRESHOLD_USDT", "0.01"))
                        except Exception:
                            threshold = 0.01

                    # 优先使用 client 提供的持仓查询方法聚合未实现盈亏
                    positions = []
                    if hasattr(self.client, "get_all_positions"):
                        positions = self.client.get_all_positions()
                    elif hasattr(self.client, "get_positions"):
                        positions = self.client.get_positions()

                    pos_unrealized_sum = 0.0
                    for p in positions or []:
                        # 兼容不同字段名
                        val = (
                            p.get("unrealizedProfit")
                            or p.get("unRealizedProfit")
                            or p.get("unrealized")
                            or p.get("unRealized")
                            or p.get("crossUnPnl")
                            or 0
                        )
                        try:
                            pos_unrealized_sum += float(val)
                        except Exception:
                            continue

                    # 只有当聚合绝对值超过阈值时才覆盖（避免微小波动误报）
                    if abs(pos_unrealized_sum) >= threshold:
                        total_unrealized_profit = pos_unrealized_sum
                except Exception:
                    # 回退时不阻塞主流程，保持原有值
                    pass

            # 重新计算可用和权益
            if total_wallet_balance > 0:
                available_balance = total_wallet_balance - total_initial_margin
            if equity == 0:
                equity = total_wallet_balance + total_unrealized_profit
            if available_balance < 0:
                available_balance = 0.0

            spot_usdt = self._extract_float(account, ["spotUsdtBalance"])
            spot_ldusdt = self._extract_float(account, ["spotLdUsdtBalance"])
            spot_total = self._extract_float(account, ["spotTotalBalance"])

            # 调试：如关键数值都为0，输出原始账户数据辅助排查
            if total_wallet_balance == 0 and available_balance == 0 and equity == 0:
                print("⚠️ 账户关键字段为0，原始返回摘要:")
                print(self._summarize_account(account))

            result = {
                "total_balance": total_wallet_balance,
                "available_balance": available_balance,
                "used_margin": total_initial_margin,
                "total_unrealized_pnl": total_unrealized_profit,
                "equity": equity,
                "spot_usdt_balance": spot_usdt,
                "spot_ldusdt_balance": spot_ldusdt,
                "spot_total_balance": spot_total,
                "margin_ratio": self._calculate_margin_ratio_v2(total_wallet_balance, total_initial_margin),
                "update_time": account.get("updateTime", 0),
                "note": account.get("note"),
                "raw_account": account,
            }
            # 更新缓存
            try:
                self._last_account_summary = result
                self._last_account_updated_at = time.time()
            except Exception:
                pass
            return result
        except Exception as e:
            print(f"⚠️ 获取账户摘要失败: {self._brief_error(e)}")
            if self._verbose_errors:
                import traceback

                traceback.print_exc()
            # 出错时尝试返回缓存（如果存在且未过期）
            if (
                self._last_account_summary is not None
                and (time.time() - self._last_account_updated_at) <= self._cache_ttl
            ):
                print("⚠️ 出错时使用缓存账户摘要回退（最近更新时间：%s）" % time.ctime(self._last_account_updated_at))
                return self._last_account_summary
            return None

    def _calculate_margin_ratio_v2(self, total_wallet: float, total_margin: float) -> float:
        """
        计算保证金率 (统一账户版本)

        保证金率 = 占用保证金 / 钱包余额 * 100%

        Args:
            total_wallet: 钱包余额 (totalWalletBalance)
            total_margin: 占用保证金 (totalInitialMargin)

        Returns:
            保证金率百分比
        """
        if total_wallet <= 0:
            return 0.0
        return (total_margin / total_wallet) * 100

    def _calculate_margin_ratio(self, account: Dict[str, Any]) -> float:
        """计算保证金率 (旧版本，保留兼容)"""
        try:
            total_balance = self._extract_float(account, ["totalWalletBalance", "equity", "walletBalance"])
            if total_balance == 0:
                return 0.0

            used_margin = self._extract_float(account, ["totalInitialMargin", "usedMargin"])
            return (used_margin / total_balance) * 100
        except BaseException:
            return 0.0

    def _extract_float(self, account: Dict[str, Any], keys: List[str]) -> float:
        """从多个候选字段中提取首个有效的浮点数"""
        for key in keys:
            val = account.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _summarize_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """压缩打印信息，避免长列表刷屏"""
        raw = account.get("raw") if isinstance(account, dict) else None
        raw_positions = raw.get("positions") if isinstance(raw, dict) else []
        raw_assets = raw.get("assets") if isinstance(raw, dict) else []

        return {
            "keys": list(account.keys()),
            "top_fields": {
                "totalWalletBalance": account.get("totalWalletBalance"),
                "availableBalance": account.get("availableBalance"),
                "totalMarginBalance": account.get("totalMarginBalance"),
                "accountEquity": account.get("accountEquity"),
                "equity": account.get("equity"),
                "available": account.get("available"),
            },
            "assets_count": (len(raw_assets) if isinstance(raw_assets, list) else 0),
            "positions_count": (len(raw_positions) if isinstance(raw_positions, list) else 0),
            "note": account.get("note"),
            "raw_keys": list(raw.keys()) if isinstance(raw, dict) else None,
            "raw_top_fields": {
                "totalWalletBalance": (raw.get("totalWalletBalance") if isinstance(raw, dict) else None),
                "totalMarginBalance": (raw.get("totalMarginBalance") if isinstance(raw, dict) else None),
                "totalInitialMargin": (raw.get("totalInitialMargin") if isinstance(raw, dict) else None),
                "totalUnrealizedProfit": (raw.get("totalUnrealizedProfit") if isinstance(raw, dict) else None),
                "accountEquity": (raw.get("accountEquity") if isinstance(raw, dict) else None),
                "equity": raw.get("equity") if isinstance(raw, dict) else None,
                "available": (raw.get("available") if isinstance(raw, dict) else None),
            },
        }

    def get_available_balance(self) -> float:
        """获取可用余额"""
        summary = self.get_account_summary()
        return summary.get("available_balance", 0.0) if summary else 0.0

    def get_total_equity(self) -> float:
        """获取账户总权益"""
        summary = self.get_account_summary()
        return summary.get("equity", 0.0) if summary else 0.0

    def get_total_unrealized_pnl(self) -> float:
        """获取总未实现盈亏"""
        summary = self.get_account_summary()
        return summary.get("total_unrealized_pnl", 0.0) if summary else 0.0
