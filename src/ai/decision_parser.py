"""
决策解析器
负责解析AI返回的决策JSON
"""

import json
import re
from typing import Any, Dict


class DecisionParser:
    """决策解析器"""

    @staticmethod
    def parse_ai_response(response: str) -> Dict[str, Any]:
        """
        解析AI响应为结构化决策

        Args:
            response: AI返回的JSON字符串

        Returns:
            {
                'action': 'BUY_OPEN',
                'confidence': 0.85,
                'leverage': 5,
                'position_percent': 20,
                'take_profit_percent': 5.0,
                'stop_loss_percent': -2.0,
                'reason': '...'
            }
        """
        try:
            # 尝试提取JSON（去除可能的markdown代码块）
            response = response.strip()

            # 如果被markdown代码块包裹，提取内容
            if "```" in response:
                match = re.search(
                    r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL
                )
                if match:
                    response = match.group(1)

            # 解析JSON
            decision = json.loads(response)

            return DecisionParser.apply_defaults(decision)

        except json.JSONDecodeError as e:
            print(f"⚠️ JSON解析失败: {e}")
            print(f"原始响应: {response}")
            return DecisionParser._get_default_decision()
        except Exception as e:
            print(f"⚠️ 解析决策时出错: {e}")
            return DecisionParser._get_default_decision()

    @staticmethod
    def apply_defaults(decision: Dict[str, Any]) -> Dict[str, Any]:
        """
        应用默认值

        Args:
            decision: 原始决策

        Returns:
            带默认值的决策
        """
        defaults = {
            "action": "HOLD",
            "confidence": 0.5,
            "leverage": 3,
            "position_percent": 15,
            "take_profit_percent": 5.0,
            "stop_loss_percent": -2.0,
            "reason": "默认持有",
        }

        # 应用默认值
        for key, default_value in defaults.items():
            if key not in decision:
                decision[key] = default_value

        return decision

    @staticmethod
    def validate_decision(decision: Dict[str, Any]) -> tuple[bool, str]:
        """
        验证决策合法性

        Returns:
            (是否有效, 错误消息)
        """
        # 检查必需字段
        required_fields = [
            "action",
            "confidence",
            "leverage",
            "position_percent",
        ]
        for field in required_fields:
            if field not in decision:
                return False, f"缺少必需字段: {field}"

        # 检查action
        valid_actions = ["BUY_OPEN", "SELL_OPEN", "CLOSE", "HOLD"]
        if decision["action"] not in valid_actions:
            return False, f"无效的action: {decision['action']}"

        # 检查confidence
        if not 0 <= decision["confidence"] <= 1:
            return False, f"confidence必须在0-1之间: {decision['confidence']}"

        # 检查leverage
        if not 1 <= decision["leverage"] <= 100:
            return False, f"leverage必须在1-100之间: {decision['leverage']}"

        # 检查position_percent
        if not 10 <= decision["position_percent"] <= 30:
            return (
                False,
                f"position_percent必须在10-30之间: {decision['position_percent']}",
            )

        return True, ""

    @staticmethod
    def _get_default_decision() -> Dict[str, Any]:
        """获取默认决策（解析失败时返回）"""
        return {
            "action": "HOLD",
            "confidence": 0.0,
            "leverage": 1,
            "position_percent": 0,
            "take_profit_percent": 0.0,
            "stop_loss_percent": 0.0,
            "reason": "AI响应解析失败，保持现状",
        }

    @staticmethod
    def extract_reason(decision: Dict[str, Any]) -> str:
        """提取决策理由"""
        return decision.get("reason", "无理由")

    @staticmethod
    def extract_action(decision: Dict[str, Any]) -> str:
        """提取交易动作"""
        return decision.get("action", "HOLD")

    @staticmethod
    def extract_confidence(decision: Dict[str, Any]) -> float:
        """提取信心度"""
        return decision.get("confidence", 0.0)

    @staticmethod
    def parse_multi_symbol_response(
        response: str,
    ) -> Dict[str, Dict[str, Any]]:
        """
        解析多币种AI响应

        Args:
            response: AI返回的多币种JSON字符串

        Returns:
            {
                'BTCUSDT': {decision...},
                'ETHUSDT': {decision...},
                ...
            }
        """
        try:
            # 尝试提取JSON（去除可能的markdown代码块）
            response = response.strip()

            # 如果被markdown代码块包裹，提取内容
            if "```" in response:
                match = re.search(
                    r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL
                )
                if match:
                    response = match.group(1)

            # 解析JSON
            all_decisions = json.loads(response)

            # 为每个币种应用默认值
            for symbol, decision in all_decisions.items():
                if isinstance(decision, dict):
                    # 特殊处理 confidence：如果是字符串，转换为数字
                    if "confidence" in decision and isinstance(
                        decision["confidence"], str
                    ):
                        conf_str = decision["confidence"].upper()
                        if conf_str == "HIGH":
                            decision["confidence"] = 0.8
                        elif conf_str == "MEDIUM":
                            decision["confidence"] = 0.6
                        elif conf_str == "LOW":
                            decision["confidence"] = 0.4
                        else:
                            decision["confidence"] = 0.5

                    all_decisions[symbol] = DecisionParser.apply_defaults(decision)

            return all_decisions

        except json.JSONDecodeError as e:
            print(f"⚠️ 多币种JSON解析失败: {e}")
            print(f"原始响应: {response}")
            # 返回空字典，表示所有币种都HOLD
            return {}
        except Exception as e:
            print(f"⚠️ 解析多币种决策时出错: {e}")
            import traceback

            traceback.print_exc()
            return {}
