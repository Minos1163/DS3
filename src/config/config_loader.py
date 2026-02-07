"""
配置加载器
负责加载和验证配置文件
"""

import json
import os
from typing import Any, Dict


class ConfigLoader:
    """配置加载器"""

    @staticmethod
    def load_json_config(file_path: str) -> Dict[str, Any]:
        """
        加载JSON配置文件

        Args:
            file_path: 配置文件路径

        Returns:
            配置字典

        Raises:
            FileNotFoundError: 文件不存在
            json.JSONDecodeError: JSON格式错误
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"配置文件不存在: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        return config

    @staticmethod
    def load_trading_config(
        file_path: str = "config/trading_config.json",
    ) -> Dict[str, Any]:
        """
        加载交易配置

        Returns:
            {
            'environment': {'mode': 'production', ...},
                'trading': {...},
                'risk': {...},
                'ai': {...},
                'schedule': {...}
            }
        """
        try:
            config = ConfigLoader.load_json_config(file_path)

            # 验证必要的配置
            ConfigLoader.validate_trading_config(config)

            return config
        except Exception as e:
            print(f"❌ 加载交易配置失败: {e}")
            raise

    @staticmethod
    def validate_trading_config(config: Dict[str, Any]) -> bool:
        """
        验证交易配置

        Returns:
            True if valid

        Raises:
            ValueError: 配置无效
        """
        # 检查必需字段
        required_sections = ["trading"]
        for section in required_sections:
            if section not in config:
                raise ValueError(f"配置缺少必要的部分: {section}")

        trading = config["trading"]
        if "symbols" not in trading or not trading["symbols"]:
            raise ValueError("配置中必须指定至少一个交易币种")

        return True

    @staticmethod
    def get_trading_symbols(config: Dict[str, Any]) -> list:
        """
        获取交易币种列表

        """
        symbols = config.get("trading", {}).get("symbols", [])
        return symbols

    @staticmethod
    def get_default_leverage(config: Dict[str, Any]) -> int:
        """获取默认杠杆"""
        return config.get("trading", {}).get("default_leverage", 3)

    @staticmethod
    def get_position_limits(config: Dict[str, Any]) -> Dict[str, float]:
        """获取仓位限制配置"""
        trading = config.get("trading", {})
        return {
            "min_percent": trading.get("min_position_percent", 10) / 100,
            "max_percent": trading.get("max_position_percent", 30) / 100,
            "reserve_percent": trading.get("reserve_percent", 20) / 100,
        }

    @staticmethod
    def get_risk_limits(config: Dict[str, Any]) -> Dict[str, float]:
        """获取风险限制配置"""
        risk = config.get("risk", {})
        # 与 RiskManager 保持一致：接收两种格式，优先解释为整数百分比
        raw_max = risk.get("max_daily_loss_percent", 10)
        try:
            rv = float(raw_max)
        except Exception:
            rv = 10.0
        if rv > 1.0:
            max_daily_loss_pct = rv / 100.0
        else:
            max_daily_loss_pct = rv

        return {
            "max_daily_loss_percent": max_daily_loss_pct,
            "max_consecutive_losses": risk.get("max_consecutive_losses", 5),
            "stop_loss_default_percent": risk.get("stop_loss_default_percent", 2) / 100,
            "take_profit_default_percent": risk.get("take_profit_default_percent", 5)
            / 100,
        }

    @staticmethod
    def get_ai_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """获取AI配置"""
        return config.get("ai", {})

    @staticmethod
    def get_schedule_config(config: Dict[str, Any]) -> Dict[str, int]:
        """获取调度配置"""
        schedule = config.get("schedule", {})
        return {
            "interval_seconds": schedule.get("interval_seconds", 180),
            "retry_times": schedule.get("retry_times", 3),
            "retry_delay_seconds": schedule.get("retry_delay_seconds", 5),
            # 启动/每次K线更新后等待多少秒再开始下载并分析（应小于等于30）
            "download_delay_seconds": schedule.get("download_delay_seconds", 5),
        }
